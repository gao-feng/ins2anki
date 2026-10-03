#!/usr/bin/env python3
"""Incrementally sync a saved/favorites collection from any supported platform.

Neither Xiaohongshu nor Douyin exposes a machine-readable favorites feed, so a
collection is described by a browser inventory: run
``scripts/browser/export_collection.js`` on the logged-in favorites page, save
the printed JSON, then pass its per-collection ``posts`` array via
``--urls-file`` (or the whole file via ``sync_collections.py``).

Instagram saved collections can additionally be enumerated directly from a
authenticated collection URL with ``--collection-url``.

Completed items are skipped while their media is intact, failures are retried
on later runs, and ``sync-state.json`` is written atomically.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_common  # noqa: E402
from platforms import (  # noqa: E402
    PLATFORMS,
    detect_platform,
    normalize_many,
    resolve_short_links,
)


def _instagram_candidates(payload: object) -> list[str]:
    candidates: list[str] = []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("webpage_url", "original_url", "url"):
                value = entry.get(key)
                if isinstance(value, str):
                    candidates.append(value)
            post_id = entry.get("id")
            if isinstance(post_id, str):
                candidates.append(f"https://www.instagram.com/p/{post_id}/")
    if isinstance(payload, dict):
        for key in ("webpage_url", "original_url"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)
    return candidates


def enumerate_collection(
    collection_url: str,
    cookies: Path | None,
    cookies_from_browser: str | None,
    platform: str | None = None,
) -> list[tuple[str, str, str]]:
    """Discover posts from a collection URL.

    Only Instagram saved collections are enumerable by yt-dlp; the other
    platforms raise ``RuntimeError`` pointing at the browser exporter.
    """
    resolved = platform or detect_platform(collection_url) or ""
    if resolved != "instagram":
        label = resolved or "this platform"
        raise RuntimeError(
            f"{label} does not expose an enumerable favorites feed to yt-dlp. "
            "Export the collection with scripts/browser/export_collection.js "
            "and pass the URLs with --urls-file."
        )
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp is not installed or not on PATH")
    cmd = [exe, "--flat-playlist", "--dump-single-json", "--no-warnings"]
    if cookies:
        cmd += ["--cookies", str(cookies.resolve())]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(collection_url)
    completed = subprocess.run(cmd, text=True, capture_output=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"cannot enumerate saved collection: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid collection JSON") from exc

    urls = normalize_many(_instagram_candidates(payload), "instagram")
    if not urls:
        raise RuntimeError(
            "the collection returned no post URLs; export visible post URLs "
            "and use --urls-file"
        )
    return urls


def urls_from_file(
    path: Path, platform: str | None, resolve: bool = True
) -> list[tuple[str, str, str]]:
    values = path.read_text(encoding="utf-8").splitlines()
    if resolve:
        values = resolve_short_links([value for value in values if value.strip()])
    return normalize_many(values, platform)


def run_download(
    downloader: Path,
    url: str,
    output_dir: Path,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[bool, str]:
    """Download one item, letting the URL decide the platform."""
    return sync_common.run_download(
        downloader,
        url,
        output_dir,
        cookies,
        cookies_from_browser,
        platform=detect_platform(url),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Incrementally download a saved/favorites collection"
    )
    parser.add_argument(
        "--platform",
        choices=("auto",) + PLATFORMS,
        default="auto",
        help="Restrict and label the platform instead of detecting it per URL",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--collection-url",
        help="Instagram saved-collection URL (other platforms need --urls-file)",
    )
    source.add_argument("--urls-file", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Maximum downloads for this run")
    return parser


def run(
    argv: list[str] | None = None,
    platform: str | None = None,
    download_fn: sync_common.DownloadFn | None = None,
) -> int:
    """Run one sync pass.

    ``platform`` and ``download_fn`` let a thin per-platform wrapper reuse this
    CLI while pinning the platform and keeping its own mockable downloader.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    platform = platform or (None if args.platform == "auto" else args.platform)
    download_fn = download_fn or run_download
    try:
        if args.urls_file:
            discovered = urls_from_file(args.urls_file.resolve(), platform)
            source_label = str(args.urls_file.resolve())
        else:
            discovered = enumerate_collection(
                args.collection_url,
                args.cookies,
                args.cookies_from_browser,
                platform,
            )
            source_label = args.collection_url
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not discovered:
        print(
            "error: no supported post URLs were found; check the platform and "
            "the URLs in the inventory",
            file=sys.stderr,
        )
        return 2

    output_dir = args.output_dir.resolve()
    state_file = args.state_file or output_dir / "sync-state.json"
    downloader = Path(__file__).with_name("download_media.py")

    return sync_common.sync_items(
        discovered,
        output_dir=output_dir,
        state_file=state_file,
        downloader=downloader,
        download_fn=download_fn,
        source_label=source_label,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        retry_failed=not args.no_retry_failed,
        dry_run=args.dry_run,
        limit=args.limit,
    )


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
