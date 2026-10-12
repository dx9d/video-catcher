#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Run yt-dlp arguments received over stdin so sensitive headers do not appear in OS argv."""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        argv = payload.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError("invalid argv payload")
        import yt_dlp
        try:
            result = yt_dlp.main(argv)
            return int(result or 0)
        except SystemExit as exc:
            code = exc.code
            return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except Exception as exc:  # noqa: BLE001
        print(f"video-catcher yt-dlp worker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
