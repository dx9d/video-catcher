# SPDX-License-Identifier: GPL-3.0-only
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ytdlp_downloader import format_selector


class YtDlpBuilderTests(unittest.TestCase):
    def test_default_height_selector(self):
        value = format_selector("1080")
        self.assertIn("height<=1080", value)

    def test_best_selector(self):
        self.assertEqual(format_selector("best"), "bv*+ba/b")

    def test_invalid_height(self):
        with self.assertRaises((ValueError, TypeError)):
            format_selector("abc")


if __name__ == "__main__":
    unittest.main()
