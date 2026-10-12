# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from stream_downloader import _select_hls_variant, download_stream_ffmpeg


class _Response:
    def __init__(self, text: str):
        self.text = text
    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, text: str):
        self.text = text
    def get(self, *args, **kwargs):
        return _Response(self.text)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720
720/index.m3u8
"""


class StreamFallbackTests(unittest.TestCase):
    def test_exact_hls_variant_is_selected(self):
        url, note = _select_hls_variant(
            _Session(MASTER),
            "https://example.test/master.m3u8",
            headers={},
            quality="720p",
            quality_mode="exact",
            timeout=10,
        )
        self.assertEqual(url, "https://example.test/720/index.m3u8")
        self.assertIn("720p", note)

    def test_exact_hls_variant_does_not_silently_downgrade(self):
        url, note = _select_hls_variant(
            _Session(MASTER),
            "https://example.test/master.m3u8",
            headers={},
            quality="1080p",
            quality_mode="exact",
            timeout=10,
        )
        self.assertEqual(url, "")
        self.assertIn("not available", note)

    def test_ffmpeg_fallback_refuses_authorization_in_process_argv(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch("stream_downloader.find_executable", return_value="/usr/bin/ffmpeg"):
                record = download_stream_ffmpeg(
                    _Session(MASTER),
                    "https://example.test/master.m3u8",
                    Path(raw),
                    headers={"Authorization": "Bearer secret"},
                    max_video_mb=10,
                )
        self.assertEqual(record.status, "failed")
        self.assertIn("Authorization", record.note)


if __name__ == "__main__":
    unittest.main()
