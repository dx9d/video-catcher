#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Dependency diagnostics that intentionally uses only the Python standard library."""
from __future__ import annotations

import importlib.util
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def module_exists(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def default_home() -> Path:
    override = os.environ.get("VIDEO_CATCHER_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".video-catcher"


def distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def node_major() -> int:
    node = shutil.which("node")
    if not node:
        return 0
    try:
        proc = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return 0
    match = re.search(r"v?(\d+)", (proc.stdout or proc.stderr or "").strip())
    return int(match.group(1)) if match else 0


def main() -> int:
    checks = {
        "python": sys.version.split()[0],
        "python>=3.10": sys.version_info >= (3, 10),
        "requests": module_exists("requests"),
        "yt-dlp-binary": bool(shutil.which("yt-dlp")),
        "yt-dlp-python-module": module_exists("yt_dlp"),
        "curl-cffi": module_exists("curl_cffi"),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "youtube-pot-plugin": distribution_version("bgutil-ytdlp-pot-provider"),
        "node-major": node_major(),
        "npm": bool(shutil.which("npm")),
        "npx": bool(shutil.which("npx")),
        "youtube-pot-provider-cache": str(
            default_home() / "providers" / "bgutil-ytdlp-pot-provider" / "1.3.1" / "server"
        ),
    }
    checks["yt-dlp"] = bool(checks["yt-dlp-binary"] or checks["yt-dlp-python-module"])
    checks["secure-authorization-forwarding"] = bool(checks["yt-dlp-python-module"])
    checks["core-ready"] = bool(
        checks["python>=3.10"] and checks["requests"] and checks["yt-dlp"] and checks["ffmpeg"] and checks["ffprobe"]
    )
    checks["youtube-pot-capable"] = bool(
        checks["youtube-pot-plugin"] == "1.3.1"
        and checks["node-major"] >= 20
        and checks["npm"]
        and checks["npx"]
    )
    checks["autonomous-ready"] = bool(checks["core-ready"] and checks["curl-cffi"])
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["core-ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
