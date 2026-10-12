# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)


def video_catcher_home() -> Path:
    override = os.environ.get("VIDEO_CATCHER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".video-catcher"


def clean_url(value: str) -> str:
    url = str(value or "").strip().strip("<>")
    if re.search(r"[\x00-\x20]", url):
        raise ValueError("URL contains whitespace or control characters")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Unsupported URL: {url or '<empty>'}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Embedded URL credentials are not allowed")
    return url


def safe_filename(text: str, fallback: str = "video", max_len: int = 160) -> str:
    value = unquote(text or "").strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if len(value) > max_len:
        stem, dot, suffix = value.rpartition(".")
        if dot and len(suffix) <= 8:
            value = stem[: max_len - len(suffix) - 1].rstrip() + "." + suffix
        else:
            value = value[:max_len].rstrip()
    return value or fallback


def slugify(text: str, fallback: str = "video-download", max_len: int = 64) -> str:
    value = re.sub(r"[^\w.-]+", "-", text or "", flags=re.U).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    return (value or fallback)[:max_len].strip("-._") or fallback


def unique_run_dir(root: Path, title: str, date_text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"{date_text}-{slugify(title)}"
    if not base.exists():
        base.mkdir(parents=True)
        return base
    for index in range(2, 1000):
        candidate = root / f"{date_text}-{slugify(title)}-{index:02d}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    raise RuntimeError(f"Cannot create a unique task directory under {root}")


def find_executable(name: str) -> str | None:
    return shutil.which(name)


def python_module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def ytdlp_command() -> list[str] | None:
    binary = find_executable("yt-dlp")
    if binary:
        return [binary]
    if python_module_available("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


def human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def disk_has_room(directory: Path, required_bytes: int, reserve_bytes: int = 256 * 1024 * 1024) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    return free >= required_bytes + reserve_bytes


def safe_forward_headers(headers: dict[str, str], *, include_authorization: bool = False) -> dict[str, str]:
    allow = {"referer", "origin", "user-agent", "accept", "accept-language"}
    if include_authorization:
        allow.add("authorization")
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if key.lower() in allow and value:
            out[key] = value
    return out


def load_netscape_cookies(path: Path) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for raw in path.expanduser().read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw or raw.startswith("#") and not raw.startswith("#HttpOnly_"):
            continue
        http_only = raw.startswith("#HttpOnly_")
        if http_only:
            raw = raw[len("#HttpOnly_"):]
        parts = raw.split("\t")
        if len(parts) != 7:
            continue
        domain, _include_subdomains, cookie_path, secure, expires, name, value = parts
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": cookie_path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        try:
            expiry = int(expires)
            if expiry > 0:
                cookie["expires"] = expiry
        except ValueError:
            pass
        cookies.append(cookie)
    return cookies


