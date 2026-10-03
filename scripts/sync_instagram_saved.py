#!/usr/bin/env python3
"""Incrementally sync Instagram saved posts to local storage.

This is a thin Instagram-pinned wrapper around the shared engine. New code
should prefer ``sync_saved.py --platform instagram``; this entry point is kept
so existing commands and the README keep working.

The script can discover posts from an authenticated saved-collection URL via
yt-dlp, or consume a newline-delimited URL file. Completed posts are skipped,
failed posts are retried on later runs, and state is written atomically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_common  # noqa: E402
import sync_saved  # noqa: E402
from platforms import normalize, normalize_many  # noqa: E402


# Re-exported so existing callers and tests keep working unchanged.
from sync_common import (  # noqa: E402,F401
    load_state,
    now_iso,
    valid_download,
    write_json_atomic,
)


def normalize_url(value: str) -> tuple[str, str] | None:
    """Return ``(shortcode, canonical_url)`` for an Instagram post URL."""
    return normalize(value, "instagram")


def unique_urls(values: list[str]) -> list[tuple[str, str]]:
    """Normalize and deduplicate Instagram URLs into ``(shortcode, url)``."""
    return [
        (item_id, url)
        for _platform, item_id, url in normalize_many(values, "instagram")
    ]


def urls_from_file(path: Path) -> list[tuple[str, str]]:
    return unique_urls(path.read_text(encoding="utf-8").splitlines())


def urls_from_collection(
    collection_url: str,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> list[tuple[str, str]]:
    return [
        (item_id, url)
        for _platform, item_id, url in sync_saved.enumerate_collection(
            collection_url, cookies, cookies_from_browser, "instagram"
        )
    ]


def run_download(
    downloader: Path,
    url: str,
    output_dir: Path,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[bool, str]:
    return sync_common.run_download(
        downloader,
        url,
        output_dir,
        cookies,
        cookies_from_browser,
        platform="instagram",
    )


def main() -> int:
    return sync_saved.run(sys.argv[1:], platform="instagram", download_fn=run_download)


if __name__ == "__main__":
    raise SystemExit(main())
