# SPDX-License-Identifier: GPL-3.0-only
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from media_detector import detect_manifest_drm
from models import DownloadRecord, MediaCandidate, SniffResult
from report import write_inspect_report
from video_catcher import _attach_separate_audio_if_needed, download_one, resume_command
from ytdlp_downloader import _run_ytdlp_args, format_selector


class _FailingSession:
    def get(self, *args, **kwargs):
        raise requests.HTTPError("403 Forbidden")


class SecurityResumeTests(unittest.TestCase):
    def test_authorization_not_exposed_in_os_argv(self):
        fake = __import__("subprocess").CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("ytdlp_downloader.python_module_available", return_value=True), patch(
            "ytdlp_downloader.subprocess.run", return_value=fake
        ) as run:
            _run_ytdlp_args(
                ["--add-header", "Authorization:Bearer top-secret", "https://example.com/master.m3u8"],
                timeout=10,
                contains_sensitive_header=True,
            )
        os_argv = run.call_args.args[0]
        self.assertFalse(any("top-secret" in part for part in os_argv))
        self.assertIn("top-secret", run.call_args.kwargs["input"])

    def test_drm_probe_failure_is_unknown_not_clear(self):
        item = MediaCandidate(
            "https://cdn.example/master.m3u8",
            "browser-response",
            kind="stream",
            content_type="application/vnd.apple.mpegurl",
        )
        status, note = detect_manifest_drm(_FailingSession(), item)
        self.assertEqual(status, "unknown")
        self.assertIn("could not complete", note)

    def test_unknown_drm_is_visible_in_report(self):
        item = MediaCandidate(
            "https://cdn.example/master.m3u8",
            "browser-response",
            kind="stream",
            drm_status="unknown",
            drm_note="DRM probe could not complete: 403",
        )
        with tempfile.TemporaryDirectory() as tmp:
            md_path, _ = write_inspect_report(Path(tmp), "https://example.com", [item], [])
            text = md_path.read_text(encoding="utf-8")
        self.assertIn("未知", text)
        self.assertIn("403", text)

    def test_quality_aliases(self):
        self.assertIn("height<=1080", format_selector("1080p"))
        self.assertIn("height<=2160", format_selector("4k"))

    def test_separate_audio_candidate_is_muxed_instead_of_accepting_silent_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            video_path = out / "video.mp4"
            audio_path = out / "audio.m4a"
            merged_path = out / "video-with-audio.mp4"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")
            merged_path.write_bytes(b"merged")
            record = DownloadRecord(
                source_url="https://example.com/page",
                strategy="sniffed-direct-http",
                status="ok",
                files=[str(video_path)],
                bytes=video_path.stat().st_size,
            )
            video_candidate = MediaCandidate(
                "https://cdn.example/video.mp4", "browser-response", kind="direct", score=80
            )
            audio_candidate = MediaCandidate(
                "https://cdn.example/audio.m4a", "browser-response", kind="audio", score=20
            )
            args = argparse.Namespace(max_video_mb=100, timeout=60)
            audio_record = DownloadRecord(
                source_url=audio_candidate.url,
                strategy="direct-http",
                status="ok",
                files=[str(audio_path)],
                bytes=audio_path.stat().st_size,
            )
            with patch("video_catcher._file_has_audio", return_value=False), patch(
                "video_catcher.download_direct", return_value=audio_record
            ), patch(
                "video_catcher.mux_video_audio", return_value=(True, "merged", merged_path)
            ):
                result = _attach_separate_audio_if_needed(
                    record,
                    video_candidate,
                    [video_candidate, audio_candidate],
                    requests.Session(),
                    out,
                    args,
                )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.files, [str(merged_path)])
            self.assertIn("separate-audio-mux", result.strategy)
            self.assertFalse(video_path.exists())
            self.assertFalse(audio_path.exists())


if __name__ == "__main__":
    unittest.main()
