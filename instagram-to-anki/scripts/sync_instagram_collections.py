#!/usr/bin/env python3
"""Create per-collection directories and incrementally sync browser discoveries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


INVALID_PATH_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
WHITESPACE = re.compile(r"\s+")


def safe_directory_name(value: str, fallback: str = "collection") -> str:
    name = unicodedata.normalize("NFKC", value)
    name = INVALID_PATH_CHARS.sub("_", name)
    name = WHITESPACE.sub(" ", name).strip(" .")
    return name[:100] or fallback


def collection_suffix(url: str) -> str:
    match = re.search(r"/saved/_/([^/?#]+)", url)
    return match.group(1)[-8:] if match else ""


def load_inventory(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read inventory {path}: {exc}") from exc
    collections = payload.get("collections") if isinstance(payload, dict) else None
    if not isinstance(collections, list):
        raise RuntimeError("inventory must contain a collections array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(collections, 1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise RuntimeError(f"collection {index} must have a name")
        posts = item.get("posts")
        if not isinstance(posts, list) or not all(isinstance(url, str) for url in posts):
            raise RuntimeError(f"collection {index} must have a posts array")
        result.append(item)
    return result


def assign_directories(collections: list[dict[str, Any]]) -> list[str]:
    assigned: list[str] = []
    used: set[str] = set()
    for index, item in enumerate(collections, 1):
        base = safe_directory_name(item["name"], f"collection-{index}")
        candidate = base
        if candidate.casefold() in used:
            suffix = collection_suffix(str(item.get("url", ""))) or str(index)
            candidate = safe_directory_name(f"{base}-{suffix}")
        counter = 2
        while candidate.casefold() in used:
            candidate = safe_directory_name(f"{base}-{counter}")
            counter += 1
        used.add(candidate.casefold())
        assigned.append(candidate)
    return assigned


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a browser-discovered Instagram collection inventory"
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--limit", type=int, help="Maximum downloads per collection")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    collections = load_inventory(args.inventory.resolve())
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    sync_script = Path(__file__).with_name("sync_instagram_saved.py")
    directories = assign_directories(collections)
    failures = 0

    for index, (item, directory_name) in enumerate(zip(collections, directories), 1):
        destination = root / directory_name
        print(
            f"[{index}/{len(collections)}] {item['name']} -> {destination}",
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
