#!/usr/bin/env python3
"""Enumerate and download Instagram saved collections through a live browser.

This is route 2 of the sync design: no cookie export, no ``yt-dlp`` extractor
for Instagram. The page (which is already logged in) calls Instagram's own web
API, the script takes the *signed* CDN URLs it returns, and Python streams them
straight to disk using the same directory layout, ``manifest.json`` and
``sync-state.json`` the rest of the pipeline already understands.

Typical first run::

    python3 browser_sync.py launch          # opens a dedicated profile window
    #  -> log into Instagram once, leave the window open
    python3 browser_sync.py check
    python3 browser_sync.py collections
    python3 browser_sync.py sync --collection 自然 --output-dir ~/Downloads/instagram-saved/自然 --limit 3

Later runs only need ``sync``; ``--launch`` starts the browser automatically
when nothing is listening yet.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_session  # noqa: E402
import cdp  # noqa: E402
import sync_common  # noqa: E402
from browser_session import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_PROFILE_DIR,
    INSTAGRAM_ORIGIN,
    SessionError,
    InstagramSession,
)


DOWNLOADER = Path(__file__).with_name("download_media.py")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------


def open_session(args: argparse.Namespace) -> InstagramSession:
    """Connect to the browser, launching it when requested and allowed."""
    endpoint = args.endpoint
    if not browser_session.is_browser_running(endpoint):
        if not args.launch:
            raise SessionError(
                f"no browser is listening on {endpoint}; pass --launch or run "
                "`browser_sync.py launch` first"
            )
        log(f"starting {args.browser or 'a Chromium browser'} on {endpoint} ...")
        browser_session.launch_browser(
            browser=args.browser,
            profile_dir=Path(args.profile_dir),
            port=args.port,
            url=INSTAGRAM_ORIGIN,
        )
    session = InstagramSession(
        endpoint=endpoint, timeout=args.timeout, origin=args.origin
    )
    session.connect()
    return session


def resolve_username(session: InstagramSession, args: argparse.Namespace) -> str:
    """Use ``--username`` when given, otherwise ask the logged-in page."""
    if args.username:
        return args.username
    try:
        username = str(session.whoami().get("username") or "")
    except (SessionError, cdp.CdpError):
        username = ""
    if username:
        log(f"account: {username}")
    return username


ROUTE_HINTS_FILE = ".route-hints.json"
#: a remembered retirement lives this long, then the route is probed again
ROUTE_HINT_TTL = 14 * 24 * 3600


def load_route_hints(root: Path | None) -> dict:
    if root is None:
        return {}
    try:
        hints = json.loads((root / ROUTE_HINTS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return hints if isinstance(hints, dict) else {}


def remember_route(root: Path | None, route: str) -> None:
    """Record that an endpoint answered 404, so later runs skip the probe.

    A missing root is left alone: a read-only command such as ``check`` must
    not create the output tree just to park a hint in it.
    """
    if root is None or not root.is_dir():
        return
    hints = load_route_hints(root)
    hints[route] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sync_common.write_json_atomic(root / ROUTE_HINTS_FILE, hints)


def route_is_retired(hints: dict, route: str, ttl: float = ROUTE_HINT_TTL) -> bool:
    stamp = str(hints.get(route) or "")
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return 0 <= (datetime.datetime.now() - moment).total_seconds() < ttl


def list_collections(
    session: InstagramSession,
    args: argparse.Namespace,
    root: Path | None = None,
) -> tuple[list[dict], str]:
    """Return ``(collections, source)``, preferring REST then network capture.

    A 404 from the collection-list route is remembered in ``root``: probing a
    retired endpoint on every run buys a scary note and nothing else. The
    memory expires after two weeks so a restored endpoint is picked back up,
    and a failing capture still reports that the probe was skipped.
    """
    rest_error = ""
    skipped = False
    if route_is_retired(load_route_hints(root), "collections_list_retired"):
        skipped = True
        rest_error = "the collection-list route is retired (remembered)"
    else:
        try:
            collections = session.collections()
            if collections:
                return collections, "api"
            rest_error = "the collection-list endpoint returned nothing"
        except (SessionError, cdp.CdpError) as exc:
            rest_error = str(exc)
            if "HTTP 404" in rest_error:
                remember_route(root, "collections_list_retired")
    if not getattr(args, "capture", True):
        raise SessionError(
            f"{rest_error}; retry without --no-capture to read the page's own requests"
        )
    if not skipped:
        log(f"note: {rest_error}")
    log("reading the collections the saved page loads for itself ...")
    try:
        collections = session.collections_via_capture(
            args.username, rounds=args.scroll_rounds, delay_ms=args.scroll_delay_ms
        )
    except (SessionError, cdp.CdpError) as exc:
        raise SessionError(f"{rest_error}; page capture also failed: {exc}")
    if collections:
        return collections, "browser-network"
    raise SessionError(
        f"{rest_error}; the saved page did not expose any collection either. "
        "Run `diagnose` to see which API paths the page called."
    )


SAVED_ID_PATTERN = re.compile(r"/saved/(?:[^/]+/)?(\d+)")


def collections_from_state(root: Path) -> list[dict]:
    """Recover collections from the folders an earlier run already created.

    Every ``sync-state.json`` records the collection URL it came from, so an
    existing output tree is a *local, deterministic* inventory: it needs no API,
    survives Instagram renaming endpoints, and keeps each collection in the
    folder it already lives in instead of re-downloading into a new one.
    """
    found: list[dict] = []
    for state_file in sorted(root.glob("*/sync-state.json")):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = str(state.get("source") or "")
        match = SAVED_ID_PATTERN.search(url)
        if not match:
            continue
        found.append({
            "id": match.group(1),
            "name": state_file.parent.name,
            "url": url,
            "source": "state-file",
        })
    return found


def resolve_collection(
    session: InstagramSession, query: str, args: argparse.Namespace
) -> dict:
    collections, _source = list_collections(session, args)
    return browser_session.match_collection(collections, query)


def full_walk_pending(state_file: Path | None) -> bool:
    """Whether this collection's state asks for one full enumeration."""
    if not state_file or not Path(state_file).is_file():
        return False
    try:
        return bool(sync_common.load_state(Path(state_file)).get("full_walk_pending"))
    except (OSError, ValueError):
        return False


