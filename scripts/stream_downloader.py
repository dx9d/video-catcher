# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from common import disk_has_room, find_executable, safe_filename
from format_inspector import parse_hls_formats
from models import DownloadRecord
from ytdlp_downloader import normalize_quality


def _headers_blob(headers: dict[str, str] | None) -> str:
    # ffmpeg accepts CRLF separated HTTP headers. Authorization is intentionally
    # not forwarded here because ffmpeg exposes -headers in the OS process argv.
    allowed = {"referer", "origin", "user-agent", "accept", "accept-language"}
    rows: list[str] = []
    for key, value in (headers or {}).items():
        if key.lower() in allowed and value:
            rows.append(f"{key}: {value}")
    return "\r\n".join(rows) + ("\r\n" if rows else "")


def _select_hls_variant(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None,
    quality: str,
    quality_mode: str,
    timeout: int,
) -> tuple[str, str]:
    try:
        response = session.get(url, headers=headers or {}, timeout=min(timeout, 30))
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return url, f"HLS master probe unavailable: {str(exc)[:150]}"
    text = response.text[:2_000_000]
    formats = [item for item in parse_hls_formats(text, url) if item.height and item.url]
    if not formats:
        return url, "single HLS media playlist"
    normalized = normalize_quality(quality)
    if normalized == "best":
        selected = max(formats, key=lambda item: (item.height, item.bitrate))
        return selected.url, f"selected HLS {selected.label}"
    target = int(normalized)
    if quality_mode == "exact":
        exact = [item for item in formats if item.height == target]
        if not exact:
            return "", f"requested exact HLS height {target}p is not available"
        selected = max(exact, key=lambda item: item.bitrate)
    else:
        eligible = [item for item in formats if item.height <= target]
        if not eligible:
            selected = min(formats, key=lambda item: item.height)
        else:
            selected = max(eligible, key=lambda item: (item.height, item.bitrate))
    return selected.url, f"selected HLS {selected.label}"


def download_stream_ffmpeg(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    headers: dict[str, str] | None = None,
    quality: str = "1080",
    quality_mode: str = "at-most",
    max_video_mb: float = 2048,
    timeout: int = 3600,
) -> DownloadRecord:
    """Fallback downloader for clear HLS/DASH streams using ffmpeg.

    This path is deliberately independent of yt-dlp, so a discovered clear
    manifest can still be downloaded when an extractor is unavailable.
    """
    record = DownloadRecord(source_url=url, strategy="ffmpeg-stream", selected_media_url=url)
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        record.note = "ffmpeg not installed"
        return record
    if any(key.lower() == "authorization" for key in (headers or {})):
        record.note = "ffmpeg fallback skipped because Authorization would be exposed in process arguments"
        return record
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = int(max_video_mb * 1024 * 1024)
    if not disk_has_room(out_dir, limit):
        record.note = f"Not enough free disk space for {max_video_mb:g} MB ceiling plus safety reserve"
        return record

    selected_url = url
    note = ""
    lowered = url.lower()
    if ".m3u8" in lowered:
        selected_url, note = _select_hls_variant(
            session,
            url,
            headers=headers,
            quality=quality,
            quality_mode=quality_mode,
            timeout=timeout,
        )
        if not selected_url:
            record.note = note
            return record

    parsed = urlparse(selected_url)
    stem = safe_filename(Path(parsed.path).stem or "stream", fallback="stream", max_len=120)
    target = out_dir / f"{stem}.mp4"
    index = 2
    while target.exists() or Path(str(target) + ".part.mp4").exists():
        target = out_dir / f"{stem}-{index:02d}.mp4"
        index += 1
    partial = Path(str(target) + ".part.mp4")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    header_blob = _headers_blob(headers)
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += [
        "-i", selected_url,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+faststart",
        str(partial),
    ]
    start = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                proc.kill()
                proc.wait(timeout=5)
                record.note = f"ffmpeg stream download timed out after {timeout}s"
                return record
            if partial.exists() and partial.stat().st_size > limit:
                proc.kill()
                proc.wait(timeout=5)
                partial.unlink(missing_ok=True)
                record.note = f"stream exceeded {max_video_mb:g} MB ceiling"
                return record
            time.sleep(0.25)
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        if proc.returncode != 0 or not partial.is_file() or partial.stat().st_size <= 0:
            record.note = f"ffmpeg stream failed: {(stderr.splitlines()[-1] if stderr else 'exit ' + str(proc.returncode))[:240]}"
            return record
        partial.replace(target)
        record.status = "ok"
        record.files = [str(target)]
        record.bytes = target.stat().st_size
        record.note = note or "downloaded clear stream with ffmpeg"
        return record
    except Exception as exc:  # noqa: BLE001
        record.note = f"ffmpeg stream failed: {str(exc)[:220]}"
        return record
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if record.status != "ok":
            partial.unlink(missing_ok=True)
