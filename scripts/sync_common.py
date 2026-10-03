#!/usr/bin/env python3
"""Shared incremental sync engine used by every platform.

The engine is deliberately platform-agnostic: callers hand it a normalized
``(platform, item_id, url)`` list plus a ``download_fn``, and it takes care of
state, validation, skipping, retries, limits and atomic writes.

State layout (``sync-state.json``)::

    {
      "version": 1,
      "source": "...",
      "output_dir": "...",
      "last_scan_at": "...",
      "last_discovered_count": 12,
      "items": {
        "<item_id>": {
          "url": "...",
          "platform": "xiaohongshu",
          "status": "completed" | "failed" | "incomplete",
          "attempts": 1,
          "last_seen_at": "...",
          "last_attempt_at": "...",
          "completed_at": "...",
          "error": "..."
        }
      }
    }

An item counts as completed only while its manifest still references a
non-empty media file. If the media disappears, the next run marks it
``incomplete`` and downloads it again.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from download_media import collect_media
from platforms import collection_suffix


IGNORED_MEDIA_SUFFIXES = {".json", ".description", ".part", ".ytdl"}
INVALID_PATH_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
WHITESPACE = re.compile(r"\s+")

DownloadFn = Callable[
    [Path, str, Path, "Path | None", "str | None"], "tuple[bool, str]"
]


def format_duration(seconds: float) -> str:
    """Render a duration as ``5m04s`` / ``1h12m`` for progress lines."""
    total = int(max(seconds, 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """Report whether a downloaded item still has usable media on disk."""
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
    platform: str | None = None,
) -> tuple[bool, str]:
    """Invoke a downloader script and validate the resulting directory."""
    cmd = [sys.executable, str(downloader), url, "--output-dir", str(output_dir)]
    if platform:
        cmd += ["--platform", platform]
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


def safe_directory_name(value: str, fallback: str = "collection") -> str:
    name = unicodedata.normalize("NFKC", value)
    name = INVALID_PATH_CHARS.sub("_", name)
    name = WHITESPACE.sub(" ", name).strip(" .")
    return name[:100] or fallback


def truncate_utf8(value: str, limit: int) -> str:
    """Cut a string to at most ``limit`` UTF-8 bytes, on a character boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore").strip()


def safe_title(value: str, fallback: str = "", limit: int = 120) -> str:
    """Filesystem-safe, byte-limited name built from an item title.

    The byte limit is what matters: APFS/HFS+ cap one name at 255 UTF-8 bytes,
    and a Chinese title costs three bytes per character, so cutting by character
    count would still produce a name the filesystem refuses.
    """
    name = unicodedata.normalize("NFKC", value)
    name = INVALID_PATH_CHARS.sub("_", name)
    name = WHITESPACE.sub(" ", name).strip(" .")
    shortened = truncate_utf8(name, limit)
    if shortened != name:
        # Cut at a word boundary when that keeps most of the title, so a long
        # Douyin caption does not end in the middle of a hashtag.
        boundary = shortened.rfind(" ")
        if boundary > len(shortened) // 2:
            shortened = shortened[:boundary]
    return shortened.strip(" .") or fallback


#: yt-dlp's placeholder caption for a post that carries no title of its own.
PLACEHOLDER_TITLE = re.compile(r"^Video by \S+$")


def manifest_title(directory: Path) -> str:
    """Read the item title the downloader recorded in its own manifest."""
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    metadata = data.get("metadata") if isinstance(data, dict) else None
    for entry in metadata or []:
        if isinstance(entry, dict):
            title = str(entry.get("title") or "").strip()
            if title and not PLACEHOLDER_TITLE.match(title):
                return title
    return ""


def item_directory(item: dict[str, Any], output_dir: Path, item_id: str) -> Path:
    """Where an item lives: the directory the state remembers, else its id.

    The remembered path is what makes a title-named item stable across runs;
    ``<output>/<item id>`` remains the landing zone for a new item and the
    fallback after a manifest was deleted by hand.
    """
    stored = item.get("output_dir")
    if stored:
        path = Path(str(stored))
        if path.is_dir():
            return path
    return output_dir / item_id


