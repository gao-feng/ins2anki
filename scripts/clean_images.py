#!/usr/bin/env python3
"""Drop the low-resolution preview copies in an already-synced tree.

Xiaohongshu lists every note image twice — the full ``!nd_dft_...`` file and a
``!nd_prv_...`` preview that has the *same pixel dimensions* but is roughly five
times smaller in bytes (measured: 21 KB vs 122 KB median) — and yt-dlp's image
pass saved both with ``--write-all-thumbnails``. Items synced before the
downloader learned to keep only the full-size variant therefore hold two files
per image.

This walks an existing tree, pairs the files back up through each item's
``*.info.json``, keeps the best variant of every image and quarantines the rest
into ``<item>/_previews/`` (or deletes them with ``--delete``). Every scanned
item's ``manifest.json`` is then repointed at the files that are actually on
disk, which also repairs manifests written under an older checkout path.

    python3 scripts/clean_images.py --root xhs-saved --dry-run
    python3 scripts/clean_images.py --root xhs-saved
    python3 scripts/clean_images.py --root xhs-saved --delete

Only files this can pair through a thumbnail id are touched: an item without an
info json, or one whose payload does not look like a list of image variants, is
reported and left alone.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_media import collect_media, partition_image_files  # noqa: E402


#: Where the previews go when they are not deleted outright.
QUARANTINE_DIR = "_previews"

SKIP_DIRS = {QUARANTINE_DIR, "__pycache__"}


def item_dirs(root: Path) -> list[Path]:
    """Every directory under ``root`` that holds a yt-dlp info json."""
    found: dict[Path, None] = {}
    for info in sorted(root.rglob("*.info.json")):
        relative = info.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        found[info.parent] = None
    return sorted(found)


def thumbnails_of(directory: Path) -> list[dict]:
    """Merge the ``thumbnails`` lists of every info json in one item."""
    entries: list[dict] = []
    for info in sorted(directory.glob("*.info.json")):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("thumbnails"), list):
            entries.extend(
                entry for entry in data["thumbnails"] if isinstance(entry, dict)
            )
    return entries


def rewrite_manifest(directory: Path) -> bool:
    """Point ``manifest.json`` at the files that are still on disk.

    Returns ``True`` when the file changed, so a manifest written under a
    different checkout path (this repo was renamed once) is repointed too.
    """
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    updated = dict(data)
    updated["media"] = collect_media(directory)
    if updated == data:
        return False
    manifest.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def clean(
    root: Path,
    delete: bool = False,
    dry_run: bool = False,
    log=lambda _message: None,
) -> dict:
    """Quarantine or delete the previews under ``root`` and report the tally."""
    summary = {
        "root": str(root),
        "action": "delete" if delete else "quarantine",
        "dry_run": dry_run,
        "items_scanned": 0,
        "items_changed": 0,
        "manifests_rewritten": 0,
        "files_removed": 0,
        "bytes_removed": 0,
        "failures": [],
    }
    for directory in item_dirs(root):
        summary["items_scanned"] += 1
        try:
            _keep, previews = partition_image_files(directory, thumbnails_of(directory))
        except OSError as exc:
            summary["failures"].append({"dir": str(directory), "error": str(exc)})
            continue
        if not previews:
            # Nothing to drop, but the manifest may still point at a checkout
            # path from before this repo was renamed.
            if not dry_run and rewrite_manifest(directory):
                summary["manifests_rewritten"] += 1
            continue

        removed = 0
        freed = 0
        for path in previews:
            try:
                size = path.stat().st_size
            except OSError as exc:
                summary["failures"].append({"file": str(path), "error": str(exc)})
                continue
            if dry_run:
                log(f"would remove {path}")
                removed += 1
                freed += size
                continue
            try:
                if delete:
                    path.unlink()
                else:
                    quarantine = directory / QUARANTINE_DIR
                    quarantine.mkdir(exist_ok=True)
                    shutil.move(str(path), str(quarantine / path.name))
            except OSError as exc:
                summary["failures"].append({"file": str(path), "error": str(exc)})
                continue
            log(f"removed {path}")
            removed += 1
            freed += size

        if removed:
            summary["items_changed"] += 1
            summary["files_removed"] += removed
            summary["bytes_removed"] += freed
        if not dry_run and rewrite_manifest(directory):
            summary["manifests_rewritten"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove the Xiaohongshu preview copies from a synced tree"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would go, without touching a file",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help=f"unlink the previews instead of moving them to {QUARANTINE_DIR}/",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.expanduser()
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}"}), file=sys.stderr)
        return 2

    log = (lambda _message: None) if args.quiet else (
        lambda message: print(message, file=sys.stderr)
    )
    summary = clean(root.resolve(), delete=args.delete, dry_run=args.dry_run, log=log)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
