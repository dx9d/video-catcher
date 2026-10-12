# SPDX-License-Identifier: GPL-3.0-only
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from format_inspector import formats_from_ytdlp_info, parse_hls_formats, parse_mpd_formats
from media_detector import rank_candidates
from models import MediaCandidate
from report import write_formats_report
from video_catcher import build_parser, resume_command
from ytdlp_downloader import format_selector


class FormatDiscoveryTests(unittest.TestCase):
    def test_exact_quality_selector_does_not_silently_downgrade(self):
        self.assertEqual(
            format_selector("1080p", "exact"),
            "bv*[height=1080]+ba/b[height=1080]",
        )
        self.assertIn("height<=1080", format_selector("1080p", "at-most"))

    def test_hls_master_lists_resolutions(self):
        text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,CODECS=\"avc1.4d401e,mp4a.40.2\"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3500000,RESOLUTION=1920x1080,FRAME-RATE=60.0,CODECS=\"avc1.640028,mp4a.40.2\"
high/index.m3u8
"""
        formats = parse_hls_formats(text, "https://cdn.example/master.m3u8")
        self.assertEqual([item.height for item in formats], [360, 1080])
        self.assertEqual(formats[-1].width, 1920)
        self.assertEqual(formats[-1].fps, 60.0)
        self.assertEqual(formats[-1].url, "https://cdn.example/high/index.m3u8")

    def test_dash_manifest_lists_video_representations(self):
        text = """<?xml version='1.0'?>
<MPD xmlns='urn:mpeg:dash:schema:mpd:2011'>
 <Period><AdaptationSet mimeType='video/mp4' codecs='avc1.640028'>
   <Representation id='v720' bandwidth='1800000' width='1280' height='720' frameRate='30'/>
   <Representation id='v1080' bandwidth='4200000' width='1920' height='1080' frameRate='60/1'/>
 </AdaptationSet></Period>
</MPD>"""
        formats = parse_mpd_formats(text, "https://cdn.example/manifest.mpd")
        self.assertEqual([item.height for item in formats], [720, 1080])
        self.assertEqual(formats[-1].format_id, "v1080")
        self.assertEqual(formats[-1].fps, 60.0)

    def test_ytdlp_formats_are_collapsed_into_user_facing_resolutions(self):
        info = {
            "formats": [
                {"format_id": "18", "height": 360, "width": 640, "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4", "tbr": 600, "filesize": 10_000_000},
                {"format_id": "137", "height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none", "ext": "mp4", "tbr": 4000, "filesize_approx": 100_000_000},
                {"format_id": "399", "height": 1080, "width": 1920, "vcodec": "av01", "acodec": "none", "ext": "mp4", "tbr": 2500, "filesize_approx": 70_000_000},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
            ]
        }
        formats = formats_from_ytdlp_info(info)
        self.assertEqual([item.height for item in formats], [360, 1080])
        self.assertEqual(formats[-1].height, 1080)
        self.assertIn("height=1080", formats[-1].selector)
        self.assertEqual(formats[-1].audio_codec, "best audio separately")

    def test_candidate_dedupe_preserves_dom_dimensions(self):
        url = "https://cdn.example/video.mp4"
        network = MediaCandidate(url, "browser-response", content_type="video/mp4", metadata={"page_host": "example.com"})
        dom = MediaCandidate(url, "browser-dom:video:currentSrc", metadata={"resource_type": "media", "width": 1920, "height": 1080})
        merged = rank_candidates([network, dom])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata.get("width"), 1920)
        self.assertEqual(merged[0].metadata.get("height"), 1080)

    def test_formats_command_is_exposed(self):
        parser = build_parser()
        args = parser.parse_args(["formats", "https://example.com/watch"])
        self.assertEqual(args.command, "formats")

    def test_resume_preserves_exact_quality_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "task.json").write_text(json.dumps({
                "urls": ["https://example.com/watch"],
                "quality": "1080",
                "quality_mode": "exact",
                "format_id": "137",
            }), encoding="utf-8")
            args = argparse.Namespace(
                run_dir=str(run_dir), quality="", quality_mode="", format_id="",
                max_video_mb=0, cookies_file="",
                min_score=0, timeout=0,
            )
            captured = {}

            def fake_download(forwarded):
                captured["args"] = forwarded
                return 0
            with patch("video_catcher.download_command", side_effect=fake_download):
                self.assertEqual(resume_command(args), 0)
            self.assertEqual(captured["args"].quality_mode, "exact")
            self.assertEqual(captured["args"].format_id, "137")

    def test_formats_report_has_machine_readable_selection_data(self):
        info = {"formats": [{"format_id": "137", "height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none", "ext": "mp4"}]}
        formats = formats_from_ytdlp_info(info)
        with tempfile.TemporaryDirectory() as tmp:
            md, js = write_formats_report(Path(tmp), "https://example.com", formats, ["ok"], {"title": "Demo"})
            payload = json.loads(js.read_text(encoding="utf-8"))
            self.assertEqual(payload["formats"][0]["height"], 1080)
            self.assertIn("1080p", md.read_text(encoding="utf-8"))
            self.assertIn("quality-mode exact", md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