def clear_full_walk(state_file: Path | None) -> None:
    """Drop the full-walk request once the sweep has actually happened."""
    if not state_file or not Path(state_file).is_file():
        return
    try:
        state = sync_common.load_state(Path(state_file))
    except (OSError, ValueError):
        return
    if state.pop("full_walk_pending", None) is not None:
        sync_common.write_json_atomic(Path(state_file), state)


def known_shortcodes(state_file: Path | None) -> set[str]:
    """Shortcodes already archived with a completed download.

    Only ``completed`` counts: a forgotten or failed item must be walked past
    so the sync meets it again, which is what makes ``repair --forget``
    self-healing under incremental enumeration.
    """
    if not state_file or not Path(state_file).is_file():
        return set()
    try:
        state = sync_common.load_state(Path(state_file))
    except (OSError, ValueError):
        return set()
    return {
        item_id
        for item_id, entry in (state.get("items") or {}).items()
        if isinstance(entry, dict) and entry.get("status") == "completed"
    }


def enumerate_items(
    session: InstagramSession,
    collection: dict,
    args: argparse.Namespace,
    known: set[str] | None = None,
) -> tuple[list[dict], str]:
    """Return ``(items, source)``, trying every route from fast to safest.

    * ``api`` — the page replays Instagram's REST feed; items carry signed URLs.
    * ``page-query`` — the page's own saved-feed GraphQL query is replayed with
      its ``variables`` swapped and paginated by cursor. This is the route that
      matches what the app itself does, so it survives REST retirements.
    * ``browser-network`` — the page is scrolled and the JSON it fetches for
      itself is read off CDP.
    * ``dom`` — only post links are harvested; media falls back to yt-dlp.
    """
    collection_id = str(collection.get("id") or "")
    url = str(collection.get("url") or "") or browser_session.collection_url(
        collection_id, username=args.username, origin=args.origin
    )
    problems: list[str] = []

    if collection_id:
        try:
            payload = session.collection_items(
                collection_id, max_items=args.max_items, delay_ms=args.delay_ms
            )
        except (SessionError, cdp.CdpError) as exc:
            problems.append(f"feed endpoint: {exc}")
        else:
            errors = [str(value) for value in payload.get("errors") or []]
            for message in errors:
                problems.append(f"feed endpoint: {message}")
            items = browser_session.project_items(payload.get("items") or [])
            if items:
                log(f"api: {len(items)} item(s) over {payload.get('pages')} page(s)")
                return items, "api"

    if args.capture and not getattr(args, "no_replay", False):
        try:
            payload = session.collection_feed(
                url,
                collection_id=collection_id or None,
                max_items=args.max_items,
                delay_ms=args.delay_ms,
                log=log,
                is_known=None if known is None else (lambda code: code in known),
            )
        except browser_session.RateLimitedError:
            # a spent quota must abort the run, not fall through to capture:
            # the capture would then read the global saved feed and file its
            # few newest items into every collection it walks
            raise
        except (SessionError, cdp.CdpError) as exc:
            problems.append(f"page query: {exc}")
        else:
            items = browser_session.project_items(payload.get("items") or [])
            if items:
                log(
                    f"page-query: {len(items)} item(s) over {payload.get('pages')} "
                    f"page(s) via {payload.get('query') or 'the page query'}"
                )
                return items, "page-query"
            problems.append(
                "page query: the page did not fetch a saved-collection feed"
            )

    if args.capture:
        try:
            payload = session.items_via_capture(
                url,
                rounds=args.scroll_rounds,
                delay_ms=args.scroll_delay_ms,
                max_items=args.max_items,
            )
        except (SessionError, cdp.CdpError) as exc:
            problems.append(f"page capture: {exc}")
        else:
            items = payload["items"]
            if items:
                log(
                    f"capture: {len(items)} item(s) from {payload['pages']} "
                    f"response(s) the page fetched"
                )
                return items, "browser-network"
            problems.append(
                "page capture: the page loaded without exposing any media"
            )

    if not args.dom_fallback:
        detail = "; ".join(problems) or "no route produced items"
        raise SessionError(
            f"{collection.get('name')!r}: {detail}. Retry with --dom-fallback to "
            "harvest post links from the page (media will use yt-dlp)."
        )

    log(f"dom: scrolling {url} for post links ...")
    links = session.dom_links(url, rounds=args.dom_rounds, delay_ms=args.dom_delay_ms)
    items = [{"code": link.rstrip("/").rsplit("/", 1)[-1], "children": []} for link in links]
    items = browser_session.project_items(items)
    log(f"dom: {len(items)} item(s)")
    return items, "dom"


# --------------------------------------------------------------------------
# Downloading through the session
# --------------------------------------------------------------------------


def write_sidecars(item: dict, directory: Path) -> Path:
    """Write ``*.info.json`` and ``*.description`` beside the media."""
    base = browser_session.safe_filename(
        f"{browser_session.item_shortcode(item)}_{item.get('username') or 'unknown'}"
    )
    metadata = browser_session.trimmed_metadata(item)
    info_json = directory / f"{base}.info.json"
    info_json.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    caption = str(item.get("caption") or "")
    if caption:
        (directory / f"{base}.description").write_text(caption, encoding="utf-8")
    return info_json


