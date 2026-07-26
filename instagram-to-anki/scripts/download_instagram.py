#!/usr/bin/env python3
"""Download an Instagram post with yt-dlp and emit a stable manifest."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cookies", type=Path)
    args = parser.parse_args()

    exe = shutil.which("yt-dlp")
    if not exe:
        parser.error("yt-dlp is not installed or not on PATH")

    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "--no-playlist",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--output",
        str(out / "%(id)s_%(title).80B.%(ext)s"),
    ]
    if args.cookies:
        cmd += ["--cookies", str(args.cookies.resolve())]
    cmd.append(args.url)
    completed = subprocess.run(cmd, text=True)
    if completed.returncode:
        return completed.returncode

    info_files = sorted(out.glob("*.info.json"))
    metadata = []
    for path in info_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            metadata.append({
                "id": data.get("id"),
                "title": data.get("title"),
                "description": data.get("description"),
                "webpage_url": data.get("webpage_url") or args.url,
                "duration": data.get("duration"),
                "timestamp": data.get("timestamp"),
                "uploader": data.get("uploader"),
                "info_json": str(path),
            })
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)

    ignored = {".json", ".description", ".part", ".ytdl"}
    media = [str(p) for p in sorted(out.iterdir()) if p.is_file() and p.suffix.lower() not in ignored and not p.name.endswith(".info.json")]
    manifest = {"source_url": args.url, "media": media, "metadata": metadata}
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
