# SPDX-License-Identifier: GPL-3.0-only
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from static_resolver import extract_candidates_from_html


class StaticResolverTests(unittest.TestCase):
    def test_video_source_and_json_url(self):
        html = '''
        <html><head><meta property="og:video" content="/media/hero.mp4"></head>
        <body><video><source src="https://cdn.example/master.m3u8"></video>
        <script>window.data={"playUrl":"https:\\/\\/cdn.example\\/alt.mp4?x=1"}</script></body></html>
        '''
        items = extract_candidates_from_html(html, "https://site.example/post/1")
        urls = {item.url for item in items}
        self.assertIn("https://site.example/media/hero.mp4", urls)
        self.assertIn("https://cdn.example/master.m3u8", urls)
        self.assertIn("https://cdn.example/alt.mp4?x=1", urls)


if __name__ == "__main__":
    unittest.main()
