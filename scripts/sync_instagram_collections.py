#!/usr/bin/env python3
"""Create per-collection directories and incrementally sync Instagram discoveries.

This is an Instagram-focused wrapper around the shared coordinator. New code
should prefer ``sync_collections.py``; this entry point is kept so existing
commands and the README keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_collections  # noqa: E402

# Re-exported so existing callers and tests keep working unchanged.
from platforms import collection_suffix  # noqa: E402,F401
from sync_common import (  # noqa: E402,F401
    assign_directories,
    load_inventory,
    safe_directory_name,
)


def main() -> int:
    return sync_collections.main()


if __name__ == "__main__":
    raise SystemExit(main())
