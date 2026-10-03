#!/usr/bin/env python3
"""Download one post from a supported platform and emit a stable manifest.

The downloader probes the item first, then chooses a strategy:

* **video** — the item exposes real video formats; download them with yt-dlp.
* **images** — a Xiaohongshu note with no video formats but a populated image
  list; download every image with ``--write-all-thumbnails``.

The probe matters: Douyin's ``thumbnails`` are video *covers*, not note images,
so treating a cover as the item's content would report a false success. Douyin
image notes (``/note/<id>``) are therefore reported as unsupported instead of
being downloaded as a single cover image.

Manifest::

    {
      "source_url": "...",
      "platform": "xiaohongshu",
      "kind": "video" | "images",
      "media": ["/abs/path/clip.mp4"],
      "metadata": [{"id": ..., "title": ..., ...}]
    }
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platforms import PLATFORMS, detect_platform  # noqa: E402


IGNORED_MEDIA_SUFFIXES = {".json", ".description", ".part", ".ytdl"}
URL_PLACEHOLDER = "%(id)s_%(title).80B.%(ext)s"


def ytdlp_executable(parser: argparse.ArgumentParser) -> str:
    exe = shutil.which("yt-dlp")
    if not exe:
        parser.error("yt-dlp is not installed or not on PATH")
    return exe


def ytdlp_args(
    cookies: Path | None, cookies_from_browser: str | None
) -> list[str]:
    args: list[str] = []
    if cookies:
        args += ["--cookies", str(cookies.resolve())]
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    return args


def collect_media(out: Path) -> list[str]:
    return [
        str(path)
        for path in sorted(out.iterdir())
        if path.is_file()
        and path.suffix.lower() not in IGNORED_MEDIA_SUFFIXES
        and not path.name.endswith(".info.json")
    ]


def probe(
    exe: str,
    url: str,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[dict | None, str]:
    """Fetch metadata without downloading media.

    Returns ``(payload, detail)``. ``payload`` is ``None`` when the probe
    itself failed, which lets the caller fall back to a plain download attempt.
    ``detail`` holds stderr only, so a successful probe does not echo the whole
    metadata blob into later error messages.
    """
    cmd = [
        exe,
        "--dump-single-json",
        "--skip-download",
        "--ignore-no-formats-error",
        "--no-warnings",
    ] + ytdlp_args(cookies, cookies_from_browser) + [url]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    stderr = completed.stderr.strip()
    if completed.returncode:
        return None, "\n".join(
            value for value in (stderr, completed.stdout.strip()) if value
        )
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, stderr
    return None, stderr


#: Codec order that keeps the result playable in macOS's own players.
AVC_FIRST_FORMAT = (
    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]"
    "/b[vcodec^=avc1]"
    "/bv*[ext=mp4]+ba[ext=m4a]"
    "/b[ext=mp4]"
    "/b"
)


def download_video(
    exe: str,
    url: str,
    out: Path,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[int, str]:
    cmd = [
        exe,
        "--no-playlist",
        # QuickTime/AVFoundation decodes H.264 and HEVC only: a VP9 or AV1
        # stream (yt-dlp's default preference on many sites) downloads fine but
        # will not open in QuickTime Player, Photos or Quick Look. Prefer
        # H.264 + AAC, then any mp4, and let yt-dlp merge into mp4.
        "--format",
        AVC_FIRST_FORMAT,
        "--merge-output-format",
        "mp4",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--output",
        str(out / URL_PLACEHOLDER),
    ] + ytdlp_args(cookies, cookies_from_browser) + [url]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    detail = "\n".join(
        value.strip() for value in (completed.stderr, completed.stdout) if value.strip()
    )
    return completed.returncode, detail


def download_images(
    exe: str,
    url: str,
    out: Path,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[int, str]:
    cmd = [
        exe,
        "--skip-download",
        "--ignore-no-formats-error",
        "--write-info-json",
        "--write-all-thumbnails",
    ]
    if shutil.which("ffmpeg"):
        cmd += ["--convert-thumbnails", "jpg"]
    cmd += [
        "--output",
        str(out / URL_PLACEHOLDER),
    ] + ytdlp_args(cookies, cookies_from_browser) + [url]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    detail = "\n".join(
        value.strip() for value in (completed.stderr, completed.stdout) if value.strip()
    )
    return completed.returncode, detail


def build_manifest(out: Path, url: str, platform: str, kind: str) -> dict:
    metadata = []
    for path in sorted(out.glob("*.info.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
            continue
        metadata.append({
            "id": data.get("id"),
            "title": data.get("title"),
            "description": data.get("description"),
            "webpage_url": data.get("webpage_url") or url,
            "duration": data.get("duration"),
            "timestamp": data.get("timestamp"),
            "uploader": data.get("uploader") or data.get("channel"),
            "info_json": str(path),
        })
    return {
        "source_url": url,
        "platform": platform,
        "kind": kind,
        "media": collect_media(out),
        "metadata": metadata,
    }


def run(
    argv: list[str] | None = None, forced_platform: str | None = None
) -> int:
    """Download one item. ``forced_platform`` pins a thin per-platform wrapper."""
    parser = argparse.ArgumentParser(
        description="Download one Instagram/Xiaohongshu/Douyin post"
    )
    parser.add_argument("url")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--platform",
        choices=("auto",) + PLATFORMS,
        default="auto",
        help="Override platform detection (default: auto)",
    )
    parser.add_argument("--cookies", type=Path)
    parser.add_argument(
        "--cookies-from-browser",
        help="Read login cookies directly with yt-dlp (for example: chrome or firefox)",
    )
    args = parser.parse_args(argv)

    exe = ytdlp_executable(parser)
    detected = detect_platform(args.url)
    platform = forced_platform or (
        args.platform if args.platform != "auto" else (detected or "unknown")
    )

    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    payload, probe_detail = probe(
        exe, args.url, args.cookies, args.cookies_from_browser
    )
    formats = payload.get("formats") if isinstance(payload, dict) else None
    thumbnails = payload.get("thumbnails") if isinstance(payload, dict) else None
    has_video = isinstance(formats, list) and bool(formats)
    has_images = isinstance(thumbnails, list) and bool(thumbnails)

    if payload is None:
        # The probe can fail on extractors that only support a straight
        # download; let the media pass decide.
        kind = "video"
    elif has_video:
        kind = "video"
    elif platform == "xiaohongshu" and has_images:
        kind = "images"
    else:
        reason = "no video formats were found"
        if platform == "douyin":
            reason += (
                "; Douyin image notes (/note/<id>) are not supported by yt-dlp"
                " and a video cover is not the note content"
            )
        print(
            json.dumps(
                {
                    "error": reason,
                    "platform": platform,
                    "url": args.url,
                    "probe": probe_detail[-2000:],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    if kind == "images":
        returncode, detail = download_images(
            exe, args.url, out, args.cookies, args.cookies_from_browser
        )
    else:
        returncode, detail = download_video(
            exe, args.url, out, args.cookies, args.cookies_from_browser
        )

    media = collect_media(out)
    if returncode or not media:
        print(detail or f"yt-dlp exited with code {returncode}", file=sys.stderr)
        return returncode or 1

    manifest = build_manifest(out, args.url, platform, kind)
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(manifest_path)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
