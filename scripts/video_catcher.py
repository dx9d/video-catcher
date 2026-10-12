#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Download-history for dedupe of ordinary downloads.
DEFAULT_DOWNLOAD_HISTORY = Path.home() / ".video-catcher" / "download-history.json"

from common import (
    DEFAULT_USER_AGENT,
    clean_url,
    find_executable,
    load_netscape_cookies,
    python_module_available,
    safe_forward_headers,
    unique_run_dir,
    ytdlp_command,
)
from direct_downloader import download_direct
from media_detector import detect_manifest_drm, rank_candidates, url_kind
from media_muxer import mux_video_audio
from format_inspector import MediaFormat, inspect_manifest_formats, quality_label
from models import DownloadRecord, MediaCandidate
from report import write_download_report, write_formats_report, write_inspect_report, write_task_state
from static_resolver import resolve_static
from stream_downloader import download_stream_ffmpeg
from verifier import verify_video
import article_archiver
from ytdlp_downloader import download_ytdlp, format_selector, probe_ytdlp, probe_ytdlp_formats
from youtube_pot import (
    bgutil_plugin_version,
    pot_prerequisites,
    pot_worthy_error,
    provider_server_dir,
    youtube_pot_session,
)


def default_out_root() -> Path:
    env = os.environ.get("VIDEO_CATCHER_OUT", "").strip()
    if env:
        return Path(env).expanduser()
    # 默认保存到用户主目录 Videos；可用 VIDEO_CATCHER_OUT 覆盖
    return Path("~/Videos").expanduser()


