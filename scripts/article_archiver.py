#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""
article-archiver — 微信公众号文章离线保存（agent 技能，非视频）

用法：
    python article_archiver.py <mp.weixin.qq.com 文章URL> [--out-root <目录>] [--title <标题>] [--no-images]

产物（<输出根>/<日期>-<标题>/）：
    index.html        离线可读全文（图片已本地化）
    article.md        纯文本 Markdown
    images/           正文图片
    article-report.json / article-report.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def default_out_root() -> Path:
    env = os.environ.get("WECHAT_ARTICLE_OUT", "").strip()
    if env:
        return Path(env).expanduser()
    # Default public Documents directory (override with WECHAT_ARTICLE_OUT).
    return Path("~/Documents").expanduser()


def unique_run_dir(title: str, root: Path | None = None) -> Path:
    base_root = (root or default_out_root()).expanduser()
    stamp = dt.date.today().isoformat()
    safe_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip()[:80] or "wechat-article"
    base = base_root / f"{stamp}-{safe_title}"
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base
    n = 2
    while True:
        cand = base_root / f"{stamp}-{safe_title}-{n:02d}"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        n += 1


def _extract(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else ""


def _extract_element_by_id(text: str, element_id: str) -> str:
    """按 id 提取对应 div 的完整内部 HTML（处理嵌套 <div>）。"""
    needle = f'id="{element_id}"'
    pos = text.find(needle)
    if pos < 0:
        return ""
    open_end = text.find(">", pos)
    if open_end < 0:
        return ""
    depth = 1
    i = open_end + 1
    while i < len(text) and depth > 0:
        next_open = text.find("<div", i)
        next_close = text.find("</div>", i)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            i = next_close + 6
    return text[open_end + 1:i]


def _abs_url(url: str, base: str = "") -> str:
    """Make a URL absolute.

    - ``//host/path`` -> ``https://host/path``
    - ``/path`` (root-relative) -> resolved against ``base`` origin
    """
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and base:
        parsed = urlparse(base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{url}"
    return url


def _image_ext_from_url(url: str, content_type: str = "") -> str:
    m = re.search(r"wx_fmt=([a-zA-Z0-9]+)", url)
    if m:
        ext = m.group(1).lower()
        return ".jpg" if ext in {"jpeg", "jpg"} else "." + ext
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}:
        return ext
    if content_type:
        m2 = re.search(r"image/(\w+)", content_type)
        if m2:
            ext = m2.group(1).lower()
            return ".jpg" if ext in {"jpeg", "jpg"} else "." + ext
    return ".jpg"


def _download_image(session: requests.Session, url: str, img_dir: Path, referer: str, timeout: int) -> Path | None:
    """下载单张图片到 img_dir，返回本地路径；失败返回 None。"""
    try:
        resp = session.get(url, headers={"Referer": referer}, timeout=timeout, stream=True)
        if resp.status_code != 200:
            return None
        ctype = resp.headers.get("Content-Type", "")
        key = hashlib.md5(url.encode()).hexdigest()[:12]
        ext = _image_ext_from_url(url, ctype)
        local_path = img_dir / f"{key}{ext}"
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        if local_path.stat().st_size == 0:
            local_path.unlink(missing_ok=True)
            return None
        return local_path
    except Exception:  # noqa: BLE001
        return None


def download_images(
    session: requests.Session,
    body_html: str,
    img_dir: Path,
    referer: str,
    timeout: int,
) -> tuple[str, list[dict[str, str]]]:
    """下载正文图片（data-src 懒加载优先，回退 src），返回 (替换后的 HTML, 图片清单)。"""
    img_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, str]] = []
    seen: set[str] = set()

    def _handle(url: str) -> str:
        url = _abs_url(url.strip(), referer)
        if not url or url in seen:
            return ""
        seen.add(url)
        local = _download_image(session, url, img_dir, referer, timeout)
        if local is None:
            images.append({"remote": url, "local": "", "error": "download failed"})
            return ""
        images.append({"remote": url, "local": str(local)})
        return f'<img src="images/{local.name}" alt="" data-done="1">'

    def repl_data_src(m: re.Match) -> str:
        replaced = _handle(m.group(2))
        return replaced or m.group(0)

    def repl_src(m: re.Match) -> str:
        tag = m.group(0)
        if 'data-done="1"' in tag or 'data-src=' in tag:
            return tag
        replaced = _handle(m.group(2))
        return replaced or tag

    # 第一遍：含 data-src 的懒加载图
    body_html = re.sub(r'<img([^>]*?)\bdata-src="([^"]+)"([^>]*)>', repl_data_src, body_html)
    # 第二遍：无 data-src 的普通图（已替换的带 data-done 标记，跳过）
    body_html = re.sub(r'<img([^>]*?)\bsrc="([^"]+)"([^>]*)>', repl_src, body_html)
    return body_html, images


