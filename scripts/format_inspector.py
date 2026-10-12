# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from common import human_size
from models import MediaCandidate


@dataclass
class MediaFormat:
    source: str
    label: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    bitrate: int = 0
    filesize: int = 0
    format_id: str = ""
    selector: str = ""
    url: str = ""
    note: str = ""

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def display_size(self) -> str:
        return human_size(self.filesize) if self.filesize else ""


def quality_label(height: int, width: int = 0) -> str:
    if height >= 2160:
        return "2160p / 4K"
    if height >= 1440:
        return "1440p / 2K"
    if height > 0:
        return f"{height}p"
    if width > 0:
        return f"{width}px wide"
    return "未知"


def _parse_attr_list(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    # HLS attribute lists may contain quoted commas inside CODECS; parse key=value tokens.
    pattern = re.compile(r'(?:^|,)([A-Z0-9-]+)=("[^"]*"|[^,]*)', re.I)
    for match in pattern.finditer(text):
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        attrs[match.group(1).upper()] = value
    return attrs


def _split_codecs(codecs: str) -> tuple[str, str]:
    parts = [part.strip() for part in (codecs or "").split(",") if part.strip()]
    video = ""
    audio = ""
    video_prefixes = ("avc", "hvc", "hev", "vp", "av01", "theora")
    audio_prefixes = ("mp4a", "opus", "vorbis", "ac-3", "ec-3")
    for part in parts:
        lowered = part.lower()
        if not video and lowered.startswith(video_prefixes):
            video = part
        elif not audio and lowered.startswith(audio_prefixes):
            audio = part
    if not video and parts:
        video = parts[0]
    return video, audio


def parse_hls_formats(text: str, manifest_url: str) -> list[MediaFormat]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    formats: list[MediaFormat] = []
    for index, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attr_list(line.split(":", 1)[1])
        next_url = ""
        for follower in lines[index + 1:]:
            if follower.startswith("#"):
                continue
            next_url = urljoin(manifest_url, follower)
            break
        resolution = attrs.get("RESOLUTION", "")
        width = height = 0
        match = re.match(r"(\d+)x(\d+)", resolution, re.I)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
        try:
            bandwidth = int(float(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0))
        except ValueError:
            bandwidth = 0
        try:
            fps = float(attrs.get("FRAME-RATE") or 0)
        except ValueError:
            fps = 0.0
        video_codec, audio_codec = _split_codecs(attrs.get("CODECS", ""))
        formats.append(
            MediaFormat(
                source="hls",
                label=quality_label(height, width),
                width=width,
                height=height,
                fps=fps,
                video_codec=video_codec,
                audio_codec=audio_codec,
                container="HLS",
                bitrate=bandwidth,
                selector=f"height={height}" if height else "",
                url=next_url,
                note=attrs.get("NAME", ""),
            )
        )
    if not formats and "#EXTM3U" in text:
        formats.append(
            MediaFormat(
                source="hls",
                label="单一 HLS 流",
                container="HLS",
                url=manifest_url,
                note="media playlist，没有公开多档清晰度信息",
            )
        )
    return _dedupe_formats(formats)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_frame_rate(value: str) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.0
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            return float(left) / float(right)
        except (ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_mpd_formats(text: str, manifest_url: str) -> list[MediaFormat]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    formats: list[MediaFormat] = []
    for adaptation in root.iter():
        if _local_name(adaptation.tag) != "AdaptationSet":
            continue
        adaptation_mime = adaptation.attrib.get("mimeType", "")
        adaptation_codecs = adaptation.attrib.get("codecs", "")
        adaptation_width = int(adaptation.attrib.get("width") or 0)
        adaptation_height = int(adaptation.attrib.get("height") or 0)
        adaptation_fps = _parse_frame_rate(adaptation.attrib.get("frameRate", ""))
        for rep in adaptation:
            if _local_name(rep.tag) != "Representation":
                continue
            mime = rep.attrib.get("mimeType") or adaptation_mime
            if mime and not mime.startswith("video/"):
                continue
            width = int(rep.attrib.get("width") or adaptation_width or 0)
            height = int(rep.attrib.get("height") or adaptation_height or 0)
            try:
                bandwidth = int(rep.attrib.get("bandwidth") or 0)
            except ValueError:
                bandwidth = 0
            codecs = rep.attrib.get("codecs") or adaptation_codecs
            fps = _parse_frame_rate(rep.attrib.get("frameRate", "")) or adaptation_fps
            fmt_id = rep.attrib.get("id", "")
            formats.append(
                MediaFormat(
                    source="dash",
                    label=quality_label(height, width),
                    width=width,
                    height=height,
                    fps=fps,
                    video_codec=codecs,
                    container="DASH",
                    bitrate=bandwidth,
                    format_id=fmt_id,
                    selector=f"height={height}" if height else "",
                    url=manifest_url,
                )
            )
    return _dedupe_formats(formats)


def _format_filesize(item: dict[str, Any]) -> int:
    for key in ("filesize", "filesize_approx"):
        value = item.get(key)
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            pass
    return 0


def formats_from_ytdlp_info(info: dict[str, Any]) -> list[MediaFormat]:
    raw_formats = info.get("formats") or []
    by_height: dict[int, list[dict[str, Any]]] = {}
    for item in raw_formats:
        if not isinstance(item, dict):
            continue
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        vcodec = str(item.get("vcodec") or "")
        if height <= 0 or vcodec in {"", "none"}:
            continue
        by_height.setdefault(height, []).append(item)

    result: list[MediaFormat] = []
    for height in sorted(by_height):
        options = by_height[height]
        # Prefer a format with audio for a single-file estimate; otherwise prefer the largest/best video track.
        def key(item: dict[str, Any]) -> tuple[float, int, int, int]:
            # Match the intent of bv*: prefer the strongest video representation at this height.
            try:
                tbr = float(item.get("tbr") or 0)
            except (TypeError, ValueError):
                tbr = 0.0
            try:
                fps = int(float(item.get("fps") or 0))
            except (TypeError, ValueError):
                fps = 0
            acodec = str(item.get("acodec") or "")
            has_audio = 1 if acodec not in {"", "none"} else 0
            return tbr, fps, _format_filesize(item), has_audio

        best = max(options, key=key)
        try:
            width = int(best.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        try:
            fps = float(best.get("fps") or 0)
        except (TypeError, ValueError):
            fps = 0.0
        try:
            bitrate = int(float(best.get("tbr") or 0) * 1000)
        except (TypeError, ValueError):
            bitrate = 0
        acodec = str(best.get("acodec") or "")
        if acodec == "none":
            acodec = "best audio separately"
        result.append(
            MediaFormat(
                source="yt-dlp",
                label=quality_label(height, width),
                width=width,
                height=height,
                fps=fps,
                video_codec=str(best.get("vcodec") or ""),
                audio_codec=acodec,
                container=str(best.get("ext") or ""),
                bitrate=bitrate,
                filesize=_format_filesize(best),
                format_id=str(best.get("format_id") or ""),
                selector=f"bv*[height={height}]+ba/b[height={height}]",
                url=str(best.get("url") or ""),
                note=("video-track size only; best audio will be downloaded separately and merged" if acodec == "best audio separately" else "exact height selector"),
            )
        )
    return _dedupe_formats(result)


def inspect_manifest_formats(
    session: requests.Session,
    candidate: MediaCandidate,
    *,
    timeout: int = 25,
) -> tuple[list[MediaFormat], str]:
    headers = dict(candidate.request_headers or {})
    headers.pop("Cookie", None)
    headers.pop("cookie", None)
    try:
        response = session.get(candidate.url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return [], f"manifest format probe failed: {str(exc)[:160]}"
    text = response.text[:2_000_000]
    content_type = (candidate.content_type or response.headers.get("Content-Type") or "").lower()
    lowered_url = candidate.url.lower()
    if ".mpd" in lowered_url or "dash+xml" in content_type:
        formats = parse_mpd_formats(text, candidate.url)
        return formats, f"DASH manifest exposed {len(formats)} video resolution(s)"
    formats = parse_hls_formats(text, candidate.url)
    return formats, f"HLS manifest exposed {len(formats)} video resolution(s)"


def _dedupe_formats(formats: list[MediaFormat]) -> list[MediaFormat]:
    seen: set[tuple[Any, ...]] = set()
    out: list[MediaFormat] = []
    for item in sorted(formats, key=lambda value: (value.height or 0, value.width or 0, value.bitrate or 0)):
        key = (item.source, item.height, item.width, item.fps, item.video_codec, item.container)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