def _session(cookies_file: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    if cookies_file:
        for item in load_netscape_cookies(Path(cookies_file)):
            try:
                session.cookies.set(
                    item["name"], item["value"], domain=item.get("domain"), path=item.get("path") or "/"
                )
            except Exception:
                pass
    return session


def _merge_candidates(*groups: list[MediaCandidate]) -> list[MediaCandidate]:
    merged: list[MediaCandidate] = []
    for group in groups:
        merged.extend(group)
    return rank_candidates(merged)


def _probe_drm(session: requests.Session, candidates: list[MediaCandidate]) -> None:
    for item in candidates:
        if item.kind != "stream":
            continue
        status, note = detect_manifest_drm(session, item)
        item.drm_status = status
        item.drm = status == "protected"
        item.drm_note = note


def _is_youtube_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "youtube.com/" in lowered or "youtu.be/" in lowered


def _lux_binary() -> str | None:
    """Locate a Lux binary (mainland-China video downloader) on PATH or via LUX_BIN."""
    env_bin = os.environ.get("LUX_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    return find_executable("lux")


# Platforms where Lux has materially better coverage than yt-dlp.
_LUX_HOSTS = (
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "gifshow.com",
    "iqiyi.com", "iq.com",
    "v.qq.com", "qq.com",
    "youku.com", "ykimg.com",
    "ixigua.com", "toutiao.com",
    "weibo.com", "weibo.cn",
    "acfun.cn",
    "baidu.com/video", "haokan.baidu.com",
    "sohu.com", "56.com", "mgtv.com", "fun.tv",
)


def _is_lux_candidate(url: str) -> bool:
    lowered = (url or "").lower()
    if "bilibili.com" in lowered or "b23.tv" in lowered:
        return False  # Bilibili stays on yt-dlp (with Lux as an optional fallback).
    return any(host in lowered for host in _LUX_HOSTS)


def download_lux(url: str, out_dir: Path, cookies_file: str = "") -> DownloadRecord:
    """Download a mainland-China platform video with the bundled Lux binary.

    Lux is tried only as a fallback when yt-dlp fails on CN platforms. It supports
    multi-thread download, cookies, and referrer headers. Output goes to out_dir and
    the produced file is located afterwards because Lux names files by its own rules.
    """
    import subprocess

    bin_path = _lux_binary()
    record = DownloadRecord(source_url=url, strategy="lux")
    if not bin_path:
        record.status = "failed"
        record.note = "Lux binary not found (install lux or set LUX_BIN)"
        return record

    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in out_dir.glob("*")}
    cmd = [bin_path, url, "-o", str(out_dir), "-m", "-retry", "10"]
    if cookies_file:
        try:
            from common import load_netscape_cookies

            cookies = "; ".join(
                f"{c['name']}={c['value']}" for c in load_netscape_cookies(Path(cookies_file))
            )
            if cookies:
                cmd += ["-c", cookies]
        except Exception:
            pass
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        record.status = "failed"
        record.note = "Lux timed out after 30 minutes"
        return record
    except Exception as exc:  # noqa: BLE001
        record.status = "failed"
        record.note = f"Lux execution error: {str(exc)[:180]}"
        return record

    if proc.returncode != 0:
        record.status = "failed"
        record.note = (proc.stderr or proc.stdout or "").strip().splitlines()[-1][:220] or "Lux exited non-zero"
        return record

    after = [p for p in out_dir.glob("*") if p.resolve() not in before and p.is_file()]
    if not after:
        record.status = "failed"
        record.note = "Lux reported success but no output file appeared"
        return record
    record.status = "ok"
    record.files = [str(p) for p in after]
    record.bytes = sum(p.stat().st_size for p in after)
    record.note = "downloaded via Lux"
    return record


def _youtube_pot_enabled(args: argparse.Namespace) -> bool:
    return not bool(getattr(args, "no_youtube_pot", False))


def _youtube_pot_setup_timeout(args: argparse.Namespace) -> int:
    return min(max(int(getattr(args, "timeout", 600) or 600), 120), 600)


def _file_has_audio(path: Path) -> bool | None:
    _state, _note, payload = verify_video(path)
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not streams:
        return None
    return any(item.get("codec_type") == "audio" for item in streams)


def _best_audio_candidate(candidates: list[MediaCandidate], video_candidate: MediaCandidate) -> MediaCandidate | None:
    from urllib.parse import urlparse

    video_url = urlparse(video_candidate.url)
    video_parent = video_url.path.rsplit("/", 1)[0]
    ranked: list[tuple[int, int, int, MediaCandidate]] = []
    for item in candidates:
        if item.kind != "audio" or item.score < 0:
            continue
        score = item.score
        audio_url = urlparse(item.url)
        audio_parent = audio_url.path.rsplit("/", 1)[0]
        if item.source == video_candidate.source:
            score += 50
        if audio_url.netloc and audio_url.netloc == video_url.netloc:
            score += 25
        if video_parent and audio_parent and (video_parent == audio_parent or video_parent.startswith(audio_parent) or audio_parent.startswith(video_parent)):
            score += 15
        if item.source.startswith("browser-") and video_candidate.source.startswith("browser-"):
            score += 10
        ranked.append((score, item.score, item.content_length, item))
    return max(ranked, key=lambda row: row[:3], default=(0, 0, 0, None))[3]


def _attach_separate_audio_if_needed(
    record: DownloadRecord,
    video_candidate: MediaCandidate,
    all_candidates: list[MediaCandidate],
    session: requests.Session,
    out_dir: Path,
    args: argparse.Namespace,
) -> DownloadRecord:
    if record.status != "ok" or len(record.files) != 1:
        return record
    audio_candidate = _best_audio_candidate(all_candidates, video_candidate)
    if audio_candidate is None:
        return record
    video_path = Path(record.files[0])
    has_audio = _file_has_audio(video_path)
    if has_audio is True:
        return record
    if has_audio is None:
        record.status = "failed"
        record.note = "separate audio track was detected but ffprobe could not confirm whether the video already has audio"
        return record

    audio_headers = safe_forward_headers(audio_candidate.request_headers, include_authorization=True)
    audio_record = download_direct(
        session,
        audio_candidate.url,
        out_dir,
        headers=audio_headers,
        max_video_mb=args.max_video_mb,
        timeout=min(args.timeout, 180),
    )
    if audio_record.status != "ok" or not audio_record.files:
        record.status = "failed"
        record.note = f"separate audio track was detected but could not be downloaded: {audio_record.note}"
        return record

    audio_path = Path(audio_record.files[0])
    ok, note, merged_path = mux_video_audio(video_path, audio_path, timeout=min(args.timeout, 900))
    if not ok or merged_path is None:
        record.status = "failed"
        record.note = note
        return record

    video_path.unlink(missing_ok=True)
    audio_path.unlink(missing_ok=True)
    record.files = [str(merged_path)]
    record.bytes = merged_path.stat().st_size
    record.strategy += "+separate-audio-mux"
    record.note = note
    return record



def _merge_formats(*groups: list[MediaFormat]) -> list[MediaFormat]:
    by_height: dict[tuple[int, str], MediaFormat] = {}
    no_height: list[MediaFormat] = []
    source_priority = {"yt-dlp": 4, "hls": 3, "dash": 3, "browser": 2, "direct": 1}
    for group in groups:
        for item in group:
            if item.height > 0:
                key = (item.height, item.label)
                current = by_height.get(key)
                if current is None or source_priority.get(item.source, 0) > source_priority.get(current.source, 0):
                    by_height[key] = item
            else:
                no_height.append(item)
    return sorted(by_height.values(), key=lambda value: (value.height, value.width, value.bitrate)) + no_height


def _formats_from_candidates(
    session: requests.Session,
    candidates: list[MediaCandidate],
    notes: list[str],
) -> list[MediaFormat]:
    groups: list[list[MediaFormat]] = []
    for item in candidates:
        if item.drm_status == "protected":
            notes.append(f"format probe skipped DRM-protected candidate: {item.url}")
            continue
        if item.kind == "stream":
            formats, note = inspect_manifest_formats(session, item)
            notes.append(note)
            if formats:
                groups.append(formats)
            continue
        if item.kind == "direct":
            try:
                width = int(item.metadata.get("width") or 0)
                height = int(item.metadata.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
            if height:
                groups.append([MediaFormat(
                    source="browser" if item.source.startswith("browser-") else "direct",
                    label=quality_label(height, width),
                    width=width,
                    height=height,
                    container="direct",
                    url=item.url,
                    note="resolution observed from the page media element",
                )])
    return _merge_formats(*groups) if groups else []


def formats_url(args: argparse.Namespace) -> int:
    url = clean_url(args.url)
    out_root = Path(args.out_root).expanduser()
    out_dir = unique_run_dir(out_root, args.title or "formats", dt.date.today().isoformat())
    notes: list[str] = []
    metadata: dict[str, Any] = {}
    try:
        formats, info, note = probe_ytdlp_formats(
            url,
            cookies_file=args.cookies_file,
            timeout=min(args.timeout, 180),
        )
        notes.append(note)
        metadata = info or {}

        # Mirror the autonomous download fallback chain for format discovery.
        if (
            not formats
            and _is_youtube_url(url)
            and _youtube_pot_enabled(args)
            and pot_worthy_error(note)
        ):
            try:
                with youtube_pot_session(setup_timeout=_youtube_pot_setup_timeout(args)) as pot:
                    notes.append(f"YouTube PO-token provider: {pot.provider}; {pot.note}")
                    formats, info_pot, pot_note = probe_ytdlp_formats(
                        url,
                        cookies_file=args.cookies_file,
                        extractor_args=pot.extractor_args,
                        prefer_python_module=True,
                        timeout=min(args.timeout, 180),
                    )
                    notes.append(f"YouTube mweb + PO-token format probe: {pot_note}")
                    if info_pot:
                        metadata = info_pot
            except Exception as exc:  # noqa: BLE001
                notes.append(f"YouTube PO-token provider unavailable: {str(exc)[:220]}")

        if not formats and not getattr(args, "no_impersonation_retry", False):
            formats, info_retry, retry_note = probe_ytdlp_formats(
                url,
                cookies_file=args.cookies_file,
                impersonate="chrome",
                timeout=min(args.timeout, 180),
            )
            notes.append(f"yt-dlp impersonation format probe: {retry_note}")
            if info_retry:
                metadata = info_retry
            if not formats and _is_youtube_url(url):
                formats, info_yt, yt_note = probe_ytdlp_formats(
                    url,
                    cookies_file=args.cookies_file,
                    impersonate="chrome",
                    extractor_args="youtube:player_client=web_safari",
                    timeout=min(args.timeout, 180),
                )
                notes.append(f"YouTube web_safari format probe: {yt_note}")
                if info_yt:
                    metadata = info_yt

        if not formats:
            session = _session(args.cookies_file)
            static_candidates, static_note = resolve_static(session, url)
            notes.append(static_note)
            _probe_drm(session, static_candidates)
            formats = _formats_from_candidates(session, static_candidates, notes)

        formats = _merge_formats(formats)
        md_path, json_path = write_formats_report(out_dir, url, formats, notes, metadata)
        print(out_dir)
        print(md_path)
        print(json_path)
        if not formats:
            print("No downloadable resolution list could be determined.")
            return 1
        for index, item in enumerate(formats, 1):
            resolution = f"{item.width}x{item.height}" if item.width and item.height else item.label
            size = f" ~{item.display_size}" if item.display_size else ""
            codec = f" {item.video_codec}" if item.video_codec else ""
            print(f"{index:02d}. {item.label:12s} {resolution:12s}{codec}{size}")
        return 0
    except Exception:
        raise


def inspect_url(args: argparse.Namespace) -> int:
    url = clean_url(args.url)
    out_root = Path(args.out_root).expanduser()
    out_dir = unique_run_dir(out_root, args.title or "inspect", dt.date.today().isoformat())
    notes: list[str] = []
    try:
        ytdlp_candidate, ytdlp_note = probe_ytdlp(
            url, cookies_file=args.cookies_file
        )
        notes.append(ytdlp_note)

        session = _session(args.cookies_file)
        static_candidates, static_note = resolve_static(session, url)
        notes.append(static_note)
        groups: list[list[MediaCandidate]] = [static_candidates]
        if ytdlp_candidate:
            groups.append([ytdlp_candidate])

        candidates = _merge_candidates(*groups)
        _probe_drm(session, candidates)
        md_path, json_path = write_inspect_report(out_dir, url, candidates, notes)
        print(out_dir)
        print(md_path)
        print(json_path)
        for index, item in enumerate(candidates[:12], 1):
            drm = ""
            if item.drm_status == "protected":
                drm = f" DRM=protected:{item.drm_note}"
            elif item.drm_status == "unknown":
                drm = f" DRM=unknown:{item.drm_note}"
            elif item.drm_status == "clear":
                drm = " DRM=clear"
            print(f"{index:02d}. score={item.score:3d} kind={item.kind:8s} {item.url}{drm}")
        return 0
    except Exception:
        raise


def _verify_record(record: DownloadRecord) -> DownloadRecord:
    if record.status != "ok" or not record.files:
        return record
    states: list[bool | None] = []
    notes: list[str] = []
    for raw in record.files:
        state, note, payload = verify_video(Path(raw))
        states.append(state)
        notes.append(f"{Path(raw).name}: {note}")
        if payload:
            record.metadata.setdefault("verification", {})[Path(raw).name] = {
                "format": (payload.get("format") or {}).get("format_name"),
                "duration": (payload.get("format") or {}).get("duration"),
            }
    if any(state is False for state in states):
        record.verified = False
        record.status = "failed"
    elif all(state is True for state in states):
        record.verified = True
    else:
        record.verified = None
    record.verification_note = " | ".join(notes)
    return record


def _download_candidate(
    source_url: str,
    candidate: MediaCandidate,
    out_dir: Path,
    session: requests.Session,
    args: argparse.Namespace,
    *,
    cookies_file: str = "",
) -> DownloadRecord:
    if candidate.drm_status == "protected" or candidate.drm:
        return DownloadRecord(
            source_url=source_url,
            strategy="drm-blocked",
            status="failed",
            selected_media_url=candidate.url,
            note=f"Protected stream detected: {candidate.drm_note}; video-catcher does not bypass DRM",
        )
    headers = safe_forward_headers(candidate.request_headers, include_authorization=True)
    if candidate.kind == "direct":
        record = download_direct(
            session, candidate.url, out_dir, headers=headers, max_video_mb=args.max_video_mb, timeout=min(args.timeout, 180)
        )
        record.source_url = source_url
        record.strategy = "sniffed-direct-http" if candidate.source == "browser-response" else "discovered-direct-http"
        record.selected_media_url = candidate.url
        return record
    if candidate.kind == "stream":
        record = download_ytdlp(
            candidate.url,
            out_dir,
            quality=args.quality,
            quality_mode=getattr(args, "quality_mode", "at-most"),
            format_id=getattr(args, "format_id", ""),
            max_video_mb=args.max_video_mb,
            cookies_file=cookies_file or args.cookies_file,
            playlist=False,
            subtitles=args.subtitles,
            sub_langs=args.sub_langs,
            embed_thumbnail=args.embed_thumbnail,
            download_archive=args.download_archive,
            headers=headers,
            timeout=args.timeout,
        )
        if record.status != "ok":
            ffmpeg_record = download_stream_ffmpeg(
                session,
                candidate.url,
                out_dir,
                headers=headers,
                quality=args.quality,
                quality_mode=getattr(args, "quality_mode", "at-most"),
                max_video_mb=args.max_video_mb,
                timeout=min(args.timeout, 1800),
            )
            if ffmpeg_record.status == "ok":
                record = ffmpeg_record
            else:
                record.note = f"{record.note}; ffmpeg fallback: {ffmpeg_record.note}".strip("; ")
        record.source_url = source_url
        if record.strategy == "ffmpeg-stream":
            record.strategy = "sniffed-stream/ffmpeg" if candidate.source == "browser-response" else "discovered-stream/ffmpeg"
        else:
            record.strategy = "sniffed-stream/yt-dlp" if candidate.source == "browser-response" else "discovered-stream/yt-dlp"
        record.selected_media_url = candidate.url
        return record
    return DownloadRecord(
        source_url=source_url,
        strategy="candidate-skip",
        note=f"Unsupported candidate kind: {candidate.kind}",
        selected_media_url=candidate.url,
    )


def _try_candidates(
    source_url: str,
    candidates: list[MediaCandidate],
    out_dir: Path,
    session: requests.Session,
    args: argparse.Namespace,
    notes: list[str],
    *,
    label: str,
    cookies_file: str = "",
) -> DownloadRecord | None:
    for candidate in candidates:
        if candidate.score < args.min_score or candidate.kind not in {"direct", "stream"}:
            continue
        attempt = _download_candidate(
            source_url,
            candidate,
            out_dir,
            session,
            args,
            cookies_file=cookies_file,
        )
        if attempt.status == "ok" and candidate.kind == "direct":
            attempt = _attach_separate_audio_if_needed(
                attempt, candidate, candidates, session, out_dir, args
            )
        notes.append(f"{label} candidate score={candidate.score}: {attempt.status} {attempt.note}".strip())
        if attempt.status == "ok":
            return _verify_record(attempt)
    return None


def download_one(url: str, out_dir: Path, args: argparse.Namespace) -> tuple[DownloadRecord, list[str]]:
    url = clean_url(url)
    notes: list[str] = []
    session = _session(args.cookies_file)
    kind = url_kind(url)

    # 1) Explicit direct URL.
    if kind == "direct":
        record = download_direct(
            session,
            url,
            out_dir,
            max_video_mb=args.max_video_mb,
            timeout=min(args.timeout, 180),
        )
        return _verify_record(record), notes

    # 2) Explicit stream URL: probe DRM before yt-dlp.
    if kind == "stream":
        candidate = MediaCandidate(url=url, source="input", kind="stream", request_headers={"Referer": url})
        drm_status, drm_note = detect_manifest_drm(session, candidate)
        candidate.drm_status = drm_status
        candidate.drm = drm_status == "protected"
        candidate.drm_note = drm_note
        record = _download_candidate(url, candidate, out_dir, session, args)
        return _verify_record(record), notes

    # 3) Known platform/extractor route.
    record = download_ytdlp(
        url,
        out_dir,
        quality=args.quality,
        quality_mode=getattr(args, "quality_mode", "at-most"),
        format_id=getattr(args, "format_id", ""),
        max_video_mb=args.max_video_mb,
        cookies_file=args.cookies_file,
        playlist=args.playlist,
        subtitles=args.subtitles,
        sub_langs=args.sub_langs,
        embed_thumbnail=args.embed_thumbnail,
        download_archive=args.download_archive,
        timeout=args.timeout,
    )
    notes.append(f"yt-dlp: {record.status} {record.note}".strip())
    if record.status in {"ok", "skipped"}:
        return _verify_record(record), notes

    # 3.0) Lux fallback for mainland-China platforms (non-Bilibili).
    # Lux parses many CN sites (Douyin, Kuaishou, iQiyi, Tencent, Youku, Xigua...)
    # more reliably than yt-dlp. Bilibili stays on yt-dlp.
    if _is_lux_candidate(url) and not getattr(args, "no_lux", False):
        lux_record = download_lux(url, out_dir, cookies_file=args.cookies_file)
        notes.append(f"lux: {lux_record.status} {lux_record.note}".strip())
        if lux_record.status in {"ok", "skipped"}:
            return _verify_record(lux_record), notes
        record = lux_record

    # 3a) YouTube-specific PO-token route. This is attempted before generic browser
    # impersonation/sniffing because current YouTube enforcement may reject a request
    # before a usable media URL is ever exposed.
    if (
        _is_youtube_url(url)
        and _youtube_pot_enabled(args)
        and pot_worthy_error(record.note)
    ):
        try:
            with youtube_pot_session(setup_timeout=_youtube_pot_setup_timeout(args)) as pot:
                notes.append(f"YouTube PO-token provider: {pot.provider}; {pot.note}")
                pot_retry = download_ytdlp(
                    url,
                    out_dir,
                    quality=args.quality,
                    quality_mode=getattr(args, "quality_mode", "at-most"),
                    format_id=getattr(args, "format_id", ""),
                    max_video_mb=args.max_video_mb,
                    cookies_file=args.cookies_file,
                    playlist=args.playlist,
                    subtitles=args.subtitles,
                    sub_langs=args.sub_langs,
                    embed_thumbnail=args.embed_thumbnail,
                    download_archive=args.download_archive,
                    extractor_args=pot.extractor_args,
                    prefer_python_module=True,
                    timeout=args.timeout,
                )
                notes.append(
                    f"YouTube mweb + PO-token retry: {pot_retry.status} {pot_retry.note}".strip()
                )
                if pot_retry.status in {"ok", "skipped"}:
                    pot_retry.strategy = "yt-dlp+youtube-pot"
                    return _verify_record(pot_retry), notes
                record = pot_retry
        except Exception as exc:  # noqa: BLE001
            notes.append(f"YouTube PO-token provider unavailable: {str(exc)[:220]}")

    # 3b) Autonomous yt-dlp retries that do not depend on the user's live Chrome profile.
    # curl_cffi impersonation can help sites that gate requests by TLS/browser fingerprint.
    if not getattr(args, "no_impersonation_retry", False):
        retry = download_ytdlp(
            url,
            out_dir,
            quality=args.quality,
            quality_mode=getattr(args, "quality_mode", "at-most"),
            format_id=getattr(args, "format_id", ""),
            max_video_mb=args.max_video_mb,
            cookies_file=args.cookies_file,
            playlist=args.playlist,
            subtitles=args.subtitles,
            sub_langs=args.sub_langs,
            embed_thumbnail=args.embed_thumbnail,
            download_archive=args.download_archive,
            impersonate="chrome",
            timeout=args.timeout,
        )
        notes.append(f"yt-dlp impersonation retry: {retry.status} {retry.note}".strip())
        if retry.status in {"ok", "skipped"}:
            retry.strategy = "yt-dlp+impersonate"
            return _verify_record(retry), notes
        record = retry

        # YouTube currently recommends client/token-aware fallbacks. web_safari may expose HLS
        # formats that avoid some GVS PO-token requirements, so try it before browser sniffing.
        if _is_youtube_url(url):
            youtube_retry = download_ytdlp(
                url,
                out_dir,
                quality=args.quality,
                quality_mode=getattr(args, "quality_mode", "at-most"),
                format_id=getattr(args, "format_id", ""),
                max_video_mb=args.max_video_mb,
                cookies_file=args.cookies_file,
                playlist=args.playlist,
                subtitles=args.subtitles,
                sub_langs=args.sub_langs,
                embed_thumbnail=args.embed_thumbnail,
                download_archive=args.download_archive,
                impersonate="chrome",
                extractor_args="youtube:player_client=web_safari",
                timeout=args.timeout,
            )
            notes.append(f"YouTube web_safari retry: {youtube_retry.status} {youtube_retry.note}".strip())
            if youtube_retry.status in {"ok", "skipped"}:
                youtube_retry.strategy = "yt-dlp+web-safari"
                return _verify_record(youtube_retry), notes
            record = youtube_retry

    # 4) Static HTML discovery (no browser involved).
    fallback_session = _session(args.cookies_file)
    static_candidates, static_note = resolve_static(fallback_session, url)
    notes.append(static_note)
    _probe_drm(fallback_session, static_candidates)
    static_result = _try_candidates(
        url,
        static_candidates,
        out_dir,
        fallback_session,
        args,
        notes,
        label="static",
        cookies_file=args.cookies_file,
    )
    if static_result is not None:
        return static_result, notes
    record.strategy = "fallback-exhausted"
    record.note = f"{record.note}; no media candidates found by static scan"
    return record, notes

def _read_url_file(path: str) -> list[str]:
    urls = []
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        urls.append(line)
    return urls


def download_command(args: argparse.Namespace) -> int:
    urls = list(args.urls)
    for path in args.url_file:
        urls.extend(_read_url_file(path))
    clean: list[str] = []
    seen: set[str] = set()
    for value in urls:
        url = clean_url(value)
        if url not in seen:
            clean.append(url)
            seen.add(url)
    if not clean:
        raise SystemExit("No valid URLs provided")

    out_root = Path(args.out_root).expanduser()
    if args.run_dir:
        out_dir = Path(args.run_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = unique_run_dir(out_root, args.title or "video-download", dt.date.today().isoformat())

    task_config = {
        "urls": clean,
        "title": args.title,
        "quality": args.quality,
        "quality_mode": getattr(args, "quality_mode", "at-most"),
        "format_id": getattr(args, "format_id", ""),
        "max_video_mb": args.max_video_mb,
        "playlist": args.playlist,
        "subtitles": args.subtitles,
        "sub_langs": args.sub_langs,
        "embed_thumbnail": args.embed_thumbnail,
        "download_archive": args.download_archive,
        "cookies_file": args.cookies_file,
        "min_score": args.min_score,
        "timeout": args.timeout,
        "no_impersonation_retry": bool(getattr(args, "no_impersonation_retry", False)),
        "no_youtube_pot": bool(getattr(args, "no_youtube_pot", False)),
        "jobs": max(1, getattr(args, "jobs", 1) or 1),
        "no_dedupe": bool(getattr(args, "no_dedupe", False)),
        "force": bool(getattr(args, "force", False)),
        "history": str(Path(getattr(args, "history", DEFAULT_DOWNLOAD_HISTORY)).expanduser()),
    }
    write_task_state(out_dir, task_config)
    records: list[DownloadRecord] = []
    notes: list[str] = []
    skipped: list[str] = []

    # —— 历史去重 ——
    history_path = Path(getattr(args, "history", DEFAULT_DOWNLOAD_HISTORY)).expanduser()
    history = _load_history(history_path) if not args.no_dedupe else {}
    if not args.no_dedupe:
        pending: list[str] = []
        for url in clean:
            prior = history.get(url)
            if prior and not args.force:
                skipped.append(url)
                records.append(DownloadRecord(
                    source_url=url,
                    strategy="history-skip",
                    status="skipped",
                    note=(
                        f"already downloaded on {prior.get('date', '?')} "
                        f"via {prior.get('strategy', '?')}; use --force to download again"
                    ),
                ))
            else:
                pending.append(url)
        clean = pending

    jobs = max(1, getattr(args, "jobs", 1) or 1)
    if jobs > 1 and len(clean) > 1:
        results: list[tuple[int, DownloadRecord, list[str]]] = [None] * len(clean)  # type: ignore[list-item]
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="vc") as pool:
            futures = {pool.submit(download_one, url, out_dir, args): (index, url) for index, url in enumerate(clean)}
            for future in concurrent.futures.as_completed(futures):
                index, url = futures[future]
                try:
                    record, item_notes = future.result()
                except Exception as exc:  # noqa: BLE001
                    record = DownloadRecord(source_url=url, strategy="error", status="failed", note=str(exc)[:220])
                    item_notes = [f"{index}: exception {exc}"]
                results[index] = (index, record, item_notes)
        for index, record, item_notes in results:
            records.append(record)
            notes.extend(f"{index + 1}: {note}" for note in item_notes)
            print(f"[{index + 1}/{len(results)}] {record.source_url}\n  -> {record.status} via {record.strategy}: {record.note}", flush=True)
    else:
        for index, url in enumerate(clean, 1):
            print(f"[{index}/{len(clean)}] {url}", flush=True)
            record, item_notes = download_one(url, out_dir, args)
            records.append(record)
            notes.extend(f"{index}: {note}" for note in item_notes)
            print(f"  -> {record.status} via {record.strategy}: {record.note}", flush=True)

    # —— 记录历史（仅成功项）——
    if not args.no_dedupe:
        for record in records:
            if record.status == "ok":
                history[record.source_url] = {
                    "date": dt.date.today().isoformat(),
                    "strategy": record.strategy,
                    "note": (record.note or "")[:220],
                }
        _save_history(history_path, history)

    md_path, json_path = write_download_report(out_dir, records, notes)
    print(out_dir)
    print(md_path)
    print(json_path)
    if skipped:
        print(f"[vc] skipped {len(skipped)} already-downloaded URL(s); use --force to re-download")
    ok = sum(1 for item in records if item.status in {"ok", "skipped"})
    return 0 if ok == len(records) else (2 if ok else 1)


def resume_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser()
    task_path = run_dir / "task.json"
    if not task_path.is_file():
        raise SystemExit(f"task.json not found in {run_dir}")
    config = json.loads(task_path.read_text(encoding="utf-8"))
    forwarded = argparse.Namespace(
        urls=config.get("urls") or [],
        url_file=[],
        out_root=str(run_dir.parent),
        run_dir=str(run_dir),
        title=config.get("title") or "resume",
        quality=args.quality or config.get("quality") or "1080",
        quality_mode=getattr(args, "quality_mode", "") or config.get("quality_mode") or "at-most",
        format_id=getattr(args, "format_id", "") or config.get("format_id") or "",
        max_video_mb=args.max_video_mb or config.get("max_video_mb") or 2048,
        cookies_file=args.cookies_file or config.get("cookies_file") or "",
        playlist=bool(config.get("playlist")),
        subtitles=bool(config.get("subtitles")),
        sub_langs=config.get("sub_langs") or "zh.*,en.*",
        embed_thumbnail=bool(config.get("embed_thumbnail")),
        download_archive=config.get("download_archive") or "",
        min_score=args.min_score or config.get("min_score") or 35,
        timeout=args.timeout or config.get("timeout") or 3600,
        no_impersonation_retry=bool(config.get("no_impersonation_retry")),
        no_youtube_pot=(
            bool(config.get("no_youtube_pot"))
            if getattr(args, "no_youtube_pot", None) is None
            else bool(args.no_youtube_pot)
        ),
        jobs=max(1, config.get("jobs", 1) or 1),
        no_dedupe=(
            bool(config.get("no_dedupe"))
            if getattr(args, "no_dedupe", None) is None
            else bool(args.no_dedupe)
        ),
        force=bool(getattr(args, "force", False)),
        history=config.get("history") or str(DEFAULT_DOWNLOAD_HISTORY),
    )
    return download_command(forwarded)


def doctor_command(_args: argparse.Namespace) -> int:
    pot_ok, pot_note = pot_prerequisites()
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "requests": python_module_available("requests"),
        "yt-dlp": bool(ytdlp_command()),
        "ffmpeg": bool(find_executable("ffmpeg")),
        "ffprobe": bool(find_executable("ffprobe")),
        "curl-cffi": python_module_available("curl_cffi"),
        "youtube-pot-plugin": bgutil_plugin_version(),
        "youtube-pot-capable": pot_ok,
        "youtube-pot-note": pot_note,
        "youtube-pot-provider-cache": str(provider_server_dir()),
        "youtube-pot-provider-built": (provider_server_dir() / "build" / "main.js").is_file(),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    required = checks["requests"] and checks["yt-dlp"] and checks["ffmpeg"] and checks["ffprobe"]
    return 0 if required else 1





def _load_history(history_path: Path) -> dict:
    """Download history: url -> {date, strategy, note}."""
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_history(history_path: Path, history: dict) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8"
    )






def add_common_download_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out-root", default=str(default_out_root()))
    parser.add_argument("--title", default="")
    parser.add_argument("--quality", default="1080", help="height such as 1080p/4k, or best")
    parser.add_argument("--quality-mode", choices=["at-most", "exact"], default="at-most", help="at-most caps the height; exact requires the requested height")
    parser.add_argument("--format-id", default="", help="advanced: exact yt-dlp video format id; audio is merged automatically when available")
    parser.add_argument("--max-video-mb", type=float, default=2048.0)
    parser.add_argument("--cookies-file", default="")
    parser.add_argument("--playlist", action="store_true")
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument("--sub-langs", default="zh.*,en.*")
    parser.add_argument("--embed-thumbnail", action="store_true")
    parser.add_argument("--download-archive", default="")
    parser.add_argument("--no-impersonation-retry", action="store_true", help="disable yt-dlp curl_cffi/browser impersonation retry")
    parser.add_argument("--no-youtube-pot", action="store_true", help="disable automatic YouTube PO-token provider fallback")
    parser.add_argument("--no-lux", action="store_true", help="disable Lux fallback for mainland-China platforms")
    parser.add_argument("--min-score", type=int, default=35)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--jobs", "-j", type=int, default=1, help="concurrent downloads (default 1 = serial)")
    parser.add_argument("--no-dedupe", action="store_true", help="disable download-history dedupe")
    parser.add_argument("--force", action="store_true", help="re-download URLs even if present in history")
    parser.add_argument("--history", default=str(DEFAULT_DOWNLOAD_HISTORY), help="download-history JSON for dedupe")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent-friendly video discovery and downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="discover and download videos")
    download.add_argument("urls", nargs="*")
    download.add_argument("--url-file", action="append", default=[])
    download.add_argument("--run-dir", default="")
    add_common_download_args(download)
    download.set_defaults(func=download_command)

    formats = sub.add_parser("formats", help="list downloadable video resolutions without downloading")
    formats.add_argument("url")
    formats.add_argument("--out-root", default=str(default_out_root()))
    formats.add_argument("--title", default="formats")
    formats.add_argument("--cookies-file", default="")
    formats.add_argument("--no-impersonation-retry", action="store_true")
    formats.add_argument("--no-youtube-pot", action="store_true")
    formats.add_argument("--timeout", type=int, default=3600)
    formats.set_defaults(func=formats_url)

    inspect = sub.add_parser("inspect", help="discover media without downloading")
    inspect.add_argument("url")
    inspect.add_argument("--out-root", default=str(default_out_root()))
    inspect.add_argument("--title", default="inspect")
    inspect.add_argument("--cookies-file", default="")
    inspect.set_defaults(func=inspect_url)

    resume = sub.add_parser("resume", help="resume a previous task directory")
    resume.add_argument("run_dir")
    resume.add_argument("--quality", default="")
    resume.add_argument("--quality-mode", choices=["at-most", "exact"], default="")
    resume.add_argument("--format-id", default="")
    resume.add_argument("--max-video-mb", type=float, default=0)
    resume.add_argument("--cookies-file", default="")
    resume.add_argument("--min-score", type=int, default=0)
    resume.add_argument("--timeout", type=int, default=0)
    resume.add_argument("--no-dedupe", dest="no_dedupe", action="store_true", default=None, help="disable dedupe on resume")
    resume.add_argument("--force", action="store_true", default=False, help="re-download even if present in history")
    pot_group = resume.add_mutually_exclusive_group()
    pot_group.add_argument("--no-youtube-pot", dest="no_youtube_pot", action="store_true", default=None)
    pot_group.add_argument("--youtube-pot", dest="no_youtube_pot", action="store_false")
    resume.set_defaults(func=resume_command)

    article = sub.add_parser(
        "article",
        help="微信公众号文章离线保存（正文+图片 -> 本地 HTML/Markdown，无需额外 Skill）",
    )
    article.add_argument("url", help="mp.weixin.qq.com 文章链接")
    article.add_argument("--out-root", default="", help="输出根目录（默认 WECHAT_ARTICLE_OUT 或 ~/Documents）")
    article.add_argument("--title", default="", help="覆盖标题（用于目录名）")
    article.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认60）")
    article.add_argument("--no-images", action="store_true", help="不下载图片，保留远程链接")
    article.set_defaults(func=article_archiver.article_command)

    doctor = sub.add_parser("doctor", help="check local dependencies")
    doctor.set_defaults(func=doctor_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "max_video_mb") and args.max_video_mb < 0:
        parser.error("--max-video-mb must be non-negative")
    if hasattr(args, "quality") and args.quality:
        try:
            format_selector(args.quality, getattr(args, "quality_mode", "at-most") or "at-most", getattr(args, "format_id", ""))
        except ValueError as exc:
            parser.error(str(exc))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
