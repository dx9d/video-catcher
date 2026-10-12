#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verifier import verify_video

# A minimal 1x1 transparent PNG.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def ffprobe_resolution(path: Path) -> tuple[int, int]:
    proc = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path)
    ])
    if proc.returncode != 0:
        return 0, 0
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return 0, 0
    return int(streams[0].get("width") or 0), int(streams[0].get("height") or 0)


def generate_mp4(path: Path, width: int, height: int, seconds: float = 1.2) -> None:
    proc = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
        "-t", str(seconds), "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", str(path),
    ], timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg fixture generation failed")


def generate_hls(root: Path, label: str, width: int, height: int) -> None:
    out = root / "hls" / label
    out.mkdir(parents=True, exist_ok=True)
    proc = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100",
        "-t", "1.5", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "0.6", "-hls_list_size", "0",
        "-hls_segment_filename", str(out / "seg-%02d.ts"), str(out / "index.m3u8"),
    ], timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"HLS fixture {label} generation failed")


def newest_video(root: Path) -> Path | None:
    videos = [p for p in root.rglob("*.mp4") if p.is_file() and ".part" not in p.name]
    return max(videos, key=lambda p: p.stat().st_mtime, default=None)


def make_wechat_article_page(webroot: Path) -> None:
    """Create a local page that mirrors the structure of a WeChat article."""
    (webroot / "fixture.png").write_bytes(PNG_1PX)
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>
<h1 id="activity-name">Selftest Article</h1>
<span id="js_author_name">Selftest Author</span>
<div id="js_content">
<p>First paragraph.</p>
<img data-src="/fixture.png" alt="img">
<p>Second paragraph.</p>
</div>
</body></html>
"""
    (webroot / "article.html").write_text(html, encoding="utf-8")


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print(json.dumps({"ok": False, "error": "ffmpeg/ffprobe required"}, indent=2))
        return 1
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="video-catcher-selftest-") as raw:
        tmp = Path(raw)
        webroot = tmp / "web"
        outputs = tmp / "outputs"
        webroot.mkdir()
        outputs.mkdir()

        generate_mp4(webroot / "fixture.mp4", 320, 180)
        for label, w, h in (("360", 640, 360), ("720", 1280, 720), ("1080", 1920, 1080)):
            generate_hls(webroot, label, w, h)
        (webroot / "master.m3u8").write_text(
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=640x360,CODECS="avc1.42e01e,mp4a.40.2"\n'
            "hls/360/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1600000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"\n'
            "hls/720/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=3200000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
            "hls/1080/index.m3u8\n",
            encoding="utf-8",
        )
        (webroot / "page.html").write_text(
            '<!doctype html><html><body><video controls src="/fixture.mp4"></video></body></html>',
            encoding="utf-8",
        )
        make_wechat_article_page(webroot)

        handler = partial(SimpleHTTPRequestHandler, directory=str(webroot))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            cli = [sys.executable, str(SCRIPT_DIR / "video_catcher.py")]

            direct_root = outputs / "direct"
            proc = run(cli + ["download", f"{base}/fixture.mp4", "--out-root", str(direct_root), "--max-video-mb", "50"], timeout=60)
            direct_video = newest_video(direct_root)
            direct_ok = proc.returncode == 0 and direct_video is not None and verify_video(direct_video)[0] is True
            results.append({"name": "direct-http-download", "ok": direct_ok, "detail": (proc.stderr or proc.stdout)[-300:]})

            static_root = outputs / "static"
            proc = run(cli + ["download", f"{base}/page.html", "--out-root", str(static_root), "--max-video-mb", "50"], timeout=60)
            static_video = newest_video(static_root)
            static_ok = proc.returncode == 0 and static_video is not None and verify_video(static_video)[0] is True
            results.append({"name": "static-page-discovery-download", "ok": static_ok, "detail": (proc.stderr or proc.stdout)[-300:]})

            format_root = outputs / "formats"
            proc = run(cli + ["formats", f"{base}/master.m3u8", "--out-root", str(format_root)], timeout=60)
            report_files = list(format_root.rglob("formats-report.json"))
            heights: list[int] = []
            if report_files:
                payload = json.loads(report_files[-1].read_text(encoding="utf-8"))
                heights = sorted({int(item.get("height") or 0) for item in payload.get("formats", []) if int(item.get("height") or 0)})
            results.append({"name": "hls-format-discovery", "ok": proc.returncode == 0 and heights == [360, 720, 1080], "detail": f"heights={heights}"})

            hls_root = outputs / "hls-download"
            proc = run(cli + ["download", f"{base}/master.m3u8", "--out-root", str(hls_root), "--quality", "720p", "--quality-mode", "exact", "--max-video-mb", "50"], timeout=90)
            hls_video = newest_video(hls_root)
            resolution = ffprobe_resolution(hls_video) if hls_video else (0, 0)
            hls_ok = proc.returncode == 0 and hls_video is not None and verify_video(hls_video)[0] is True and resolution == (1280, 720)
            results.append({"name": "hls-exact-720-download", "ok": hls_ok, "detail": f"resolution={resolution}; {(proc.stderr or proc.stdout)[-220:]}"})

            article_root = outputs / "article"
            proc = run(cli + ["article", f"{base}/article.html", "--out-root", str(article_root), "--title", "selftest-article"], timeout=60)
            article_reports = list(article_root.rglob("article-report.json"))
            article_payload = json.loads(article_reports[-1].read_text(encoding="utf-8")) if article_reports else {}
            article_ok = (
                proc.returncode in (0, 2)
                and article_payload.get("title") == "Selftest Article"
                and article_payload.get("author") == "Selftest Author"
                and any(im.get("local") for im in article_payload.get("images", []))
            )
            results.append({"name": "wechat-article-offline-save", "ok": article_ok, "detail": f"title={article_payload.get('title')!r}; images={len(article_payload.get('images', []))}"})
        finally:
            server.shutdown()
            server.server_close()

    required = [item for item in results if item.get("required", True)]
    ok = all(bool(item["ok"]) for item in required)
    payload = {
        "ok": ok,
        "required_tests_passed": sum(1 for item in required if item.get("ok")),
        "required_tests_total": len(required),
        "tests": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())