#!/usr/bin/env python3
"""Download an Instagram post with yt-dlp and emit a stable manifest.

This is a thin Instagram-pinned wrapper around the shared ``download_media.py``.
New code should prefer ``download_media.py``; this entry point is kept so
existing commands and the README keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import download_media  # noqa: E402


def main() -> int:
    return download_media.run(sys.argv[1:], forced_platform="instagram")


if __name__ == "__main__":
    raise SystemExit(main())
