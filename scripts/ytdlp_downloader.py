# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from common import disk_has_room, python_module_available, safe_forward_headers, ytdlp_command
from models import DownloadRecord, MediaCandidate
from format_inspector import MediaFormat, formats_from_ytdlp_info

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".flv"}
SCRIPT_DIR = Path(__file__).resolve().parent


def _append_extractor_args(cmd: list[str], extractor_args: str | list[str] | tuple[str, ...]) -> None:
    if isinstance(extractor_args, str):
        values = [extractor_args] if extractor_args.strip() else []
    else:
        values = [str(value).strip() for value in (extractor_args or []) if str(value).strip()]
    for value in values:
        cmd += ["--extractor-args", value]


def _diagnostic_tail(stdout: str, stderr: str, *, limit: int = 800) -> str:
    lines = "\n".join(part for part in (stdout, stderr) if part).strip().splitlines()
    if not lines:
        return ""
    text = " | ".join(line.strip() for line in lines[-8:] if line.strip())
    return text[-limit:]


def normalize_quality(quality: str) -> str:
    value = str(quality or "").strip().lower()
    aliases = {
        "4k": "2160",
        "2160p": "2160",
        "2k": "1440",
        "1440p": "1440",
        "1080p": "1080",
        "720p": "720",
        "480p": "480",
        "360p": "360",
    }
    value = aliases.get(value, value)
    if value == "best":
        return value
    try:
        height = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("quality must be a height such as 1080/1080p, 4k, or 'best'") from exc
    if height <= 0:
        raise ValueError("quality must be a positive height or 'best'")
    return str(height)


def format_selector(quality: str, mode: str = "at-most", format_id: str = "") -> str:
    if format_id:
        value = str(format_id).strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("format_id must be a non-empty yt-dlp format id without whitespace")
        return f"{value}+ba/{value}"
    quality = normalize_quality(quality)
    if quality == "best":
        return "bv*+ba/b"
    if mode not in {"at-most", "exact"}:
        raise ValueError("quality mode must be 'at-most' or 'exact'")
    height = int(quality)
    if mode == "exact":
        return f"bv*[height={height}]+ba/b[height={height}]"
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def _base_cmd() -> list[str] | None:
    return ytdlp_command()


