# SPDX-License-Identifier: GPL-3.0-only
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from direct_downloader import download_direct

DATA = b"\x00\x00\x00\x18ftypmp42" + b"A" * (1024 * 64)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        start = 0
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = DATA[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA)-1}/{len(DATA)}")
        else:
            body = DATA
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class DirectDownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/sample.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_download_and_resume_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            partial = out / "sample.mp4.part"
            partial.write_bytes(DATA[:1000])
            record = download_direct(requests.Session(), self.url, out, max_video_mb=10)
            self.assertEqual(record.status, "ok")
            self.assertEqual((out / "sample.mp4").read_bytes(), DATA)
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