def make_session_downloader(
    media_by_url: dict[str, dict],
    prefer_ytdlp: bool = False,
    allow_ytdlp_fallback: bool = True,
    stats: dict[str, int] | None = None,
    session: "InstagramSession | None" = None,
) -> sync_common.DownloadFn:
    """Build a ``download_fn`` that streams signed CDN URLs itself.

    Items without a direct URL (DOM fallback, or a platform the API did not
    describe) are handed to the existing yt-dlp downloader, so the two paths
    coexist in one sync run. Every item is counted in ``stats`` so a slow run
    can be explained instead of guessed at: yt-dlp needs a full extractor pass
    per post, the session path is a plain authenticated HTTP GET.

    ``session`` lets the downloader ask Instagram for a reel's full
    description when the listing only carried its cover image.
    """
    counters = stats if stats is not None else {}

    def fallback(
        downloader: Path,
        url: str,
        output_dir: Path,
        cookies: Path | None,
        cookies_from_browser: str | None,
    ) -> tuple[bool, str]:
        return sync_common.run_download(
            downloader, url, output_dir, cookies, cookies_from_browser, platform="instagram"
        )

    def use_ytdlp(
        downloader: Path,
        url: str,
        output_dir: Path,
        cookies: Path | None,
        cookies_from_browser: str | None,
        reason: str,
    ) -> tuple[bool, str]:
        counters["via_ytdlp"] = counters.get("via_ytdlp", 0) + 1
        if not allow_ytdlp_fallback:
            return False, f"yt-dlp fallback disabled ({reason}); no direct media URL"
        log(f"yt-dlp fallback ({reason}): {url}")
        return fallback(downloader, url, output_dir, cookies, cookies_from_browser)

    def download(
        downloader: Path,
        url: str,
        output_dir: Path,
        cookies: Path | None,
        cookies_from_browser: str | None,
    ) -> tuple[bool, str]:
        item = media_by_url.get(url)
        if prefer_ytdlp:
            return use_ytdlp(downloader, url, output_dir, cookies, cookies_from_browser, "--prefer-yt-dlp")
        if item is None:
            return use_ytdlp(downloader, url, output_dir, cookies, cookies_from_browser, "no API payload")
        if session is not None and browser_session.video_part_missing(item):
            pk = str(item.get("pk") or "")
            if not pk:
                return use_ytdlp(
                    downloader, url, output_dir, cookies, cookies_from_browser, "video part without a pk"
                )
            try:
                item = session.media_info(pk)
            except (SessionError, cdp.CdpError) as exc:
                return use_ytdlp(
                    downloader, url, output_dir, cookies, cookies_from_browser,
                    f"media info failed: {str(exc)[:200]}",
                )
            media_by_url[url] = item
        plan = browser_session.item_media(item)
        if not plan:
            return use_ytdlp(downloader, url, output_dir, cookies, cookies_from_browser, "no media in payload")
        counters["via_session"] = counters.get("via_session", 0) + 1

        output_dir.mkdir(parents=True, exist_ok=True)
        multiple = len(plan) > 1
        files: list[Path] = []
        try:
            for media in plan:
                destination = output_dir / browser_session.item_filename(item, media, multiple)
                if destination.exists() and destination.stat().st_size > 0:
                    # a retried item must not re-download the parts that already
                    # completed before the failure
                    log(f"  [{browser_session.item_shortcode(item)}] {destination.name} already on disk")
                    files.append(destination)
                    continue
                started_at = time.monotonic()
                size = browser_session.download_url(media["url"], destination)
                seconds = max(time.monotonic() - started_at, 1e-6)
                log(
                    f"  [{browser_session.item_shortcode(item)}] {destination.name} "
                    f"{size / 1048576:.1f} MiB in {seconds:.1f}s "
                    f"({size / 1048576 / seconds:.1f} MiB/s)"
                )
                files.append(destination)
        except SessionError as exc:
            return False, str(exc)

        info_json = write_sidecars(item, output_dir)
        manifest = browser_session.build_manifest(item, files, output_dir, info_json=info_json)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True, f"browser session: {len(files)} file(s)"

    return download


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def command_check(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"endpoint": args.endpoint}
    try:
        payload["browser"] = cdp.version(args.endpoint, timeout=args.timeout).get("Browser")
    except cdp.CdpError as exc:
        payload["error"] = str(exc)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    with InstagramSession(
        endpoint=args.endpoint, timeout=args.timeout, origin=args.origin
    ) as session:
        who = session.whoami()
        payload.update({"logged_in": bool(who.get("logged_in")), "username": who.get("username") or None})
        if who.get("logged_in"):
            args.username = args.username or who.get("username") or ""
            try:
                collections, source = list_collections(
                    session, args, Path(args.output_root)
                )
            except (SessionError, cdp.CdpError, RuntimeError) as exc:
                payload["collections_error"] = str(exc)
            else:
                payload["collections"] = len(collections)
                payload["collections_source"] = source
                payload["collection_names"] = [item.get("name") for item in collections]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("logged_in") else 2


PLAYABLE_VIDEO_CODECS = frozenset(
    {"h264", "avc1", "avc3", "hevc", "h265", "hev1", "hvc1", "mjpeg", "prores", "mpeg4"}
)
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov")
BACKUP_SUFFIX = ".unplayable"
#: Junk left behind by an interrupted download, yt-dlp included.
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".part-frag")


def find_partials(root: Path) -> list[Path]:
    """List leftover partial files (``*.part``, ``*.ytdl``) under ``root``."""
    root = Path(root)
    if not root.is_dir():
        return []
    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith(PARTIAL_SUFFIXES)
            or ".part-" in path.name
        )
    ]
    return sorted(found)


