# SPDX-License-Identifier: GPL-3.0-only
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from media_detector import detect_drm_text, rank_candidates
from models import MediaCandidate


class MediaDetectorTests(unittest.TestCase):
    def test_master_manifest_scores_above_thumbnail(self):
        items = rank_candidates([
            MediaCandidate("https://cdn.example/master.m3u8", "browser-response", content_type="application/vnd.apple.mpegurl"),
            MediaCandidate("https://cdn.example/thumbnail.mp4", "browser-response", content_type="video/mp4", content_length=200_000),
        ])
        self.assertEqual(items[0].kind, "stream")
        self.assertGreater(items[0].score, items[1].score)

    def test_hls_aes128_is_not_treated_as_drm(self):
        drm, note = detect_drm_text('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"', "stream-hls")
        self.assertFalse(drm)
        self.assertEqual(note, "")

    def test_hls_sample_aes_is_drm(self):
        drm, note = detect_drm_text('#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery"', "stream-hls")
        self.assertTrue(drm)
        self.assertIn("SAMPLE-AES", note)

    def test_widevine_mpd_is_drm(self):
        drm, note = detect_drm_text('<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>', "stream-dash")
        self.assertTrue(drm)
        self.assertIn("Widevine", note)


if __name__ == "__main__":
    unittest.main()