def _run_ytdlp_args(
    argv: list[str],
    *,
    timeout: int,
    contains_sensitive_header: bool = False,
    prefer_python_module: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp, keeping sensitive headers out of the operating-system command line."""
    if contains_sensitive_header:
        if not python_module_available("yt_dlp"):
            raise RuntimeError(
                "Authorization-bearing downloads require the Python yt-dlp package; install requirements.txt"
            )
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "ytdlp_worker.py")],
            input=json.dumps({"argv": argv}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    if prefer_python_module:
        if not python_module_available("yt_dlp"):
            raise RuntimeError("Python yt-dlp package is required for plugin-backed downloads")
        return subprocess.run(
            [sys.executable, "-m", "yt_dlp", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    base = _base_cmd()
    if not base:
        raise RuntimeError("yt-dlp not installed")
    return subprocess.run([*base, *argv], capture_output=True, text=True, timeout=timeout)


def probe_ytdlp(
    url: str,
    *,
    cookies_file: str = "",
    timeout: int = 90,
) -> tuple[MediaCandidate | None, str]:
    base = _base_cmd()
    if not base:
        return None, "yt-dlp not installed"
    cmd = ["--ignore-config", "--skip-download", "--no-playlist", "--dump-single-json"]
    if cookies_file:
        cmd += ["--cookies", str(Path(cookies_file).expanduser())]
    cmd += [url]
    try:
        proc = _run_ytdlp_args(cmd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"yt-dlp probe failed: {str(exc)[:160]}"
    if proc.returncode != 0:
        diagnostic = _diagnostic_tail(proc.stdout or "", proc.stderr or "", limit=500)
        return None, diagnostic or f"yt-dlp exited {proc.returncode}"
    try:
        info = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        return None, f"yt-dlp returned invalid JSON: {exc}"
    candidate = MediaCandidate(
        url=url,
        source="yt-dlp",
        kind="platform",
        score=100,
        reasons=["yt-dlp extractor recognized URL"],
        metadata={
            "title": info.get("title"),
            "id": info.get("id"),
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "live_status": info.get("live_status"),
        },
    )
    return candidate, "yt-dlp recognized the URL"


def probe_ytdlp_formats(
    url: str,
    *,
    cookies_file: str = "",
    impersonate: str = "",
    extractor_args: str | list[str] | tuple[str, ...] = "",
    prefer_python_module: bool = False,
    timeout: int = 90,
) -> tuple[list[MediaFormat], dict, str]:
    if not _base_cmd():
        return [], {}, "yt-dlp not installed"
    cmd = ["--ignore-config", "--skip-download", "--no-playlist", "--dump-single-json"]
    if cookies_file:
        cmd += ["--cookies", str(Path(cookies_file).expanduser())]
    if impersonate:
        cmd += ["--impersonate", impersonate]
    _append_extractor_args(cmd, extractor_args)
    cmd += [url]
    try:
        proc = _run_ytdlp_args(cmd, timeout=timeout, prefer_python_module=prefer_python_module)
    except Exception as exc:  # noqa: BLE001
        return [], {}, f"yt-dlp format probe failed: {str(exc)[:180]}"
    if proc.returncode != 0:
        diagnostic = _diagnostic_tail(proc.stdout or "", proc.stderr or "", limit=600)
        return [], {}, diagnostic or f"yt-dlp exited {proc.returncode}"
    try:
        info = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        return [], {}, f"yt-dlp returned invalid format JSON: {exc}"
    formats = formats_from_ytdlp_info(info)
    note = f"yt-dlp exposed {len(formats)} video resolution(s)"
    return formats, info, note


def download_ytdlp(
    url: str,
    out_dir: Path,
    *,
    quality: str = "1080",
    quality_mode: str = "at-most",
    format_id: str = "",
    max_video_mb: float = 2048,
    cookies_file: str = "",
    playlist: bool = False,
    subtitles: bool = False,
    sub_langs: str = "zh.*,en.*",
    embed_thumbnail: bool = False,
    download_archive: str = "",
    headers: dict[str, str] | None = None,
    impersonate: str = "",
    extractor_args: str | list[str] | tuple[str, ...] = "",
    prefer_python_module: bool = False,
    timeout: int = 3600,
) -> DownloadRecord:
    record = DownloadRecord(source_url=url, strategy="yt-dlp", selected_media_url=url)
    if not _base_cmd():
        record.note = "yt-dlp not installed"
        return record
    out_dir.mkdir(parents=True, exist_ok=True)
    required_budget = int(max_video_mb * 1024 * 1024)
    if not disk_has_room(out_dir, required_budget):
        record.note = (
            f"Not enough free disk space for the configured {max_video_mb:g} MB ceiling "
            "plus the 256 MB safety reserve"
        )
        return record

    try:
        selector = format_selector(quality, quality_mode, format_id)
    except ValueError as exc:
        record.note = str(exc)
        return record

    template = str(out_dir / "%(title).150B [%(id)s].%(ext)s")
    cmd = [
        "--ignore-config",
        "--continue",
        "--part",
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "fragment:exp=1:20",
        "--concurrent-fragments", "4",
        "--no-progress",
        "--print", "after_move:filepath",
        "--merge-output-format", "mp4",
        "--embed-metadata",
        "--write-info-json",
        "--max-filesize", str(int(max_video_mb * 1024 * 1024)),
        "-f", selector,
        "-o", template,
    ]
    cmd += ["--yes-playlist" if playlist else "--no-playlist"]
    if cookies_file:
        cmd += ["--cookies", str(Path(cookies_file).expanduser())]
    if subtitles:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", sub_langs, "--convert-subs", "srt"]
    if embed_thumbnail:
        cmd += ["--embed-thumbnail"]
    if download_archive:
        cmd += ["--download-archive", str(Path(download_archive).expanduser())]
    if impersonate:
        cmd += ["--impersonate", impersonate]
    _append_extractor_args(cmd, extractor_args)

    forwarded = safe_forward_headers(headers or {}, include_authorization=True)
    has_authorization = any(key.lower() == "authorization" for key in forwarded)
    for key, value in forwarded.items():
        cmd += ["--add-header", f"{key}:{value}"]
    cmd += [url]

    before = {path.resolve() for path in out_dir.glob("*")}
    try:
        proc = _run_ytdlp_args(
            cmd,
            timeout=timeout,
            contains_sensitive_header=has_authorization,
            prefer_python_module=prefer_python_module,
        )
    except subprocess.TimeoutExpired:
        record.note = f"yt-dlp timed out after {timeout}s; .part files may be resumable"
        return record
    except Exception as exc:  # noqa: BLE001
        record.note = str(exc)[:240]
        return record

    printed = []
    for line in (proc.stdout or "").splitlines():
        path = Path(line.strip())
        if path.exists() and path.suffix.lower() in VIDEO_SUFFIXES:
            printed.append(path.resolve())
    created = [
        path.resolve()
        for path in out_dir.glob("*")
        if path.resolve() not in before and path.suffix.lower() in VIDEO_SUFFIXES
    ]
    files = sorted({str(path) for path in printed + created})
    diagnostic = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    lowered = diagnostic.lower()
    if proc.returncode == 0 and files:
        record.status = "ok"
        record.files = files
        record.bytes = sum(Path(path).stat().st_size for path in files)
        record.metadata["selection"] = {"quality": quality, "quality_mode": quality_mode, "format_id": format_id, "selector": selector}
        return record
    if proc.returncode == 0 and download_archive and (
        "already been recorded in the archive" in lowered or "already been downloaded" in lowered
    ):
        record.status = "skipped"
        record.note = "Already present in download archive"
        return record
    record.note = _diagnostic_tail(proc.stdout or "", proc.stderr or "", limit=800) or f"yt-dlp exited {proc.returncode}"
    return record