def find_sidecar_junk(root: Path) -> list[Path]:
    """List cover images and audio leftovers that sit next to their video.

    The yt-dlp fallback used to write a converted thumbnail beside every
    video, and a DASH merge can leave its ``.m4a`` input behind. A jpg counts
    as junk only when an mp4 with the exact same stem exists in the same
    directory: carousel images carry ``_1``/``_2`` indexes and different
    stems, so they are never touched. Parked backups are left to --clean.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    junk: list[Path] = []
    for directory in (path.parent for path in root.rglob("manifest.json")):
        if directory.name.endswith(BACKUP_SUFFIX) or not directory.is_relative_to(root):
            continue
        files = {path.name: path for path in directory.iterdir() if path.is_file()}
        mp4s = {Path(name).stem for name in files if name.lower().endswith(".mp4")}
        if not mp4s:
            continue
        for name, path in files.items():
            lowered = name.lower()
            stem = Path(name).stem
            if lowered.endswith(".jpg") and stem in mp4s:
                junk.append(path)
            elif lowered.endswith((".m4a", ".webm")) and any(
                mp4 == stem or mp4.startswith(stem) or stem.startswith(mp4)
                for mp4 in mp4s
            ):
                junk.append(path)
    return sorted(junk)


def probe_video_codec(path: Path, exe: str | None = None) -> str | None:
    """Return the first video codec of ``path`` (e.g. ``vp9``), or None."""
    exe = exe or shutil.which("ffprobe")
    if not exe:
        return None
    try:
        completed = subprocess.run(
            [
                exe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (completed.stdout or "").strip().splitlines()
    if not first:
        return None
    return first[0].split(",")[0].strip() or None


def find_unplayable(root: Path, probe=probe_video_codec) -> list[dict]:
    """List completed downloads whose video codec QuickTime cannot decode.

    Instagram's own progressive ``video_versions`` are H.264, but anything that
    went through yt-dlp may be VP9/AV1 in an mp4 container: playable in VLC,
    invisible to QuickTime Player, Photos and Quick Look.
    """
    root = Path(root)
    findings: list[dict] = []
    for state_file in sorted(root.glob("*/sync-state.json")):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item_id, item in (state.get("items") or {}).items():
            if not isinstance(item, dict) or item.get("status") != "completed":
                continue
            directory = Path(str(item.get("output_dir") or state_file.parent / str(item_id)))
            if not directory.is_dir():
                continue
            for media in sorted(directory.iterdir()):
                if not media.is_file() or media.suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                codec = probe(media)
                if codec and codec.casefold() not in PLAYABLE_VIDEO_CODECS:
                    findings.append({
                        "collection": state_file.parent.name,
                        "id": str(item_id),
                        "codec": codec,
                        "media": str(media),
                        "directory": str(directory),
                        "state_file": str(state_file),
                    })
    return findings


def find_cover_only(root: Path) -> list[dict]:
    """List completed downloads that hold a reel's cover where its video belongs.

    The saved and collection listings describe a reel by its cover image only,
    so before the downloader learned to ask ``/api/v1/media/<pk>/info/`` for
    the full media those items completed as a lone JPEG. The manifest still
    records ``media_type`` 2 while the directory holds no video at all.

    The tree on disk is the source of truth, not the sync state: an interrupted
    run leaves entries without a status, and two concurrent runs can shrink a
    state file, while the cover JPEGs stay exactly where they are.
    """
    root = Path(root)
    findings: list[dict] = []
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        directory = manifest_path.parent
        if directory.name.endswith(BACKUP_SUFFIX):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = (manifest.get("metadata") or [{}])[0]
        if metadata.get("media_type") != 2:
            continue
        if any(
            path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
            for path in directory.iterdir()
        ):
            continue
        findings.append({
            "collection": directory.parent.name,
            "id": directory.name,
            "directory": str(directory),
            "state_file": str(directory.parent / "sync-state.json"),
        })
    return findings


def _parked_backlog(root: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """Group the parked backups into per-collection item lists.

    A parked manifest remembers its post's pk in yt-dlp's info-dict shape,
    which is everything the per-post media-info route needs: the enriched
    answer replaces the whole item, so the conversion only carries identity.
    """
    groups: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for backup in sorted(root.glob(f"*/**/*{BACKUP_SUFFIX}*")):
        if not backup.is_dir() or not backup.is_relative_to(root):
            continue
        item_id = backup.name.split(BACKUP_SUFFIX)[0]
        try:
            manifest = json.loads(
                (backup / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            skipped.append(backup.name)
            continue
        meta = (manifest.get("metadata") or [{}])[0]
        pk = str(meta.get("id") or "")
        code = str(meta.get("shortcode") or item_id)
        if not pk:
            skipped.append(backup.name)
            continue
        try:
            media_type = int(meta.get("media_type") or 0)
        except (TypeError, ValueError):
            media_type = 0
        groups.setdefault(backup.parent.name, []).append({
            "pk": pk,
            "code": code,
            "media_type": media_type or 2,
            "product_type": meta.get("product_type") or "",
            "taken_at": 0,
            "username": meta.get("uploader") or "",
            "caption": meta.get("description") or "",
            "duration": None,
            "video_url": "",
            "image_url": "",
            "children": [],
        })
    return groups, skipped


def command_refetch(args: argparse.Namespace) -> int:
    """Re-download the parked backlog post by post, extension-style.

    The saved-collection query carries the day's rate limit, but the per-post
    media-info route — the one a browser extension rides on — answers fine.
    Every parked manifest knows its post's pk, so the whole backlog can be
    fetched without enumerating anything. A canary item is asked first: if
    that route is throttled too, nothing is attempted.
    """
    try:
        with sync_common.SyncLock("instagram"):
            return _refetch(args)
    except sync_common.SyncBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _refetch(args: argparse.Namespace) -> int:
    root = Path(args.output_root).expanduser().resolve()
    groups, skipped = _parked_backlog(root)
    if skipped:
        log(f"note: {len(skipped)} parked item(s) have no readable manifest: {skipped[:5]}")
    if not groups:
        print(json.dumps({"collections": 0, "items": 0, "note": "nothing is parked"}, indent=2))
        return 0
    canary = next(iter(groups.values()))[0]
    with open_session(args) as session:
        try:
            session.media_info(str(canary.get("pk") or ""))
        except (SessionError, cdp.CdpError) as exc:
            print(
                f"error: the per-post route is throttled too ({exc}); "
                "nothing was downloaded, wait for the quota to reset",
                file=sys.stderr,
            )
            return 1
        log(f"canary ok: asking Instagram for {sum(len(v) for v in groups.values())} parked item(s)")
        totals = {
            "collections": len(groups),
            "items": sum(len(v) for v in groups.values()),
            "downloaded": 0,
            "failed": 0,
            "via_session": 0,
            "via_ytdlp": 0,
        }
        errors = 0
        for collection, items in groups.items():
            log(f"--- {collection}: {len(items)} parked item(s)")
            code, summary = run_sync(
                items,
                root / collection,
                None,
                args,
                source_label=f"repair refetch {root / collection}",
                session=session,
            )
            totals["downloaded"] += int(summary.get("downloaded", 0))
            totals["failed"] += int(summary.get("failed", 0))
            totals["via_session"] += int(summary.get("via_session", 0))
            totals["via_ytdlp"] += int(summary.get("via_ytdlp", 0))
            # the parked backlog has been walked: no full enumeration needed
            # on the next sync, failed items stay non-known either way
            clear_full_walk(root / collection / "sync-state.json")
            if code:
                errors += 1
        totals["errors"] = errors
        print(json.dumps(totals, ensure_ascii=False, indent=2))
        return 2 if errors else 0


def command_probe(args: argparse.Namespace) -> int:
    """Say whether it is safe to run the sync launcher right now.

    A rate-limited session answers navigations with an error page and the
    page-query returns nothing, so a sync started too early downloads the
    few items the global feed offers into collections they do not belong
    to. The probe walks one page of the smallest collection — no downloads
    — and answers the only question that matters: safe to double-click?
    """
    try:
        with sync_common.SyncLock("instagram"):
            return _probe(args)
    except sync_common.SyncBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _probe(args: argparse.Namespace) -> int:
    try:
        with InstagramSession(
            endpoint=args.endpoint, timeout=args.timeout, origin=args.origin
        ) as session:
            try:
                who = session.whoami()
            except (SessionError, cdp.CdpError):
                who = {}
            args.username = args.username or who.get("username") or ""
            try:
                collections, _source = list_collections(session, args, Path(args.output_root))
            except (SessionError, cdp.CdpError) as exc:
                print(f"✗ 拿不到收藏夹列表（{exc}），现在不能同步")
                return 1
            if not collections:
                print("✗ 拿不到收藏夹列表，现在不能同步")
                return 1
            smallest = min(collections, key=lambda c: int(c.get("count") or 0))
            url = str(smallest.get("url") or "") or browser_session.collection_url(
                str(smallest.get("id") or ""), username=args.username, origin=args.origin
            )
            try:
                payload = session.collection_feed(
                    url,
                    collection_id=str(smallest.get("id") or "") or None,
                    max_items=30,
                    page_size=12,
                    delay_ms=args.delay_ms,
                )
            except browser_session.RateLimitedError:
                print("✗ 仍被限流：收藏流查询的配额还没恢复，过几个小时或明早再查")
                return 1
            except (SessionError, cdp.CdpError) as exc:
                print(f"✗ 收藏夹「{smallest.get('name')}」打不开（{exc}），现在不能同步")
                return 1
            items = browser_session.project_items(payload.get("items") or [])
            if items:
                print(
                    f"✓ 可以同步了：收藏夹「{smallest.get('name')}」第 1 页拿到 "
                    f"{len(items)} 条，双击「同步Instagram收藏.command」即可"
                )
                return 0
            print(
                f"✗ 仍被封锁：收藏夹「{smallest.get('name')}」一条都枚举不到，"
                "过几个小时再查"
            )
            return 1
    except (SessionError, cdp.CdpError) as exc:
        print(f"✗ 浏览器连不上（{exc}）：先打开专用浏览器再检查")
        return 2


def command_repair(args: argparse.Namespace) -> int:
    """Tidy an output tree: unplayable files, leftover partials, backups."""
    if getattr(args, "refetch", False):
        return command_refetch(args)
    if getattr(args, "clean", False):
        return command_clean(args)
    root = Path(args.output_root).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    covers_mode = bool(getattr(args, "covers", False))
    if covers_mode:
        findings = find_cover_only(root)
    else:
        if not shutil.which("ffprobe"):
            log("note: ffprobe not found; install ffmpeg to detect codecs")
        findings = find_unplayable(root)
    partials = find_partials(root)
    sidecars = find_sidecar_junk(root)
    partial_bytes = sum(path.stat().st_size for path in partials + sidecars)
    by_codec: dict[str, int] = {}
    for finding in findings:
        codec = finding.get("codec")
        if codec:
            by_codec[codec] = by_codec.get(codec, 0) + 1

    if getattr(args, "parts", False) and (partials or sidecars):
        removed = 0
        for path in partials + sidecars:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                log(f"refusing to delete {resolved} (outside {root})")
                continue
            try:
                resolved.unlink()
                removed += 1
            except OSError as exc:
                log(f"could not remove {resolved}: {exc}")
        log(
            f"removed {removed} junk file(s) "
            f"({len(sidecars)} cover/audio sidecar(s)), "
            f"{partial_bytes / 1048576:.1f} MiB"
        )

    count_key = "cover_only" if covers_mode else "unplayable"
    if not findings:
        print(json.dumps({
            "root": str(root),
            count_key: 0,
            "partial_files": len(partials),
            "partial_mib": round(partial_bytes / 1048576, 1),
            "kept": "run with --parts to delete partial files",
        }, ensure_ascii=False, indent=2))
        return 0
    if not args.forget:
        print(json.dumps({
            "root": str(root),
            count_key: len(findings),
            "by_codec": by_codec,
            "partial_files": len(partials),
            "partial_mib": round(partial_bytes / 1048576, 1),
            "items": sorted({finding["id"] for finding in findings}),
            "next": [
                "rerun with --forget to drop these items from the sync state",
                "then sync again: "
                + ("the reel's video URL is asked for directly"
                   if covers_mode else
                   "the browser session fetches Instagram's H.264 version"),
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    forgotten: set[tuple[str, str]] = set()
    parked: list[str] = []
    for finding in findings:
        directory = Path(finding["directory"]).resolve()
        if not directory.is_relative_to(root):
            log(f"refusing to touch {directory} (outside {root})")
            continue
        # Rename instead of deleting: the item is dropped from the state so the
        # next sync fetches Instagram's H.264 version, but if that post is gone
        # the original file is still recoverable right next to it.
        backup = directory.with_name(directory.name + BACKUP_SUFFIX)
        counter = 2
        while backup.exists():
            backup = directory.with_name(f"{directory.name}{BACKUP_SUFFIX}{counter}")
            counter += 1
        try:
            directory.rename(backup)
        except OSError as exc:
            log(f"could not park {directory}: {exc}")
            continue
        parked.append(str(backup))
        forgotten.add((finding["state_file"], finding["id"]))

    for state_path in {finding["state_file"] for finding in findings}:
        path = Path(state_path)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = state.get("items") or {}
        for key in [key for key in items if (state_path, key) in forgotten]:
            del items[key]
        # Forgotten items wait deep in the collection, often below pages that
        # are already complete; incremental enumeration would stop above them.
        # The flag makes the next sync walk this collection in full once.
        state["full_walk_pending"] = True
        sync_common.write_json_atomic(path, state)

    print(json.dumps({
        "root": str(root),
        "forgotten": len(forgotten),
        "parked": parked,
        "by_codec": by_codec,
        "next": [
            "run sync again: these items will be fetched as "
            + ("videos (their cover was saved before the reel fix)"
               if covers_mode else "H.264"),
            f"originals were renamed with a {BACKUP_SUFFIX} suffix, not deleted",
            f"delete them once you are happy: repair --output-root {root} --clean",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def playable_media(directory: Path) -> bool:
    """Report whether a folder already holds a video macOS players can open."""
    if not directory.is_dir():
        return False
    for media in sorted(directory.iterdir()):
        if not media.is_file() or media.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        codec = probe_video_codec(media)
        if codec and codec.casefold() in PLAYABLE_VIDEO_CODECS:
            return True
    return False


def command_clean(args: argparse.Namespace) -> int:
    """Delete parked originals **only** once a playable replacement exists.

    Deleting the backup before the re-download has actually landed would destroy
    the only local copy on the strength of an intention, so every candidate is
    checked against the sync state first and kept when it cannot be verified.
    """
    root = Path(args.output_root).expanduser().resolve()
    removed: list[str] = []
    kept: list[str] = []
    for backup in sorted(root.glob(f"*/**/*{BACKUP_SUFFIX}*")):
        if not backup.is_dir() or not backup.is_relative_to(root):
            continue
        item_id = backup.name.split(BACKUP_SUFFIX)[0]
        state_file = backup.parent / "sync-state.json"
        entry: dict = {}
        try:
            entry = (json.loads(state_file.read_text(encoding="utf-8")).get("items") or {}).get(item_id) or {}
        except (OSError, json.JSONDecodeError):
            entry = {}
        directory = Path(str(entry.get("output_dir") or backup.parent / item_id))
        if entry.get("status") != "completed" or not playable_media(directory):
            kept.append(f"{backup.name}: no playable replacement yet")
            continue
        try:
            shutil.rmtree(backup)
            removed.append(str(backup))
        except OSError as exc:
            log(f"could not remove {backup}: {exc}")
    if kept:
        log(f"kept {len(kept)} original(s): {kept[0]}")
    print(json.dumps({
        "root": str(root),
        "removed": len(removed),
        "kept": len(kept),
        "kept_items": kept[:20],
        "note": "sync first: --clean only deletes originals that already have a playable replacement",
    }, ensure_ascii=False, indent=2))
    return 0


def command_diagnose(args: argparse.Namespace) -> int:
    """Show what the page itself asks for, so a broken route is self-explaining."""
    report: dict[str, Any] = {"endpoint": args.endpoint, "origin": args.origin}
    with open_session(args) as session:
        who = session.whoami()
        report["username"] = who.get("username") or None
        report["logged_in"] = bool(who.get("logged_in"))
        if not report["logged_in"]:
            report["next"] = [
                "run: browser_sync.py launch",
                "log into Instagram in that window, then rerun diagnose",
            ]
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        args.username = args.username or who.get("username") or ""
        try:
            payload = session.collection_items(
                str(args.collection or ""), max_items=1, delay_ms=args.delay_ms
            )
        except (SessionError, cdp.CdpError) as exc:
            report["rest_feed"] = {"ok": False, "error": str(exc)}
        else:
            report["rest_feed"] = {
                "ok": bool(payload.get("items")),
                "items": len(payload.get("items") or []),
                "errors": payload.get("errors") or [],
            }
        saved_url = browser_session.collection_url(
            None, username=args.username, origin=args.origin
        )
        log(f"diagnose: reading {saved_url} ...")
        page = session.capture_page(
            saved_url,
            rounds=args.scroll_rounds,
            delay_ms=args.scroll_delay_ms,
            max_items=args.max_items,
        )
        report["saved_page"] = {
            "url": page["url"],
            "json_responses": page["payloads"],
            "media_items": len(page["items"]),
            "collections": page["collections"],
            "dom_links": len(page["links"]),
        }
        report["api_paths_called"] = page["api_urls"]
        if not page["payloads"]:
            report["hint"] = (
                "the page made no JSON request matching /api/v1/ or /graphql; "
                "make sure it finished loading (or raise --scroll-rounds)"
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_launch(args: argparse.Namespace) -> int:
    process, endpoint = browser_session.launch_browser(
        browser=args.browser,
        profile_dir=Path(args.profile_dir),
        port=args.port,
        url=INSTAGRAM_ORIGIN,
    )
    print(json.dumps({
        "endpoint": endpoint,
        "profile_dir": str(Path(args.profile_dir).expanduser()),
        "pid": process.pid,
        "next": [
            "log into Instagram in the window that just opened",
            "keep it open, then run: browser_sync.py check",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def command_collections(args: argparse.Namespace) -> int:
    with open_session(args) as session:
        args.username = resolve_username(session, args)
        collections, source = list_collections(session, args)
    log(f"collection list came from: {source}")
    if args.json:
        print(json.dumps(collections, ensure_ascii=False, indent=2))
        return 0
    if not collections:
        print("no saved collections were returned; is this account logged in?")
        return 2
    width = max(len(str(item.get("name"))) for item in collections)
    for collection in collections:
        count = collection.get("count")
        suffix = f"  {count} items" if count is not None else ""
        print(f"{str(collection.get('name')).ljust(width)}  {collection.get('id')}{suffix}")
    return 0


def command_inventory(args: argparse.Namespace) -> int:
    with open_session(args) as session:
        args.username = resolve_username(session, args)
        collection = resolve_collection(session, args.collection, args)
        items, source = enumerate_items(session, collection, args)
    inventory = browser_session.build_inventory(
        str(collection.get("name") or args.collection),
        items,
        url=browser_session.collection_url(
            str(collection.get("id") or ""), username=args.username, origin=args.origin
        ),
        source=f"browser-session:{source}",
    )
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"wrote {output} ({len(items)} posts, source={source})")
    print(json.dumps(inventory, ensure_ascii=False, indent=2))
    return 0


def run_sync(
    items: list[dict],
    output_dir: Path,
    state_file: Path | None,
    args: argparse.Namespace,
    source_label: str,
    session: "InstagramSession | None" = None,
) -> tuple[int, dict]:
    """Sync one collection's items and return ``(exit_code, summary)``."""
    media_by_url = {browser_session.item_page_url(item): item for item in items}
    discovered = [
        ("instagram", browser_session.item_shortcode(item), browser_session.item_page_url(item))
        for item in items
    ]
    if not args.include_photos:
        discovered = [
            entry
            for entry in discovered
            if any(
                part["kind"] == "video"
                for part in browser_session.item_media(media_by_url[entry[2]])
            )
            # a reel the listing described by its cover only is still a video
            or browser_session.video_part_missing(media_by_url[entry[2]])
        ]
    output_dir = Path(output_dir).expanduser()
    if state_file is None:
        state_file = output_dir / "sync-state.json"
    buffer = io.StringIO()
    stats: dict[str, int] = {"via_session": 0, "via_ytdlp": 0}
    code = sync_common.sync_items(
        discovered,
        output_dir=output_dir,
        state_file=state_file,
        downloader=DOWNLOADER,
        download_fn=make_session_downloader(
            media_by_url,
            prefer_ytdlp=args.prefer_yt_dlp,
            allow_ytdlp_fallback=not args.no_yt_dlp_fallback,
            stats=stats,
            session=session,
        ),
        source_label=source_label,
        retry_failed=not args.no_retry_failed,
        dry_run=args.dry_run,
        limit=args.limit,
        stream=buffer,
        jobs=args.jobs,
    )
    try:
        summary = json.loads(buffer.getvalue() or "{}")
    except json.JSONDecodeError:
        summary = {}
    summary.update(stats)
    if summary.get("via_ytdlp"):
        log(
            f"note: {summary['via_ytdlp']} item(s) went through yt-dlp; those are slow "
            "because each one needs a full extractor pass. Use --no-yt-dlp-fallback to "
            "see them as failures instead."
        )
    return code, summary


