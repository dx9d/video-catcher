# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from models import MediaCandidate

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".flv", ".ogv"}
STREAM_EXTENSIONS = {".m3u8", ".mpd"}
AUDIO_EXTENSIONS = {".m4a", ".aac", ".mp3", ".opus", ".ogg", ".wav"}
MEDIA_MIME_PREFIXES = ("video/", "audio/")
STREAM_MIMES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "application/dash+xml",
}
NEGATIVE_TOKENS = ("advert", "/ads/", "doubleclick", "promo", "sprite", "thumbnail", "thumb", "avatar", "logo")
POSITIVE_TOKENS = ("master", "manifest", "playlist", "video", "stream", "playback")


def url_kind(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    lowered = (url or "").lower()
    if suffix in VIDEO_EXTENSIONS:
        return "direct"
    if suffix in STREAM_EXTENSIONS or mime in STREAM_MIMES:
        return "stream"
    if suffix in AUDIO_EXTENSIONS or mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "direct"

    # Some large platforms use extensionless CDN URLs. Browser network metadata and
    # query hints are enough to classify these without hard-coding an extractor.
    if "mime=audio" in lowered or "mime%3daudio" in lowered or "/audio/tos/" in lowered:
        return "audio"
    if "mime=video" in lowered or "mime%3dvideo" in lowered:
        return "direct"
    if "videoplayback" in lowered or "/video/tos/" in lowered or "playwm" in lowered:
        return "direct"
    return "unknown"


def looks_like_media(url: str, content_type: str = "") -> bool:
    kind = url_kind(url, content_type)
    if kind != "unknown":
        return True
    lowered = (content_type or "").lower()
    return any(lowered.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES)


def score_candidate(candidate: MediaCandidate) -> MediaCandidate:
    if candidate.source == "yt-dlp" or candidate.kind == "platform":
        candidate.kind = "platform"
        candidate.score = max(candidate.score, 100)
        if "yt-dlp extractor recognized URL" not in candidate.reasons:
            candidate.reasons.append("yt-dlp extractor recognized URL")
        return candidate
    score = 0
    reasons: list[str] = []
    kind = url_kind(candidate.url, candidate.content_type)
    candidate.kind = kind
    lowered_url = candidate.url.lower()
    mime = (candidate.content_type or "").split(";", 1)[0].lower()

    if kind == "stream":
        score += 45
        reasons.append("stream manifest")
    elif kind == "direct":
        score += 38
        reasons.append("video resource")
    elif kind == "audio":
        score += 8
        reasons.append("audio-only resource")

    if mime.startswith("video/"):
        score += 22
        reasons.append("video MIME")
    elif mime in STREAM_MIMES:
        score += 22
        reasons.append("stream MIME")

    if candidate.content_length >= 50 * 1024 * 1024:
        score += 18
        reasons.append("large media payload")
    elif candidate.content_length >= 5 * 1024 * 1024:
        score += 12
        reasons.append("media-sized payload")
    elif 0 < candidate.content_length < 512 * 1024:
        score -= 12
        reasons.append("very small payload")

    if any(token in lowered_url for token in POSITIVE_TOKENS):
        score += 8
        reasons.append("media URL hint")
    if "master.m3u8" in lowered_url or "manifest.mpd" in lowered_url:
        score += 12
        reasons.append("master manifest hint")
    if any(token in lowered_url for token in NEGATIVE_TOKENS):
        score -= 30
        reasons.append("advert/preview asset hint")
    if candidate.source == "browser-response":
        score += 8
        reasons.append("observed in browser network")
    if candidate.metadata.get("resource_type") == "media":
        score += 12
        reasons.append("browser resource type=media")
    if candidate.status and candidate.status >= 400:
        score -= 25
        reasons.append(f"HTTP {candidate.status}")

    candidate.score = max(-100, min(score, 100))
    candidate.reasons = reasons
    return candidate


def rank_candidates(candidates: list[MediaCandidate]) -> list[MediaCandidate]:
    by_url: dict[str, MediaCandidate] = {}
    for item in candidates:
        item = score_candidate(item)
        existing = by_url.get(item.url)
        if existing is None or item.score > existing.score:
            by_url[item.url] = item
        elif existing:
            if not existing.request_headers and item.request_headers:
                existing.request_headers = item.request_headers
            if not existing.response_headers and item.response_headers:
                existing.response_headers = item.response_headers
            if item.content_length > existing.content_length:
                existing.content_length = item.content_length
            # Network responses usually score higher, while DOM observations may carry
            # videoWidth/videoHeight. Preserve complementary metadata when deduplicating.
            for key, value in (item.metadata or {}).items():
                if value not in (None, "", 0, False) and not existing.metadata.get(key):
                    existing.metadata[key] = value
    return sorted(by_url.values(), key=lambda x: (x.drm_status == "protected", -x.score, -x.content_length, x.url))


def detect_drm_text(text: str, kind: str) -> tuple[bool, str]:
    lowered = (text or "").lower()
    if kind == "stream-hls":
        if "method=sample-aes" in lowered or "sample-aes-ctr" in lowered:
            return True, "HLS SAMPLE-AES encryption detected"
        keyformat_matches = re.findall(r"keyformat=\"?([^,\"\n]+)", lowered)
        if any(value not in {"identity", ""} for value in keyformat_matches):
            return True, "HLS DRM KEYFORMAT detected"
        if "com.apple.streamingkeydelivery" in lowered:
            return True, "FairPlay marker detected"
        return False, ""
    if kind == "stream-dash":
        markers = {
            "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
            "9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
            "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
        }
        for marker, name in markers.items():
            if marker in lowered:
                return True, f"{name} ContentProtection detected"
        if "cenc:pssh" in lowered or "<pssh" in lowered:
            return True, "CENC PSSH protection detected"
    return False, ""


def _read_limited_response(response: requests.Response, limit: int = 192 * 1024) -> str:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(16 * 1024):
        if not chunk:
            continue
        remain = limit - total
        chunks.append(chunk[:remain])
        total += len(chunks[-1])
        if total >= limit:
            break
    return b"".join(chunks).decode("utf-8", errors="ignore")


def detect_manifest_drm(
    session: requests.Session,
    candidate: MediaCandidate,
    *,
    timeout: int = 20,
) -> tuple[str, str]:
    parsed = urlparse(candidate.url)
    suffix = Path(parsed.path).suffix.lower()
    mime = candidate.content_type.lower()
    if suffix not in STREAM_EXTENSIONS and "mpegurl" not in mime and "dash+xml" not in mime:
        return "not-checked", ""
    headers = dict(candidate.request_headers or {})
    headers.pop("cookie", None)
    headers.pop("Cookie", None)
    try:
        response = session.get(candidate.url, headers=headers, timeout=timeout, stream=True)
        response.raise_for_status()
        text = _read_limited_response(response)
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"DRM probe could not complete: {str(exc)[:120]}"
    if suffix == ".mpd" or "dash+xml" in mime:
        drm, note = detect_drm_text(text, "stream-dash")
    else:
        drm, note = detect_drm_text(text, "stream-hls")
    return ("protected", note) if drm else ("clear", "No supported DRM marker detected")