def html_to_markdown(body_html: str) -> str:
    """把正文 HTML 粗略转换为纯文本 Markdown。"""
    text = body_html
    text = re.sub(r'<img[^>]*\bsrc="([^"]+)"[^>]*>', r'![](\1)', text)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = re.sub(r"</(p|div|section|h[1-6]|blockquote|li|br)>", "\n", text, flags=re.I)
    text = re.sub(r"<(p|div|section|h[1-6]|blockquote|li|br)[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{max-width:820px;margin:0 auto;padding:24px 16px;font:16px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222}}
h1{{font-size:24px;line-height:1.4;margin:0 0 8px}}
.meta{{color:#888;font-size:14px;margin:0 0 24px}}
#js_content img{{max-width:100%;height:auto;display:block;margin:12px auto}}
#js_content section,#js_content p{{margin:0 0 14px}}
a{{color:#0a7}}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<div id="js_content">{body}</div>
</body>
</html>
"""


def article_command(args: argparse.Namespace) -> int:
    url = (args.url or "").strip()
    if not url.startswith("http"):
        raise SystemExit("invalid article URL")
    out_root = Path(args.out_root).expanduser() if args.out_root else default_out_root()
    timeout = max(10, getattr(args, "timeout", 60) or 60)
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    try:
        resp = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"fetch failed: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"fetch failed HTTP {resp.status_code}")
        return 1
    page = resp.text

    title = _extract(r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', page)
    if not title:
        title = _extract(r'<h1[^>]*class="rich_media_title"[^>]*>(.*?)</h1>', page)
    title = re.sub(r"<[^>]+>", "", title).strip() or (getattr(args, "title", "") or "微信文章")
    author = _extract(r'<span[^>]*id="js_author_name"[^>]*>(.*?)</span>', page)
    if not author:
        author = _extract(r'<a[^>]*id="js_name"[^>]*>(.*?)</a>', page)
    author = re.sub(r"<[^>]+>", "", author).strip()
    publish_time = _extract(r'<em[^>]*id="publish_time"[^>]*>(.*?)</em>', page)
    if not publish_time:
        ts = _extract(r'var\s+ct\s*=\s*"(\d+)"', page)
        if ts:
            try:
                publish_time = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OverflowError, OSError):
                publish_time = ""

    body = _extract_element_by_id(page, "js_content")
    out_dir = unique_run_dir(getattr(args, "title", "") or title or "wechat-article", root=out_root)
    images: list[dict[str, str]] = []
    notes: list[str] = []
    if body:
        if getattr(args, "no_images", False):
            body = re.sub(r'<img([^>]*?)\bdata-src="([^"]+)"', r'<img\1 src="\2"', body)
            notes.append("--no-images: 图片保持远程链接（需联网查看）")
        else:
            body, images = download_images(session, body, out_dir / "images", url, timeout)
            failed = [im for im in images if not im.get("local")]
            if failed:
                notes.append(f"{len(failed)} 张图片下载失败（正文仍可读）")
    else:
        notes.append("未找到正文（文章可能已删除、需关注/登录、或命中反爬页）")

    meta = " ｜ ".join(p for p in [author, publish_time] if p) or url
    html_doc = PAGE_TEMPLATE.format(
        title=html.escape(title or "微信文章"),
        meta=html.escape(meta),
        body=body or "<p>（正文不可用，可能已删除或需登录）</p>",
    )
    (out_dir / "index.html").write_text(html_doc, encoding="utf-8")
    md_text = f"# {title}\n\n> {meta}\n\n{html_to_markdown(body) if body else ''}".strip() + "\n"
    (out_dir / "article.md").write_text(md_text, encoding="utf-8")

    report = {
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": url,
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "html": str(out_dir / "index.html"),
        "markdown": str(out_dir / "article.md"),
        "images": images,
        "notes": notes,
    }
    (out_dir / "article-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rep_lines = [
        f"# article-archiver 报告 — {title}",
        "",
        f"- 来源：{url}",
        f"- 作者：{author}",
        f"- 发布时间：{publish_time}",
        f"- 图片：{len(images)}（成功 {sum(1 for i in images if i.get('local'))}）",
        f"- 说明：{'；'.join(notes) if notes else '无'}",
        "",
    ]
    (out_dir / "article-report.md").write_text("\n".join(rep_lines), encoding="utf-8")

    print(out_dir)
    print(out_dir / "index.html")
    print(out_dir / "article.md")
    print(out_dir / "article-report.json")
    return 0 if body else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="微信公众号文章离线保存")
    p.add_argument("url", help="mp.weixin.qq.com 文章链接")
    p.add_argument("--out-root", default="", help="输出根目录（默认 WECHAT_ARTICLE_OUT 或 ~/Documents）")
    p.add_argument("--title", default="", help="覆盖标题（用于目录名）")
    p.add_argument("--timeout", type=int, default=60, help="单请求超时秒数（默认60）")
    p.add_argument("--no-images", action="store_true", help="不下载图片，保留远程链接")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return article_command(args)


if __name__ == "__main__":
    raise SystemExit(main())