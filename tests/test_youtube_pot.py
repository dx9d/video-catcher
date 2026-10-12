# SPDX-License-Identifier: GPL-3.0-only
import argparse
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from format_inspector import MediaFormat
from models import DownloadRecord
from video_catcher import download_one, formats_url
from youtube_pot import PotRuntime, pot_worthy_error, youtube_pot_session
from ytdlp_downloader import probe_ytdlp_formats


@contextlib.contextmanager
def fake_pot_session(*args, **kwargs):
    yield PotRuntime(
        base_url="http://127.0.0.1:45678",
        provider="bgutil/1.3.1",
        note="cached provider ready",
    )


class YouTubePotTests(unittest.TestCase):
    def _download_args(self, **overrides):
        values = dict(
            cookies_file="",
            max_video_mb=100,
            timeout=60,
            quality="1080",
            quality_mode="at-most",
            format_id="",
            playlist=False,
            subtitles=False,
            sub_langs="zh.*,en.*",
            embed_thumbnail=False,
            download_archive="",
            min_score=35,
            no_impersonation_retry=False,
            no_youtube_pot=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_pot_worthy_error_markers(self):
        self.assertTrue(pot_worthy_error("ERROR: Sign in to confirm you're not a bot"))
        self.assertTrue(pot_worthy_error("HTTP Error 429: Too Many Requests"))
        self.assertTrue(pot_worthy_error("PO Token required"))
        self.assertFalse(pot_worthy_error("Video unavailable: uploader removed this video"))

    def test_multiple_extractor_args_are_forwarded_as_repeated_flags(self):
        proc = __import__("subprocess").CompletedProcess(
            args=[], returncode=1, stdout="", stderr="failed"
        )
        with patch("ytdlp_downloader._base_cmd", return_value=["yt-dlp"]), patch(
            "ytdlp_downloader._run_ytdlp_args", return_value=proc
        ) as run:
            probe_ytdlp_formats(
                "https://www.youtube.com/watch?v=abcdefghijk",
                extractor_args=[
                    "youtube:player_client=mweb",
                    "youtubepot-bgutilhttp:base_url=http://127.0.0.1:45678",
                ],
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv.count("--extractor-args"), 2)
        self.assertIn("youtube:player_client=mweb", argv)
        self.assertIn("youtubepot-bgutilhttp:base_url=http://127.0.0.1:45678", argv)

    def test_youtube_bot_check_uses_pot_before_generic_fallbacks(self):
        failed = DownloadRecord(
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
            strategy="yt-dlp",
            note="ERROR: Sign in to confirm you're not a bot",
        )
        success = DownloadRecord(
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
            strategy="yt-dlp",
            status="ok",
            files=["video.mp4"],
        )
        args = self._download_args()
        with patch("video_catcher.download_ytdlp", side_effect=[failed, success]) as ytdlp, patch(
            "video_catcher.youtube_pot_session", fake_pot_session
        ), patch("video_catcher._verify_record", side_effect=lambda record: record):
            record, notes = download_one(
                "https://www.youtube.com/watch?v=abcdefghijk",
                Path(tempfile.mkdtemp()),
                args,
            )
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.strategy, "yt-dlp+youtube-pot")
        self.assertEqual(ytdlp.call_count, 2)
        second = ytdlp.call_args_list[1].kwargs
        self.assertEqual(second.get("impersonate", ""), "")
        self.assertTrue(second["prefer_python_module"])
        self.assertIn("youtube:player_client=mweb", second["extractor_args"])
        self.assertTrue(any("PO-token provider" in note for note in notes))


    def test_pot_setup_failure_continues_to_impersonation(self):
        failed = DownloadRecord(
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
            strategy="yt-dlp",
            note="ERROR: Sign in to confirm you're not a bot",
        )
        success = DownloadRecord(
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
            strategy="yt-dlp",
            status="ok",
            files=["video.mp4"],
        )
        args = self._download_args()

        @contextlib.contextmanager
        def broken_pot(*args, **kwargs):
            raise RuntimeError("Node.js >=20 is required")
            yield  # pragma: no cover

        with patch("video_catcher.download_ytdlp", side_effect=[failed, success]) as ytdlp, patch(
            "video_catcher.youtube_pot_session", broken_pot
        ), patch("video_catcher._verify_record", side_effect=lambda record: record):
            record, notes = download_one(
                "https://www.youtube.com/watch?v=abcdefghijk",
                Path(tempfile.mkdtemp()),
                args,
            )
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.strategy, "yt-dlp+impersonate")
        self.assertEqual(ytdlp.call_args_list[1].kwargs["impersonate"], "chrome")
        self.assertTrue(any("PO-token provider unavailable" in note for note in notes))

    def test_task_scoped_provider_is_closed_on_exit(self):
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode or 0

            def kill(self):
                self.returncode = -9

        fake_process = FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "server"
            (server / "build").mkdir(parents=True)
            (server / "build" / "main.js").write_text("", encoding="utf-8")
            with patch("youtube_pot.ensure_bgutil_server", return_value=(server, "ready")), patch(
                "youtube_pot.find_executable", return_value="node"
            ), patch("youtube_pot._free_port", return_value=45678), patch(
                "youtube_pot._wait_for_port", return_value=True
            ), patch("youtube_pot.subprocess.Popen", return_value=fake_process), patch(
                "youtube_pot.video_catcher_home", return_value=Path(tmp) / "home"
            ):
                with youtube_pot_session() as runtime:
                    self.assertEqual(runtime.base_url, "http://127.0.0.1:45678")
                    self.assertFalse(fake_process.terminated)
                self.assertTrue(fake_process.terminated)

    def test_formats_command_uses_pot_after_youtube_bot_check(self):
        args = argparse.Namespace(
            url="https://www.youtube.com/watch?v=abcdefghijk",
            out_root=tempfile.mkdtemp(),
            title="formats",
            cookies_file="",
            no_impersonation_retry=False,
            no_youtube_pot=False,
            timeout=60,
        )
        fmt = MediaFormat(source="yt-dlp", label="1080p", width=1920, height=1080, format_id="137")
        with patch(
            "video_catcher.probe_ytdlp_formats",
            side_effect=[
                ([], {}, "ERROR: Sign in to confirm you're not a bot"),
                ([fmt], {"title": "Demo"}, "yt-dlp exposed 1 video resolution(s)"),
            ],
        ) as probe, patch("video_catcher.youtube_pot_session", fake_pot_session):
            result = formats_url(args)
        self.assertEqual(result, 0)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("youtube:player_client=mweb", probe.call_args_list[1].kwargs["extractor_args"])
        self.assertTrue(probe.call_args_list[1].kwargs["prefer_python_module"])


if __name__ == "__main__":
    unittest.main()
