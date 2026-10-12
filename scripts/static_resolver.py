# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from common import DEFAULT_USER_AGENT
from media_detector import looks_like_media, rank_candidates
from models import MediaCandidate

MEDIA_META_KEYS = {
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "twitter:player:stream",
}
MEDIA_ATTRS = {"src", "data-src", "data-video", "data-url"}
EXT_RE = re.compile(
    r"(?P<url>https?:(?:\\/\\/|//)[^\"'<>\s]+?\.(?:mp4|webm|mov|m4v|mkv|m3u8|mpd)(?:\?[^\"'<>\s]*)?)",
    re.I,
)
REL_RE = re.compile(
    r"(?P<url>(?:/|\.\.?/)[^\"'<>\s]+?\.(?:mp4|webm|mov|m4v|mkv|m3u8|mpd)(?:\?[^\"'<>\s]*)?)",
    re.I,
)


class _MediaHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"video", "source", "audio"}:
            for key in MEDIA_ATTRS:
                value = values.get(key, "").strip()
                if value:
                    self.urls.append((urljoin(self.base_url, value), f"<{tag} {key}>"))
        if tag == "meta":
            prop = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if prop in MEDIA_META_KEYS and content:
                self.urls.append((urljoin(self.base_url, content), f"meta:{prop}"))


def _normalize_script_url(value: str) -> str:
    return value.replace("\\/", "/").replace("\\u0026", "&")


def extract_candidates_from_html(html_text: str, base_url: str) -> list[MediaCandidate]:
    parser = _MediaHTMLParser(base_url)
    try:
        parser.feed(html_text)
    except Exception:
        pass
    found: list[MediaCandidate] = []
    for url, source in parser.urls:
        found.append(MediaCandidate(url=html_lib.unescape(url), source=f"static-html:{source}"))

    decoded = html_lib.unescape(html_text)
    for regex in (EXT_RE, REL_RE):
        for match in regex.finditer(decoded):
            raw = _normalize_script_url(match.group("url"))
            found.append(MediaCandidate(url=urljoin(base_url, raw), source="static-html:regex"))

    # Common JSON-encoded media keys without assuming a particular site schema.
    for key in (
        "contentUrl", "videoUrl", "playUrl", "playbackUrl", "hlsUrl", "dashUrl",
        "playAddr", "play_addr", "downloadAddr", "downloadUrl", "videoPlayUrl",
    ):
        pattern = re.compile(rf'[\"\']{re.escape(key)}[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', re.I)
        for match in pattern.finditer(decoded):
            raw = _normalize_script_url(match.group(1))
            if raw.startswith(("http://", "https://", "/", "./", "../")):
                found.append(MediaCandidate(url=urljoin(base_url, raw), source=f"static-html:json:{key}"))
    return rank_candidates(found)


def resolve_static(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 25,
) -> tuple[list[MediaCandidate], str]:
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    try:
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return [], f"static fetch failed: {str(exc)[:180]}"

    content_type = response.headers.get("Content-Type", "")
    if looks_like_media(response.url, content_type):
        candidate = MediaCandidate(
            url=response.url,
            source="static-response",
            content_type=content_type,
            content_length=int(response.headers.get("Content-Length") or 0),
            status=response.status_code,
            request_headers={"Referer": url, "User-Agent": DEFAULT_USER_AGENT},
            response_headers=dict(response.headers),
        )
        return rank_candidates([candidate]), "URL resolved directly to media"

    text = response.text[:5_000_000]
    candidates = extract_candidates_from_html(text, response.url)
    for item in candidates:
        item.request_headers.setdefault("Referer", response.url)
        item.request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    return candidates, f"static scan found {len(candidates)} candidate(s)"
