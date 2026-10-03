#!/usr/bin/env python3
"""Incrementally sync Instagram saved posts to local storage.

The script can discover posts from an authenticated saved-collection URL via
yt-dlp, or consume a newline-delimited URL file. Completed posts are skipped,
failed posts are retried on later runs, and state is written atomically.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POST_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
IGNORED_MEDIA_SUFFIXES = {".json", ".description", ".part", ".ytdl"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(value: str) -> tuple[str, str] | None:
    match = POST_RE.search(value.strip())
    if not match:
        return None
    code = match.group(1)
    return code, f"https://www.instagram.com/p/{code}/"


def unique_urls(values: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_url(value)
        if normalized and normalized[0] not in seen:
            seen.add(normalized[0])
            result.append(normalized)
    return result


def urls_from_file(path: Path) -> list[tuple[str, str]]:
    return unique_urls(path.read_text(encoding="utf-8").splitlines())


def urls_from_collection(
    collection_url: str,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> list[tuple[str, str]]:
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
    urls = unique_urls(candidates)
    if not urls:
        raise RuntimeError(
            "the collection returned no post URLs; export visible post URLs and use --urls-file"
        )
    return urls


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "items": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise RuntimeError(f"invalid state file: {path}")
    return state


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def valid_download(directory: Path) -> bool:
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    media = data.get("media")
    if not isinstance(media, list) or not media:
        return False
    for raw in media:
        path = Path(raw)
        if not path.is_absolute():
            path = directory / path
        if path.is_file() and path.stat().st_size > 0:
            return True
    # Support older manifests whose paths moved together with their directory.
    return any(
        path.is_file()
        and path.stat().st_size > 0
        and path.suffix.lower() not in IGNORED_MEDIA_SUFFIXES
        and not path.name.endswith(".info.json")
        for path in directory.iterdir()
    )


def run_download(
    downloader: Path,
    url: str,
    output_dir: Path,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> tuple[bool, str]:
    cmd = [sys.executable, str(downloader), url, "--output-dir", str(output_dir)]
    if cookies:
        cmd += ["--cookies", str(cookies.resolve())]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    output = "\n".join(
        value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
    )
    if completed.returncode == 0 and valid_download(output_dir):
        return True, output
    return False, output or f"downloader exited with code {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally download an Instagram saved collection"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--collection-url")
    source.add_argument("--urls-file", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Maximum downloads for this run")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_file = (args.state_file or output_dir / "sync-state.json").resolve()
    downloader = Path(__file__).with_name("download_instagram.py")

    if args.urls_file:
        discovered = urls_from_file(args.urls_file.resolve())
        source_label = str(args.urls_file.resolve())
    else:
        discovered = urls_from_collection(
            args.collection_url, args.cookies, args.cookies_from_browser
        )
        source_label = args.collection_url

    state = load_state(state_file)
    items: dict[str, Any] = state["items"]
    discovered_at = now_iso()
    for code, url in discovered:
        item = items.setdefault(code, {"url": url, "attempts": 0})
        item["url"] = url
        item["last_seen_at"] = discovered_at
        directory = output_dir / code
        if valid_download(directory):
            item.update({
                "status": "completed",
                "output_dir": str(directory),
                "completed_at": item.get("completed_at", discovered_at),
            })
            item.pop("error", None)
        elif item.get("status") == "completed":
            item["status"] = "incomplete"
            item.pop("completed_at", None)

    pending: list[tuple[str, str]] = []
    for code, url in discovered:
        status = items[code].get("status")
        if status == "completed":
            continue
        if status == "failed" and args.no_retry_failed:
            continue
        pending.append((code, url))
    if args.limit is not None:
        pending = pending[: args.limit]

    state.update({
        "version": 1,
        "source": source_label,
        "output_dir": str(output_dir),
        "last_scan_at": discovered_at,
        "last_discovered_count": len(discovered),
    })
    write_json_atomic(state_file, state)

    if args.dry_run:
        print(json.dumps({
            "discovered": len(discovered),
            "pending": len(pending),
            "pending_urls": [url for _, url in pending],
            "state_file": str(state_file),
        }, ensure_ascii=False, indent=2))
        return 0

    completed_count = 0
    failed_count = 0
    for index, (code, url) in enumerate(pending, 1):
        print(f"[{index}/{len(pending)}] {code}", flush=True)
        item = items[code]
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_attempt_at"] = now_iso()
        ok, detail = run_download(
            downloader,
            url,
            output_dir / code,
            args.cookies,
            args.cookies_from_browser,
        )
        if ok:
            completed_count += 1
            item.update({
                "status": "completed",
                "output_dir": str(output_dir / code),
                "completed_at": now_iso(),
            })
            item.pop("error", None)
        else:
            failed_count += 1
            item.update({"status": "failed", "error": detail[-4000:]})
        write_json_atomic(state_file, state)

    total_completed = sum(1 for item in items.values() if item.get("status") == "completed")
    total_failed = sum(1 for item in items.values() if item.get("status") == "failed")
    print(json.dumps({
        "discovered": len(discovered),
        "attempted": len(pending),
        "downloaded": completed_count,
        "failed": failed_count,
        "total_completed": total_completed,
        "total_failed": total_failed,
        "state_file": str(state_file),
    }, ensure_ascii=False, indent=2))
    return 2 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