#: Compound suffixes yt-dlp writes next to the media itself.
COMPOUND_SUFFIXES = (".info.json", ".description")

#: Serialises directory renames, which two worker threads could race on.
RETITLE_LOCK = threading.Lock()


def split_download_name(name: str) -> tuple[str, str]:
    """Split a downloaded file name into ``(image index, suffix)``.

    Image notes number their files (``.0.jpg``, ``_3.jpg``) and videos do not,
    while the info/description sidecars carry a compound suffix that must keep
    its ``.info`` part.
    """
    for suffix in COMPOUND_SUFFIXES:
        if name.endswith(suffix):
            return "", suffix
    stem, dot, extension = name.rpartition(".")
    if not dot:
        return "", ""
    match = re.search(r"[._](\d+)$", stem)
    if match:
        return match.group(1), f".{extension}"
    return "", f".{extension}"


def plan_titles(directory: Path, title: str) -> dict[str, str]:
    """Map every file in ``directory`` to the name it should have.

    Numbered images keep their order and are renumbered ``.1``…``.N`` (already
    consecutive numbers are left alone, so the rename is idempotent) and every
    other file is named after the title. Collisions get a ``-2`` counter.
    """
    indexed: list[tuple[int, str, str]] = []
    plain: list[tuple[str, str]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        index, suffix = split_download_name(path.name)
        if index:
            indexed.append((int(index), path.name, suffix))
        else:
            plain.append((path.name, suffix))

    indexed.sort(key=lambda entry: (entry[0], entry[1]))
    renumber = [entry[0] for entry in indexed] != list(range(1, len(indexed) + 1))

    used: set[str] = set()

    def claim(base: str, suffix: str) -> str:
        candidate = f"{base}{suffix}"
        counter = 2
        while candidate in used or candidate == "manifest.json":
            candidate = f"{base}-{counter}{suffix}"
            counter += 1
        used.add(candidate)
        return candidate

    planned: dict[str, str] = {}
    for position, (index, name, suffix) in enumerate(indexed, 1):
        number = position if renumber else index
        planned[name] = claim(f"{title}.{number}", suffix)
    for name, suffix in plain:
        planned[name] = claim(title, suffix)
    return planned


def rewrite_manifest_paths(directory: Path, planned: dict[str, str]) -> None:
    """Repoint a manifest at the files after they were renamed."""
    manifest = directory / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    data["media"] = collect_media(directory)
    # Tell later passes that the file numbers are sequence numbers now, not the
    # thumbnail ids a downloader wrote, so nothing tries to pair them up again.
    data["file_names"] = "title"
    for entry in data.get("metadata") or []:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info_json")
        if info:
            old_name = Path(str(info)).name
            entry["info_json"] = str(directory / planned.get(old_name, old_name))
    write_json_atomic(manifest, data)


def is_title_named(directory: Path) -> bool:
    """Whether an item's files were already renamed after its title."""
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("file_names") == "title"


def retitle_item(directory: Path, dry_run: bool = False) -> Path:
    """Rename an item's files and folder after its title.

    Downloads land in ``<output>/<item id>`` behind ``<item id>_`` file prefixes:
    right for the state machine, unreadable in a file browser. Once the
    downloader has written the title into the manifest, both are named after it.
    An item without a title, or one that already carries the title, is left
    exactly as it is.
    """
    title = safe_title(manifest_title(directory))
    if not title or not directory.is_dir():
        return directory
    planned = plan_titles(directory, title)
    already_titled = directory.name == title
    if already_titled and all(name == target for name, target in planned.items()):
        # Already renamed, possibly before the marker existed: stamp it so the
        # preview cleaner knows not to pair these files up again.
        if not is_title_named(directory):
            rewrite_manifest_paths(directory, planned)
        return directory
    if dry_run:
        return directory if already_titled else _title_directory(directory.parent, title)

    moves = [
        (name, target) for name, target in planned.items() if name != target
    ]
    finished: list[tuple[str, str]] = []
    try:
        # Two phases: park every file under a temporary name first, so a target
        # can reuse a name another file is about to give up.
        for position, (name, _target) in enumerate(moves):
            (directory / name).rename(directory / f".retitle-{position}")
        for position, (name, target) in enumerate(moves):
            (directory / f".retitle-{position}").rename(directory / target)
            finished.append((name, target))
    except OSError:
        for name, target in reversed(finished):
            try:
                (directory / target).rename(directory / name)
            except OSError:
                pass
        return directory

    if not already_titled:
        with RETITLE_LOCK:
            target = _title_directory(directory.parent, title)
            try:
                directory = directory.rename(target)
            except OSError:
                return directory
    rewrite_manifest_paths(directory, planned)
    return directory


def _title_directory(parent: Path, title: str) -> Path:
    """Return the first free ``parent/<title>`` name (``(2)``, ``(3)``…)."""
    candidate = parent / title
    counter = 2
    while candidate.exists():
        candidate = parent / f"{title} ({counter})"
        counter += 1
    return candidate


def assign_directories(collections: list[dict[str, Any]]) -> list[str]:
    """Map inventory entries to unique, filesystem-safe directory names."""
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


def load_inventory(path: Path) -> list[dict[str, Any]]:
    """Validate a browser inventory and return its ``collections`` array."""
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
        platform = item.get("platform")
        if platform is not None and not isinstance(platform, str):
            raise RuntimeError(f"collection {index} platform must be a string")
        result.append(item)
    return result


def summarize_error(detail: str, limit: int = 160) -> str:
    """Pull the human-readable reason out of a downloader failure.

    ``download_media`` prints a pretty-printed JSON object whose first line is
    a bare ``{``, and yt-dlp leads its stderr with upgrade warnings before the
    ``ERROR:`` line that actually says what went wrong. Either way the first
    line of the raw text is useless, so dig one level deeper.
    """
    text = str(detail or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        reason = str(payload["error"]).strip()
        for line in reason.splitlines():
            if line.startswith("ERROR:"):
                return line[:limit]
        return (reason.splitlines()[0] if reason else text)[:limit]
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[:limit]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped not in {"{", "}"}:
            return stripped[:limit]
    return text[:limit]


def sync_items(
    discovered: list[tuple[str, str, str]],
    output_dir: Path,
    state_file: Path,
    downloader: Path,
    download_fn: DownloadFn,
    source_label: str,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
    retry_failed: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    stream: Any = None,
    jobs: int = 1,
) -> int:
    """Run one incremental sync pass and return a process exit code.

    ``discovered`` is a list of ``(platform, item_id, url)`` tuples, already
    normalized and deduplicated by the caller. Progress goes to stderr so
    stdout carries only the final JSON summary and stays pipeable.

    ``jobs`` downloads several items at once. Items are independent (one
    directory each) and signed CDN URLs are per-file, so parallelism is safe
    and is what closes the gap to a browser extension: a per-item extractor
    such as yt-dlp cannot be parallelised this cheaply because most of its time
    is spent in rate-limited API round trips rather than in the transfer.
    """
    out = stream or sys.stdout
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_file.resolve()

    state = load_state(state_file)
    items: dict[str, Any] = state["items"]
    discovered_at = now_iso()
    for platform, item_id, url in discovered:
        item = items.setdefault(item_id, {"url": url, "attempts": 0})
        item["url"] = url
        item["platform"] = platform
        item["last_seen_at"] = discovered_at
        directory = item_directory(item, output_dir, item_id)
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

    pending: list[tuple[str, str, str]] = []
    for platform, item_id, url in discovered:
        status = items[item_id].get("status")
        if status == "completed":
            continue
        if status == "failed" and not retry_failed:
            continue
        pending.append((platform, item_id, url))
    if limit is not None:
        pending = pending[:limit]

    state.update({
        "version": 1,
        "source": source_label,
        "output_dir": str(output_dir),
        "last_scan_at": discovered_at,
        "last_discovered_count": len(discovered),
    })
    write_json_atomic(state_file, state)

    if dry_run:
        print(json.dumps({
            # keep the schema identical to a real run so callers can read the
            # same keys whether or not they passed --dry-run
            "discovered": len(discovered),
            "pending": len(pending),
            "attempted": 0,
            "downloaded": 0,
            "failed": 0,
            "failures": [],
            "pending_urls": [url for _, _, url in pending],
            "state_file": str(state_file),
        }, ensure_ascii=False, indent=2), file=out)
        return 0

    completed_count = 0
    failed_count = 0
    started = time.monotonic()

    def prepare(item_id: str) -> None:
        item = items[item_id]
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_attempt_at"] = now_iso()

    def record(item_id: str, ok: bool, detail: str, directory: Path) -> None:
        nonlocal completed_count, failed_count
        item = items[item_id]
        if ok:
            completed_count += 1
            item.update({
                "status": "completed",
                "output_dir": str(directory),
                "completed_at": now_iso(),
            })
            item.pop("error", None)
        else:
            failed_count += 1
            item.update({"status": "failed", "error": detail[-4000:]})
        write_json_atomic(state_file, state)
        done = completed_count + failed_count
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 else 0.0
        remaining = max(len(pending) - done, 0)
        eta = remaining / rate if rate > 0 else 0.0
        reason = "" if ok else f"  {summarize_error(detail)}"
        print(
            f"[{done}/{len(pending)}] {item_id} {'ok' if ok else 'FAILED'}"
            f"  {rate:.1f} items/s  elapsed {format_duration(elapsed)}"
            f"  eta {format_duration(eta)}{reason}",
            file=sys.stderr,
            flush=True,
        )

    def finish(directory: Path) -> Path:
        """Give a finished item its title-based name, when it has a title."""
        if not valid_download(directory):
            return directory
        return retitle_item(directory)

    if jobs <= 1:
        for _platform, item_id, url in pending:
            prepare(item_id)
            landing = item_directory(items[item_id], output_dir, item_id)
            ok, detail = download_fn(
                downloader, url, landing, cookies, cookies_from_browser
            )
            if ok:
                landing = finish(landing)
            record(item_id, ok, detail, landing)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {}
            for _platform, item_id, url in pending:
                prepare(item_id)
                landing = item_directory(items[item_id], output_dir, item_id)
                future = pool.submit(
                    download_fn,
                    downloader,
                    url,
                    landing,
                    cookies,
                    cookies_from_browser,
                )
                futures[future] = (item_id, url, landing)
            for future in as_completed(futures):
                item_id, url, landing = futures[future]
                try:
                    ok, detail = future.result()
                except Exception as exc:  # a worker must never kill the run
                    ok, detail = False, f"unexpected download error: {exc}"
                if ok:
                    landing = finish(landing)
                record(item_id, ok, detail, landing)

    # Keep the last run's shape on disk: it explains a slow or failed pass
    # without having to re-read the console output.
    state["jobs"] = max(jobs, 1)
    state["elapsed_seconds"] = round(time.monotonic() - started, 1)
    write_json_atomic(state_file, state)

    attempted_ids = {entry[1] for entry in pending}
    failed_now = [
        (key, summarize_error(str(value.get("error") or ""), 200))
        for key, value in items.items()
        if value.get("status") == "failed" and key in attempted_ids
    ]
    if failed_now:
        print(f"{len(failed_now)} item(s) failed; rerun to retry them:", file=sys.stderr)
        for key, reason in failed_now:
            print(f"  {key}: {reason}", file=sys.stderr)
        print(f"  (details also in {state_file})", file=sys.stderr)

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
        "jobs": max(jobs, 1),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "failures": [{"id": key, "error": reason} for key, reason in failed_now[:20]],
    }, ensure_ascii=False, indent=2), file=out)
    return 2 if failed_count else 0
