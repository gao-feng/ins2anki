#!/usr/bin/env python3
"""Rename already-downloaded items after their titles.

Downloads land in ``<output>/<item id>`` behind ``<item id>_`` file prefixes.
New downloads are renamed to ``<title>`` as soon as the downloader writes the
title into the manifest; this applies the same rename to a tree that was synced
before that, and repoints the ``sync-state.json`` files so the next sync still
recognises every item as already done.

    python3 scripts/retitle_items.py --root xhs-saved --dry-run
    python3 scripts/retitle_items.py --root xhs-saved

Two items can share a title; the second one keeps its position and gets
``<title> (2)``. If the tree still holds preview images, run
``clean_images.py`` first: pairing those needs the numbers in the current file
names, and after the rename the cleaner skips items whose numbers are sequence
numbers rather than thumbnail ids.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_common  # noqa: E402


#: Directories that never hold a downloaded item.
SKIP_DIRS = {"_previews", "__pycache__"}


def item_directories(root: Path) -> list[Path]:
    """Every directory under ``root`` that holds an item manifest."""
    found: dict[Path, None] = {}
    for manifest in sorted(root.rglob("manifest.json")):
        relative = manifest.relative_to(root)
        if any(
            part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]
        ):
            continue
        found[manifest.parent] = None
    return sorted(found)


def update_states(root: Path, moved: dict[tuple[str, str], str]) -> int:
    """Repoint the state entries that described a renamed directory.

    An entry whose stored path no longer exists is also repointed at the
    ``<state directory>/<item id>`` directory that now holds it, which repairs
    state written under a previous checkout path.
    """
    updated = 0
    for state_file in sorted(root.rglob("sync-state.json")):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = state.get("items") if isinstance(state, dict) else None
        if not isinstance(items, dict):
            continue
        changed = False
        for item_id, item in items.items():
            if not isinstance(item, dict):
                continue
            stored = Path(str(item.get("output_dir") or ""))
            new_path = moved.get((str(stored.parent), stored.name)) or moved.get(
                (str(state_file.parent), str(item_id))
            )
            if not new_path and not stored.is_dir():
                beside = state_file.parent / str(item_id)
                if (beside / "manifest.json").is_file():
                    new_path = str(beside)
            if new_path and item.get("output_dir") != new_path:
                item["output_dir"] = new_path
                changed = True
        if changed:
            sync_common.write_json_atomic(state_file, state)
            updated += 1
    return updated


def retitle_tree(root: Path, dry_run: bool = False, log=lambda _message: None) -> dict:
    """Rename every titled item under ``root`` and report the tally."""
    summary = {
        "root": str(root),
        "dry_run": dry_run,
        "items_scanned": 0,
        "items_renamed": 0,
        "items_untouched": 0,
        "states_updated": 0,
        "failures": [],
    }
    moved: dict[tuple[str, str], str] = {}
    for directory in item_directories(root):
        summary["items_scanned"] += 1
        try:
            target = sync_common.retitle_item(directory, dry_run=dry_run)
        except OSError as exc:
            summary["failures"].append({"dir": str(directory), "error": str(exc)})
            continue
        if target == directory:
            summary["items_untouched"] += 1
            continue
        summary["items_renamed"] += 1
        moved[(str(directory.parent), directory.name)] = str(target)
        log(f"{directory.name}  ->  {target.name}")
    if not dry_run:
        summary["states_updated"] = update_states(root, moved)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Name saved items after their title (directory and files)"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the new names without renaming anything",
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
    summary = retitle_tree(root.resolve(), dry_run=args.dry_run, log=log)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