def command_sync(args: argparse.Namespace) -> int:
    try:
        with sync_common.SyncLock("instagram"):
            return _command_sync(args)
    except sync_common.SyncBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _command_sync(args: argparse.Namespace) -> int:
    if not args.collection and not args.all_collections:
        print("error: pass --collection <id|name> or --all-collections", file=sys.stderr)
        return 2
    if args.collection and not args.output_dir:
        print("error: --output-dir is required with --collection", file=sys.stderr)
        return 2

    with open_session(args) as session:
        args.username = resolve_username(session, args)
        if not args.all_collections:
            collection = resolve_collection(session, args.collection, args)
            state_file = Path(args.state_file or Path(args.output_dir) / "sync-state.json")
            full = args.full or full_walk_pending(state_file)
            known = None if full else known_shortcodes(state_file)
            try:
                items, source = enumerate_items(session, collection, args, known=known)
            except browser_session.RateLimitedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                print(
                    "nothing was downloaded; a rate-limited enumeration cannot "
                    "be trusted. Probe again later with 检查Instagram状态.command",
                    file=sys.stderr,
                )
                return 1
            if not items:
                print("error: no items were discovered for this collection", file=sys.stderr)
                return 2
            log(
                f"syncing {len(items)} item(s) into {Path(args.output_dir).expanduser()} "
                f"(source={source}, direct={'no' if args.prefer_yt_dlp else 'yes'})"
            )
            code, summary = run_sync(
                items,
                args.output_dir,
                args.state_file,
                args,
                source_label=browser_session.collection_url(
                    str(collection.get("id") or ""), username=args.username, origin=args.origin
                ),
                session=session,
            )
            # the sweep has happened: pending-but-failed items stay non-known,
            # so incremental walks still meet them on later pages
            clear_full_walk(state_file)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return code

        root = Path(args.output_root).expanduser()
        existing = collections_from_state(root)
        live: list[dict] = []
        live_error = ""
        try:
            live, source = list_collections(session, args, root)
        except SessionError as exc:
            live_error = str(exc)
        if existing:
            known = {str(item.get("id")) for item in existing}
            fresh = [
                item
                for item in live
                if item.get("id") and str(item.get("id")) not in known
            ]
            collections = existing + fresh
            log(
                f"collections: {len(existing)} from existing folders in {root}"
                + (f", {len(fresh)} newly listed on Instagram" if fresh else "")
            )
            if live_error:
                log(f"note: Instagram did not list collections live ({live_error})")
        elif live:
            collections = live
        else:
            log(f"warning: {live_error or 'no collections were listed'}")
            log(
                "falling back to the whole saved list as one collection; "
                "use `diagnose` to see what the page actually requested"
            )
            collections = [{"id": "", "name": "全部收藏", "url": browser_session.collection_url(
                None, username=args.username, origin=args.origin
            )}]
        labelled = [
            {
                "name": str(collection.get("name") or collection.get("id")),
                "url": str(collection.get("url") or "") or browser_session.collection_url(
                    str(collection.get("id") or ""), username=args.username, origin=args.origin
                ),
                "id": collection.get("id"),
            }
            for collection in collections
        ]
        directories = sync_common.assign_directories(labelled)
        totals = {
            "collections": len(collections),
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "via_session": 0,
            "via_ytdlp": 0,
        }
        errors = 0
        for collection, directory in zip(collections, directories):
            log(f"--- {collection.get('name')} -> {root / directory}")
            state_file = root / directory / "sync-state.json"
            full = args.full or full_walk_pending(state_file)
            known = None if full else known_shortcodes(state_file)
            try:
                items, source = enumerate_items(session, collection, args, known=known)
            except browser_session.RateLimitedError as exc:
                log(f"error: {exc}")
                log(
                    "stopping the whole sync before downloading anything: a "
                    "rate-limited enumeration cannot tell collections apart "
                    "and would file items into the wrong folders. Probe with "
                    "检查Instagram状态.command and retry once it answers ✓"
                )
                errors += 1
                break
            except (SessionError, cdp.CdpError) as exc:
                log(f"skipping {collection.get('name')}: {exc}")
                errors += 1
                continue
            if not items:
                totals["skipped"] += 1
                continue
            code, summary = run_sync(
                items,
                root / directory,
                None,
                args,
                source_label=browser_session.collection_url(
                    str(collection.get("id") or ""), username=args.username, origin=args.origin
                ),
                session=session,
            )
            totals["downloaded"] += int(summary.get("downloaded", 0))
            totals["failed"] += int(summary.get("failed", 0))
            totals["via_session"] += int(summary.get("via_session", 0))
            totals["via_ytdlp"] += int(summary.get("via_ytdlp", 0))
            clear_full_walk(state_file)
            if code:
                errors += 1
        totals["output_root"] = str(root)
        totals["errors"] = errors
        print(json.dumps(totals, ensure_ascii=False, indent=2))
        return 2 if errors else 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags the download engine reads, shared by sync and repair --refetch."""
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="parallel media downloads (default: 4; signed CDN URLs are independent)",
    )
    parser.add_argument(
        "--prefer-yt-dlp",
        action="store_true",
        help="use the old yt-dlp path for every item (A/B comparison)",
    )
    parser.add_argument(
        "--no-yt-dlp-fallback",
        action="store_true",
        help="fail items that have no direct media URL instead of falling back to slow yt-dlp",
    )
    parser.add_argument(
        "--include-photos",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep image posts as well as videos (default: yes)",
    )


def add_session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="DevTools endpoint")
    parser.add_argument(
        "--origin",
        default=INSTAGRAM_ORIGIN,
        help="page origin used for in-page API calls (advanced/testing)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="CDP timeout in seconds")
    parser.add_argument(
        "--launch",
        action="store_true",
        help="start the browser automatically when nothing is listening",
    )
    parser.add_argument(
        "--browser", help="browser executable (default: first Chromium build found)"
    )
    parser.add_argument("--port", type=int, default=9222, help="debug port for --launch")
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="dedicated profile directory used by --launch",
    )
    parser.add_argument(
        "--username",
        default="",
        help="account name used to build saved-collection URLs",
    )


def add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags for reading the page's own API traffic (used by more than `sync`)."""
    parser.add_argument(
        "--capture",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="read the JSON the page fetches for itself when a REST endpoint is retired",
    )
    parser.add_argument(
        "--scroll-rounds",
        type=int,
        default=60,
        help="scroll rounds for page capture (stops early once the feed settles)",
    )
    parser.add_argument("--scroll-delay-ms", type=int, default=900)


