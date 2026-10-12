# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import subprocess
from pathlib import Path

from common import find_executable


def _unique_output(video_path: Path, *, suffix: str = ".mp4") -> Path:
    base = video_path.with_name(f"{video_path.stem}-with-audio{suffix}")
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = video_path.with_name(f"{video_path.stem}-with-audio-{index:02d}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate muxed output filename")


def mux_video_audio(video_path: Path, audio_path: Path, *, timeout: int = 900) -> tuple[bool, str, Path | None]:
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not installed; cannot merge separate video/audio tracks", None

    common = [
        ffmpeg, "-y", "-v", "error",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
    ]
    attempts = [
        ("mp4-stream-copy", _unique_output(video_path, suffix=".mp4"), ["-c", "copy"]),
        ("mp4-aac-audio", _unique_output(video_path, suffix=".mp4"), ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]),
        ("mkv-stream-copy", _unique_output(video_path, suffix=".mkv"), ["-c", "copy"]),
        (
            "mp4-h264-aac-transcode",
            _unique_output(video_path, suffix=".mp4"),
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k"],
        ),
    ]

    last_error = ""
    for label, target, codecs in attempts:
        cmd = [*common, *codecs, str(target)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:220]
            continue
        if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return True, f"separate video/audio tracks merged with ffmpeg ({label})", target
        last_error = (proc.stderr or proc.stdout or f"ffmpeg exited {proc.returncode}").strip()[-220:]
        target.unlink(missing_ok=True)
    return False, f"ffmpeg merge failed: {last_error}", None
