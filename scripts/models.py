# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SENSITIVE_HEADER_NAMES = {"cookie", "authorization", "proxy-authorization", "x-api-key"}
DRM_STATUSES = {"not-checked", "clear", "protected", "unknown"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if key.lower() in SENSITIVE_HEADER_NAMES:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


@dataclass
class MediaCandidate:
    url: str
    source: str
    kind: str = "unknown"
    content_type: str = ""
    content_length: int = 0
    status: int = 0
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    # drm is retained for compatibility; drm_status is the authoritative state.
    drm: bool = False
    drm_status: str = "not-checked"
    drm_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict, repr=False)
    response_headers: dict[str, str] = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["request_headers"] = _redact_headers(self.request_headers)
        data["response_headers"] = _redact_headers(self.response_headers)
        return data


@dataclass
class DownloadRecord:
    source_url: str
    strategy: str
    status: str = "failed"
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    note: str = ""
    selected_media_url: str = ""
    verified: bool | None = None
    verification_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SniffResult:
    page_url: str
    final_url: str
    candidates: list[MediaCandidate]
    cookies: list[dict[str, Any]] = field(default_factory=list, repr=False)
    note: str = ""
