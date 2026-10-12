# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from common import DEFAULT_USER_AGENT, disk_has_room, safe_filename
from models import DownloadRecord


def _filename_from_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return safe_filename(unquote(match.group(1)))
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return safe_filename(match.group(1))
    return ""


def _target_for(url: str, response: requests.Response, out_dir: Path) -> Path:
    parsed = urlparse(url)
    fallback = safe_filename(Path(parsed.path).name or "video.mp4")
    filename = _filename_from_headers(response.headers) or fallback
    if not Path(filename).suffix:
        content_type = response.headers.get("Content-Type", "").lower()
        filename += ".webm" if "webm" in content_type else ".mp4"
    target = out_dir / filename
    if not target.exists() or Path(str(target) + ".part").exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 1000):
        candidate = out_dir / f"{stem}-{index:02d}{suffix}"
        if not candidate.exists() and not Path(str(candidate) + ".part").exists():
            return candidate
    raise RuntimeError("Could not allocate output filename")


def download_direct(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    headers: dict[str, str] | None = None,
    max_video_mb: float = 2048,
    timeout: int = 120,
) -> DownloadRecord:
    record = DownloadRecord(source_url=url, strategy="direct-http", selected_media_url=url)
    out_dir.mkdir(parents=True, exist_ok=True)
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, **(headers or {})}
    partial: Path | None = None
    try:
        response = session.get(url, headers=request_headers, timeout=timeout, stream=True, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            record.note = f"Unexpected content type: {content_type.split(';', 1)[0]}"
            return record
        target = _target_for(response.url, response, out_dir)
        partial = Path(str(target) + ".part")
        limit = int(max_video_mb * 1024 * 1024)
        resume_from = partial.stat().st_size if partial.exists() else 0
        expected_total = 0
        mode = "wb"

        if resume_from:
            response.close()
            resumed_headers = dict(request_headers)
            resumed_headers["Range"] = f"bytes={resume_from}-"
            response = session.get(url, headers=resumed_headers, timeout=timeout, stream=True, allow_redirects=True)
            response.raise_for_status()
            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                if match and int(match.group(1)) == resume_from:
                    if match.group(3).isdigit():
                        expected_total = int(match.group(3))
                    mode = "ab"
                else:
                    resume_from = 0
            else:
                resume_from = 0

        content_length = response.headers.get("Content-Length")
        if not expected_total and content_length:
            try:
                expected_total = resume_from + int(content_length)
            except ValueError:
                expected_total = 0
        if expected_total > limit:
            record.note = f"Video exceeds size limit ({max_video_mb:g} MB)"
            return record
        if expected_total and not disk_has_room(out_dir, max(0, expected_total - resume_from)):
            record.note = "Not enough free disk space for the expected download size"
            return record

        size = resume_from
        with open(partial, mode) as handle:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    handle.close()
                    partial.unlink(missing_ok=True)
                    record.note = f"Video exceeded size limit while downloading ({max_video_mb:g} MB)"
                    return record
                handle.write(chunk)

        if expected_total and size != expected_total:
            record.note = f"Incomplete download {size}/{expected_total}; partial kept at {partial.name}"
            return record
        partial.replace(target)
        record.status = "ok"
        record.files = [str(target)]
        record.bytes = target.stat().st_size
        return record
    except Exception as exc:  # noqa: BLE001
        suffix = f"; partial kept at {partial.name}" if partial and partial.exists() else ""
        record.note = f"{str(exc)[:240]}{suffix}"
        return record
