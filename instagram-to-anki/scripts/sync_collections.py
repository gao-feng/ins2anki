#!/usr/bin/env python3
"""Sync every collection in a browser inventory, one directory each.

Each collection gets a filesystem-safe directory and its own incremental
``sync-state.json``, so re-running the command after refreshing the inventory
only downloads new or incomplete posts.

Inventory format (produced by ``scripts/browser/export_collection.js``)::

    {
      "collections": [
        {
          "name": "英语",
          "platform": "xiaohongshu",
          "url": "https://www.xiaohongshu.com/user/profile/<uid>",
          "posts": ["https://www.xiaohongshu.com/explore/<note_id>?xsec_token=..."]
        }
      ]
    }

``platform`` is optional and overrides ``--platform`` for that collection.
``url`` is optional metadata used only to disambiguate duplicate names.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_common  # noqa: E402
from platforms import PLATFORMS, detect_platform  # noqa: E402


def collection_platform(item: dict, default: str | None) -> str | None:
    """Resolve the platform for one inventory entry."""
    declared = item.get("platform")
    if isinstance(declared, str) and declared in PLATFORMS:
        return declared
    if default:
        return default
    for post in item.get("posts", []):
        detected = detect_platform(post)
        if detected:
            return detected
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a browser-discovered collection inventory"
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--platform",
        choices=("auto",) + PLATFORMS,
        default="auto",
        help="Platform for entries that do not declare one",
    )
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--limit", type=int, help="Maximum downloads per collection")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        collections = sync_common.load_inventory(args.inventory.resolve())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    default_platform = None if args.platform == "auto" else args.platform
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    sync_script = Path(__file__).with_name("sync_saved.py")
    directories = sync_common.assign_directories(collections)
    failures = 0

    for index, (item, directory_name) in enumerate(zip(collections, directories), 1):
        destination = root / directory_name
        platform = collection_platform(item, default_platform)
        print(
            f"[{index}/{len(collections)}] {item['name']} "
            f"({platform or 'auto'}) -> {destination}",
            flush=True,
        )
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            handle.write("\n".join(item["posts"]))
            handle.write("\n")
            urls_file = Path(handle.name)
        try:
            cmd = [
                sys.executable,
                str(sync_script),
                "--urls-file",
                str(urls_file),
                "--output-dir",
                str(destination),
            ]
            if platform:
                cmd += ["--platform", platform]
            if args.cookies:
                cmd += ["--cookies", str(args.cookies.resolve())]
            if args.cookies_from_browser:
                cmd += ["--cookies-from-browser", args.cookies_from_browser]
            if args.limit is not None:
                cmd += ["--limit", str(args.limit)]
            if args.no_retry_failed:
                cmd.append("--no-retry-failed")
            if args.dry_run:
                cmd.append("--dry-run")
            completed = subprocess.run(cmd)
            if completed.returncode:
                failures += 1
        finally:
            urls_file.unlink(missing_ok=True)

    print(json.dumps({
        "collections": len(collections),
        "failed_collections": failures,
        "output_dir": str(root),
        "directories": directories,
    }, ensure_ascii=False, indent=2))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