def add_enumeration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-items", type=int, default=5000)
    parser.add_argument("--delay-ms", type=int, default=400, help="pause between API pages")
    add_capture_arguments(parser)
    parser.add_argument(
        "--dom-fallback",
        action="store_true",
        help="scroll the rendered page for post links when no route returns media",
    )
    parser.add_argument("--dom-rounds", type=int, default=60)
    parser.add_argument("--dom-delay-ms", type=int, default=1200)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Instagram saved collections through a live browser session"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="report login state and collections")
    add_session_arguments(check)
    add_capture_arguments(check)
    check.add_argument("--max-items", type=int, default=5000)
    check.add_argument("--delay-ms", type=int, default=400)
    check.add_argument(
        "--output-root", type=Path, default=Path("instagram-saved"),
        help="where the sync state lives (retired-route hints are read from here)",
    )
    check.set_defaults(func=command_check)

    probe = subparsers.add_parser(
        "probe", help="say whether Instagram will serve a collection feed right now"
    )
    add_session_arguments(probe)
    add_capture_arguments(probe)
    probe.add_argument("--delay-ms", type=int, default=400)
    probe.add_argument(
        "--output-root", type=Path, default=Path("instagram-saved"),
        help="where the sync state lives (retired-route hints are read from here)",
    )
    probe.set_defaults(func=command_probe)

    launch = subparsers.add_parser("launch", help="start the dedicated debug browser")
    launch.add_argument("--browser")
    launch.add_argument("--port", type=int, default=9222)
    launch.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    launch.set_defaults(func=command_launch)

    collections = subparsers.add_parser("collections", help="list saved collections")
    add_session_arguments(collections)
    add_capture_arguments(collections)
    collections.add_argument("--max-items", type=int, default=5000)
    collections.add_argument("--delay-ms", type=int, default=400)
    collections.add_argument("--json", action="store_true")
    collections.set_defaults(func=command_collections)

    repair = subparsers.add_parser(
        "repair",
        help="find downloads macOS players cannot open and forget them for re-sync",
    )
    repair.add_argument("--output-root", type=Path, default=Path("instagram-saved"))
    repair.add_argument(
        "--forget",
        action="store_true",
        help="drop those items from the sync state and park their files for re-download",
    )
    repair.add_argument(
        "--covers",
        action="store_true",
        help="find reels saved as their cover image (a video with no video file), "
             "then forget and re-sync them",
    )
    repair.add_argument(
        "--parts",
        action="store_true",
        help="delete leftover partial files (*.part, *.ytdl) and cover/audio "
             "sidecars sitting next to their video",
    )
    repair.add_argument(
        "--clean",
        action="store_true",
        help="delete the parked originals once the refreshed files look fresh",
    )
    repair.add_argument(
        "--refetch",
        action="store_true",
        help="re-download every parked item through the per-post media-info route, "
             "without the saved-collection query that carries the rate limit",
    )
    add_session_arguments(repair)
    add_download_arguments(repair)
    repair.set_defaults(func=command_repair)

    diagnose = subparsers.add_parser(
        "diagnose", help="report the API paths the saved page actually calls"
    )
    add_session_arguments(diagnose)
    add_enumeration_arguments(diagnose)
    diagnose.add_argument("--collection", help="also probe this collection id")
    diagnose.set_defaults(func=command_diagnose)

    inventory = subparsers.add_parser("inventory", help="write a collection inventory JSON")
    add_session_arguments(inventory)
    add_enumeration_arguments(inventory)
    inventory.add_argument("--collection", required=True, help="collection id or name")
    inventory.add_argument("--output", type=Path)
    inventory.set_defaults(func=command_inventory)

    sync = subparsers.add_parser("sync", help="download a collection incrementally")
    add_session_arguments(sync)
    add_enumeration_arguments(sync)
    sync.add_argument("--collection", help="collection id or name")
    sync.add_argument(
        "--all-collections",
        action="store_true",
        help="sync every saved collection into its own folder under --output-root",
    )
    sync.add_argument("--output-dir", type=Path, help="target folder for a single collection")
    sync.add_argument(
        "--output-root",
        type=Path,
        default=Path("instagram-saved"),
        help="root folder for --all-collections (default: ./instagram-saved)",
    )
    sync.add_argument("--state-file", type=Path)
    add_download_arguments(sync)
    sync.add_argument(
        "--full", action="store_true",
        help="walk every page instead of stopping at already-archived items",
    )
    sync.set_defaults(func=command_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (SessionError, cdp.CdpError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
