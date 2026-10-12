# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common import find_executable


def verify_video(path: Path, *, timeout: int = 30) -> tuple[bool | None, str, dict]:
    if not path.is_file() or path.stat().st_size <= 0:
        return False, "file missing or empty", {}
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        return None, "ffprobe not installed; basic file existence check passed", {"bytes": path.stat().st_size}
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"ffprobe failed: {str(exc)[:160]}", {}
    if proc.returncode != 0:
        return False, (proc.stderr or "ffprobe could not read file")[-220:], {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False, "ffprobe returned invalid JSON", {}
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        return False, "no video stream found", payload
    fmt = payload.get("format") or {}
    note = (
        f"video={video.get('codec_name') or '?'} {video.get('width') or '?'}x{video.get('height') or '?'}; "
        f"audio={(audio or {}).get('codec_name') or 'none'}; duration={fmt.get('duration') or '?'}s"
    )
    return True, note, payload
