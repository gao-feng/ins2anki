#!/usr/bin/env python3
"""Reuse a real, logged-in browser through CDP instead of exporting cookies.

``yt-dlp``/``gallery-dl`` need a Netscape cookie file, which expires, breaks
when the browser holds a lock on its cookie database, and still looks like a
CLI client to Instagram. This module does the opposite: it talks to a browser
the user already logged into, asks the *page* to call Instagram's own web API
(same origin, same session, same TLS fingerprint), and then downloads the
signed CDN URLs the API returned.

Why the signed URLs are the important part: ``scontent*.cdninstagram.com``
links carry their own signature and do not need cookies, so the media download
never touches the session at all. Only enumeration does.

Chromium >= M136 ignores ``--remote-debugging-port`` when it is started with
the *default* profile directory, so :func:`launch_browser` always passes an
explicit ``--user-data-dir`` (``~/.ins2anki/browser-profile`` by default). Log
in once in that window; every later run reuses the session.

Everything here is standard library only; the CDP transport lives in
:mod:`cdp`.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import random
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cdp  # noqa: E402


DEFAULT_ENDPOINT = cdp.DEFAULT_ENDPOINT
DEFAULT_PROFILE_DIR = Path.home() / ".ins2anki" / "browser-profile"
INSTAGRAM_ORIGIN = "https://www.instagram.com/"

# The public web app id Instagram's own frontend sends. It is stable for years
# and is what every unauthenticated-but-logged-in web client uses.
IG_APP_ID = "936619743392459"

BROWSER_CANDIDATES = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "microsoft-edge",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": INSTAGRAM_ORIGIN,
    "Accept": "*/*",
}

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


#: Instagram's "you asked too often" complaint, seen on /api/graphql replays
RATE_LIMIT_CODES = {1675004}


def rate_limit_message(payload: Any) -> str | None:
    """Return Instagram's rate-limit complaint, if the payload carries one.

    A throttled graphql answers HTTP 200 with an empty ``data`` and this
    error tucked inside — without looking, an exhausted quota reads as an
    empty collection and the sync files whatever the global feed offers
    into every folder it walks.
    """
    if not isinstance(payload, dict):
        return None
    for error in payload.get("errors") or []:
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "")
        if error.get("code") in RATE_LIMIT_CODES or "rate limit" in message.lower():
            return message or "rate limit exceeded"
    return None


class SessionError(RuntimeError):
    """Raised when no usable browser session is available."""


class MediaDownloadError(SessionError):
    """A CDN HTTP failure whose status can trigger a signed-URL refresh."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class RateLimitedError(SessionError):
    """Instagram refused the query because its quota for this session is spent."""


# --------------------------------------------------------------------------
# Browser discovery and launch
# --------------------------------------------------------------------------


def find_browser(explicit: str | None = None) -> str:
    """Locate a Chromium-based browser to drive."""
    if explicit:
        if Path(explicit).exists() or shutil.which(explicit):
            return explicit
        raise SessionError(f"browser not found: {explicit}")
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise SessionError(
        "no Chromium-based browser found; pass --browser with an executable path"
    )


def endpoint_url(port: int = 9222, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def launch_browser(
    browser: str | None = None,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    port: int = 9222,
    url: str = INSTAGRAM_ORIGIN,
    extra_args: Iterable[str] = (),
    wait: float = 30.0,
) -> tuple[subprocess.Popen, str]:
    """Start the browser with remote debugging on a dedicated profile.

    Returns ``(process, endpoint)``. The process keeps running after this
    function returns, so the caller decides when to stop it; the login made in
    this window is what later runs reuse.
    """
    executable = find_browser(browser)
    profile_dir = Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    endpoint = endpoint_url(port)
    command = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        *extra_args,
        url,
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        cdp.wait_for_endpoint(endpoint, timeout=wait)
    except cdp.CdpError as exc:
        process.terminate()
        raise SessionError(str(exc)) from exc
    return process, endpoint


def is_browser_running(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 2.0) -> bool:
    """Report whether a DevTools endpoint answers."""
    try:
        cdp.version(endpoint, timeout=timeout)
        return True
    except cdp.CdpError:
        return False


# --------------------------------------------------------------------------
# Page-context JavaScript
# --------------------------------------------------------------------------

# Shared helpers injected as a self-contained IIFE. ``fetch`` runs in the page,
# so it carries the session cookies, the right origin and the real fingerprint.
_JS_PRELUDE = """
globalThis.__ins2anki = (() => {
  const APP_ID = %(app_id)s;
  const csrf = (document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/) || [])[1] || "";
  const headers = {
    "x-ig-app-id": APP_ID,
    "x-csrftoken": csrf,
    "x-requested-with": "XMLHttpRequest",
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const getJson = async (path, timeoutMs = 0) => {
    const controller = new AbortController();
    const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
    let response, text;
    try {
      response = await fetch(path, { credentials: "include", headers, signal: controller.signal });
      text = await response.text();
    } catch (err) {
      if (controller.signal.aborted) throw new Error(`Request timed out for ${path}`);
      throw err;
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
    if (!response.ok) {
      // an HTML body is the SPA shell and carries no diagnostic value; a
      // JSON body (rate limits, login walls) is worth keeping a prefix of
      const summary = text.startsWith("<") ? "HTML shell" : text.slice(0, 160);
      const error = new Error(`HTTP ${response.status} for ${path}: ${summary}`);
      error.status = response.status;
      throw error;
    }
    try {
      return JSON.parse(text);
    } catch (err) {
      throw new Error(`invalid JSON from ${path}: ${text.slice(0, 200)}`);
    }
  };
  const best = (list, pickWidth) => {
    let winner = null;
    for (const entry of list || []) {
      const width = Number((pickWidth ? pickWidth(entry) : entry.width) || 0);
      if (!winner || width > winner.width) winner = { width, entry };
    }
    return winner ? winner.entry : null;
  };
  // QuickTime decodes H.264/HEVC only: the widest video_version is sometimes
  // a VP9 or AV1 stream that downloads fine but will not open. Rank entries
  // avc/hevc first, unmarked second, vp09/av01 last, then take the widest.
  const codecTier = (entry) => {
    const type = String(entry && (entry.type || entry.mime_type) || "");
    if (/(avc1|hvc1|hev1)/i.test(type)) return 0;
    if (/(vp09|vp9|av01)/i.test(type)) return 2;
    return 1;
  };
  const bestVideo = (list) => {
    let winner = null;
    for (const entry of list || []) {
      const rank = codecTier(entry);
      const width = Number((entry.width || entry.config_width || 0), 10) || 0;
      if (!winner || rank < winner.rank || (rank === winner.rank && width > winner.width)) {
        winner = { rank, width, entry };
      }
    }
    return winner ? winner.entry : null;
  };
  const project = (media) => {
    if (!media) return null;
    const videos = media.video_versions || [];
    const video = bestVideo(videos);
    const candidates =
      (media.image_versions2 && media.image_versions2.candidates) ||
      media.display_resources ||
      [];
    const image = best(candidates, (entry) => entry.width || entry.config_width);
    const user = media.user || media.owner || {};
    const result = {
      pk: String(media.pk || media.id || ""),
      code: media.code || media.shortcode || "",
      media_type: media.media_type,
      product_type: media.product_type || "",
      taken_at: media.taken_at || media.taken_at_timestamp || media.device_timestamp || 0,
      username: user.username || media.username || "",
      caption: (media.caption && media.caption.text) || media.caption_text || "",
      duration: media.video_duration || null,
      video_url: video ? video.url || video.src || "" : "",
      image_url: image ? image.url || image.src || "" : "",
      children: [],
    };
    const children = media.carousel_media || media.carousel_media_edits || [];
    if (children.length) {
      result.children = children.map(project).filter(Boolean);
    }
    return result;
  };
  return { APP_ID, csrf, headers, sleep, getJson, project };
})();
"""


def js_prelude(app_id: str = IG_APP_ID) -> str:
    """Return the shared page-context helper IIFE."""
    return _JS_PRELUDE % {"app_id": json.dumps(str(app_id))}


_JS_LIST_COLLECTIONS = """
(async () => {
  const payload = await __ins2anki.getJson("/api/v1/collections/list/");
  if (payload.status === "fail") {
    throw new Error(`Instagram refused the collection list: ${payload.message || "unknown"}`);
  }
  const raw = payload.items || payload.collections || [];
  return raw.map((item) => ({
    id: String(item.collection_id || item.id || ""),
    name: item.collection_name || item.name || "",
    type: item.collection_type || item.type || "",
    count: item.media_count ?? item.count ?? null,
  }));
})()
"""

_JS_COLLECTION_ITEMS = """
(async () => {
  const collectionId = %(collection_id)s;
  const maxItems = %(max_items)s;
  const delayMs = %(delay_ms)s;
  const items = [];
  const errors = [];
  let maxId = "";
  let pages = 0;
  while (true) {
    const query = new URLSearchParams({ count: "33" });
    if (maxId) query.set("max_id", maxId);
    const path = `/api/v1/feed/collection/${encodeURIComponent(collectionId)}/posts/?${query}`;
    let payload;
    try {
      payload = await __ins2anki.getJson(path);
    } catch (err) {
      errors.push(String(err.message || err));
      break;
    }
    if (payload.status === "fail") {
      errors.push(`Instagram refused the page: ${payload.message || "unknown"}`);
      break;
    }
    pages += 1;
    for (const media of payload.items || []) {
      const projected = __ins2anki.project(media);
      if (projected) items.push(projected);
      if (items.length >= maxItems) break;
    }
    const next = payload.next_max_id || "";
    if (items.length >= maxItems || !payload.more_available || !next) break;
    if (next === maxId) break;
    maxId = next;
    await __ins2anki.sleep(delayMs);
  }
  return { items, pages, errors, truncated: items.length >= maxItems };
})()
"""

_JS_SAVED_ALL_ITEMS = """
(async () => {
  const maxItems = %(max_items)s;
  const delayMs = %(delay_ms)s;
  const items = [];
  const errors = [];
  let maxId = "";
  let pages = 0;
  while (true) {
    const query = new URLSearchParams({ count: "33" });
    if (maxId) query.set("max_id", maxId);
    const path = `/api/v1/feed/saved/posts/?${query}`;
    let payload;
    try {
      payload = await __ins2anki.getJson(path);
    } catch (err) {
      errors.push(String(err.message || err));
      break;
    }
    if (payload.status === "fail") {
      errors.push(`Instagram refused the page: ${payload.message || "unknown"}`);
      break;
    }
    pages += 1;
    for (const media of payload.items || []) {
      const projected = __ins2anki.project(media);
      if (projected) items.push(projected);
      if (items.length >= maxItems) break;
    }
    const next = payload.next_max_id || "";
    if (items.length >= maxItems || !payload.more_available || !next) break;
    if (next === maxId) break;
    maxId = next;
    await __ins2anki.sleep(delayMs);
  }
  return { items, pages, errors, truncated: items.length >= maxItems };
})()
"""

_JS_MEDIA_INFO = """
(async () => {
  const pk = %(pk)s;
  await __ins2anki.sleep(%(delay_ms)s);
  const payload = await __ins2anki.getJson(`/api/v1/media/${encodeURIComponent(pk)}/info/`, %(fetch_timeout_ms)s);
  if (payload.status === "fail") {
    throw new Error(`Instagram refused media ${pk}: ${payload.message || "unknown"}`);
  }
  const media = (payload.items || [])[0] || null;
  return media ? __ins2anki.project(media) : null;
})()
"""

_JS_DOM_LINKS = """
(async () => {
  const rounds = %(rounds)s;
  const delayMs = %(delay_ms)s;
  const pattern = /instagram\\.com\\/(?:p|reel|tv)\\/([A-Za-z0-9_-]+)/i;
  const seen = new Map();
  const collect = () => {
    for (const anchor of document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
      const match = String(anchor.getAttribute("href") || "").match(pattern);
      if (match) seen.set(match[1], `https://www.instagram.com/p/${match[1]}/`);
    }
  };
  let stable = 0;
  for (let round = 1; round <= rounds; round += 1) {
    const before = seen.size;
    for (const element of document.querySelectorAll("div, main, section")) {
      if (element.scrollHeight > element.clientHeight + 200) {
        element.scrollTop = element.scrollHeight;
      }
    }
    window.scrollTo(0, document.body.scrollHeight);
    collect();
    if (seen.size === before) {
      stable += 1;
      if (stable >= 3 && round > 3) break;
    } else {
      stable = 0;
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return { urls: Array.from(seen.values()), rounds };
})()
"""

_JS_SCROLL = """
(async () => {
  const rounds = %(rounds)s;
  const delayMs = %(delay_ms)s;
  const pattern = /instagram\\.com\\/(?:p|reel|tv)\\/([A-Za-z0-9_-]+)/i;
  const state =
    globalThis.__ins2ankiScroll ||
    (globalThis.__ins2ankiScroll = { links: {}, stable: 0, rounds: 0 });
  const collect = () => {
    for (const anchor of document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
      const match = String(anchor.getAttribute("href") || "").match(pattern);
      if (match) state.links[match[1]] = `https://www.instagram.com/p/${match[1]}/`;
    }
  };
  for (let round = 0; round < rounds; round += 1) {
    const before = Object.keys(state.links).length;
    for (const element of document.querySelectorAll("div, main, section")) {
      if (element.scrollHeight > element.clientHeight + 200) {
        element.scrollTop = element.scrollHeight;
      }
    }
    window.scrollTo(0, document.body.scrollHeight);
    collect();
    state.rounds += 1;
    state.stable = Object.keys(state.links).length === before ? state.stable + 1 : 0;
    if (state.stable >= 3 && state.rounds > 3) break;
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  const collections = {};
  for (const anchor of document.querySelectorAll('a[href*="/saved/"]')) {
    const match = String(anchor.getAttribute("href") || "").match(/\\/saved\\/(?:[^/?#]+\\/)?(\\d+)/);
    const name = String(anchor.textContent || "").trim();
    if (match && name) collections[match[1]] = name;
  }
  return {
    links: Object.values(state.links),
    count: Object.keys(state.links).length,
    stable: state.stable,
    rounds: state.rounds,
    href: location.href,
    collections: Object.entries(collections).map(([id, name]) => ({
      id,
      name,
      source: "browser-dom",
    })),
  };
})()
"""

_JS_PERF_URLS = """
(() => {
  const seen = new Set();
  const urls = [];
  for (const entry of performance.getEntriesByType("resource")) {
    const name = String(entry.name || "");
    if (!/\\/api\\/v1\\/|graphql|\\/api\\/graphql/.test(name)) continue;
    if (seen.has(name)) continue;
    seen.add(name);
    urls.push(name);
  }
  return urls.slice(-80);
})()
"""

_JS_RESET_SCROLL = """
(() => {
  delete globalThis.__ins2ankiScroll;
  return true;
})()
"""

_JS_WHOAMI = """
(async () => {
  const cookieId = (document.cookie.match(/(?:^|;\\s*)ds_user_id=([^;]+)/) || [])[1] || "";
  const result = {
    cookie_user_id: cookieId,
    csrf: Boolean(__ins2anki.csrf),
    logged_in: Boolean(cookieId),
    username: "",
  };
  for (const path of [
    "/api/v1/accounts/edit/web_form_data/",
    "/api/v1/accounts/current_user/",
  ]) {
    try {
      const payload = await __ins2anki.getJson(path);
      const candidate =
        (payload.form_data && payload.form_data.username) ||
        (payload.user && payload.user.username) ||
        payload.username;
      if (candidate) {
        result.username = candidate;
        result.logged_in = true;
        break;
      }
    } catch (err) {
      result.error = String(err.message || err);
    }
  }
  return result;
})()
"""


# --------------------------------------------------------------------------
# Pure helpers (unit tested without a browser)
# --------------------------------------------------------------------------


def pk_to_shortcode(pk: str | int) -> str:
    """Convert an Instagram media id to its URL shortcode."""
    value = int(pk)
    if value <= 0:
        return ""
    digits = []
    while value:
        value, remainder = divmod(value, 64)
        digits.append(SHORTCODE_ALPHABET[remainder])
    return "".join(reversed(digits))


def shortcode_to_pk(code: str) -> int:
    """Convert a URL shortcode back to its media id."""
    value = 0
    for char in code:
        index = SHORTCODE_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"invalid shortcode character: {char!r}")
        value = value * 64 + index
    return value


def safe_filename(value: str, fallback: str = "instagram") -> str:
    """Normalize a user-supplied name into a safe file/dir component."""
    name = unicodedata.normalize("NFKC", value or "")
    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or fallback


def item_shortcode(item: dict) -> str:
    """Return the canonical shortcode for a projected API item."""
    code = str(item.get("code") or "").strip()
    if code:
        return code
    pk = str(item.get("pk") or "").strip()
    return pk_to_shortcode(pk) if pk else ""


def video_part_missing(item: dict) -> bool:
    """Whether the payload describes a video part without a direct URL.

    The saved and collection listings describe a reel by its cover image only:
    ``media_type`` 2 with no ``video_url``. Those items need their full
    description asked for before anything can be streamed.
    """
    for part in (item.get("children") or [item]):
        if part.get("media_type") == 2 and not str(part.get("video_url") or ""):
            return True
    return False


def item_media(item: dict) -> list[dict[str, str]]:
    """Flatten a projected item into an ordered list of downloadable media.

    Carousel children keep their API order; a video child with both a video and
    a cover image yields the video only. ``index`` is the 1-based position used
    to disambiguate file names.
    """
    children = item.get("children") or []
    parts: list[dict[str, Any]] = children if children else [item]
    media: list[dict[str, str]] = []
    for position, part in enumerate(parts, 1):
        video = str(part.get("video_url") or "")
        image = str(part.get("image_url") or "")
        if video:
            media.append({"kind": "video", "url": video, "index": position})
        elif image and part.get("media_type") != 2:
            media.append({"kind": "image", "url": image, "index": position})
    return media


def media_extension(url: str, kind: str) -> str:
    """Guess a file extension from a CDN URL, defaulting by media kind."""
    path = urllib.parse.urlsplit(url).path if "://" in url else url
    suffix = Path(path).suffix.lower()
    if suffix in (".mp4", ".jpg", ".jpeg", ".png", ".webp", ".heic"):
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".mp4" if kind == "video" else ".jpg"


def item_filename(item: dict, media: dict[str, str], multiple: bool) -> str:
    """Build the on-disk file name for one media part."""
    code = item_shortcode(item) or str(item.get("pk") or "instagram")
    stem = safe_filename(f"{code}_{item.get('username') or 'unknown'}")
    suffix = media_extension(media["url"], media["kind"])
    if multiple:
        return f"{stem}_{media['index']}{suffix}"
    return f"{stem}{suffix}"


def item_page_url(item: dict) -> str:
    """Return the canonical post URL used as the sync key."""
    code = item_shortcode(item)
    if code:
        return f"https://www.instagram.com/p/{code}/"
    return f"https://www.instagram.com/p/{item.get('pk', '')}/"


def trimmed_metadata(item: dict) -> dict:
    """Build the ``*.info.json`` payload stored next to the media."""
    return {
        "id": str(item.get("pk") or ""),
        "shortcode": item_shortcode(item),
        "webpage_url": item_page_url(item),
        "type": "instagram",
        "uploader": item.get("username") or None,
        "timestamp": item.get("taken_at") or None,
        "duration": item.get("duration"),
        "media_type": item.get("media_type"),
        "product_type": item.get("product_type") or None,
        "description": item.get("caption") or "",
        "children": [
            {"pk": str(part.get("pk") or ""), "media_type": part.get("media_type")}
            for part in (item.get("children") or [])
        ],
    }


def build_manifest(
    item: dict,
    files: list[Path],
    directory: Path,
    info_json: Path | None = None,
) -> dict:
    """Assemble the same manifest shape the yt-dlp downloader writes."""
    metadata = trimmed_metadata(item)
    metadata["info_json"] = str(info_json) if info_json else None
    return {
        "source_url": item_page_url(item),
        "platform": "instagram",
        "kind": "video" if any(part["kind"] == "video" for part in item_media(item)) else "images",
        "media": [str(path) for path in files],
        "metadata": [metadata],
        "session": {
            "downloader": "browser-session",
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "directory": str(directory),
        },
    }


RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504, 522, 524})


def _retry_delay(attempt: int, base: float = 0.6) -> float:
    """Exponential backoff with a little jitter, capped at ~8s."""
    return min(base * (2 ** (attempt - 1)), 8.0) * (0.85 + random.random() * 0.3)


def download_url(
    url: str,
    destination: Path,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
    chunk: int = 1 << 16,
    attempts: int = 4,
    retry_base: float = 0.6,
) -> int:
    """Stream a signed CDN URL to ``destination`` and return its size.

    Signed ``cdninstagram`` URLs carry their own credentials; no cookies are
    sent. Downloads land in a ``.part`` file first so an interrupted run never
    leaves a file that :func:`sync_common.valid_download` would accept.

    Instagram's CDN routinely drops connections mid-transfer (TLS
    ``UNEXPECTED_EOF_WHILE_READING``, ``IncompleteRead``, 5xx), so a failure
    here is retried with backoff and, when the server supports it, resumed with
    a ``Range`` request instead of restarting the file.
    """
    base_headers = dict(headers or DOWNLOAD_HEADERS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error = "unknown error"

    for attempt in range(1, max(attempts, 1) + 1):
        resume_from = partial.stat().st_size if partial.exists() else 0
        request_headers = dict(base_headers)
        if resume_from:
            request_headers["Range"] = f"bytes={resume_from}-"
        request = urllib.request.Request(url, headers=request_headers)
        written = resume_from
        expected: int | None = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                if content_type in {"text/html", "application/json"}:
                    partial.unlink(missing_ok=True)
                    raise MediaDownloadError(
                        f"media download returned {content_type} instead of media", response.status
                    )
                if response.status == 206:
                    content_range = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        str(response.headers.get("Content-Range") or ""),
                    )
                    if (content_range is None
                            or int(content_range[1]) != resume_from
                            or int(content_range[2]) < int(content_range[1])
                            or int(content_range[2]) >= int(content_range[3])):
                        # Never append bytes from a different offset to a partial.
                        last_error = "invalid Content-Range for resumed download"
                        partial.unlink(missing_ok=True)
                        time.sleep(_retry_delay(attempt, retry_base))
                        continue
                    total = content_range[3]
                    if total.isdigit():
                        expected = int(total)
                        if resume_from > expected:
                            # a stale partial (an older tool, a different
                            # stream) is longer than the real resource: start
                            # over instead of gluing mismatched bytes together
                            response.close()
                            partial.unlink(missing_ok=True)
                            time.sleep(_retry_delay(attempt, retry_base))
                            continue
                else:
                    length = response.headers.get("Content-Length")
                    if length and str(length).isdigit():
                        expected = int(length)
                        written = 0
                mode = "ab" if resume_from and response.status == 206 else "wb"
                if mode == "wb":
                    written = 0
                with partial.open(mode) as handle:
                    while True:
                        block = response.read(chunk)
                        if not block:
                            break
                        handle.write(block)
                        written += len(block)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(200).decode("utf-8", "replace")
            except Exception:  # pragma: no cover - diagnostics only
                pass
            finally:
                exc.close()
            last_error = f"HTTP {exc.code} for {url} {body}".strip()
            if exc.code == 416 and resume_from and attempt < attempts:
                # A stale or already-full partial cannot satisfy the range.
                # Restart once with a plain request instead of failing the item.
                partial.unlink(missing_ok=True)
                time.sleep(_retry_delay(attempt, retry_base))
                continue
            if exc.code not in RETRYABLE_STATUS or attempt >= attempts:
                partial.unlink(missing_ok=True)
                raise MediaDownloadError(f"media download failed: {last_error}", exc.code) from exc
            time.sleep(_retry_delay(attempt, retry_base))
            continue
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= attempts:
                partial.unlink(missing_ok=True)
                raise SessionError(f"media download failed: {last_error}") from exc
            time.sleep(_retry_delay(attempt, retry_base))
            continue

        if expected is not None and written != expected:
            last_error = f"size mismatch: {written}/{expected} bytes"
            if attempt >= attempts:
                partial.unlink(missing_ok=True)
                raise SessionError(f"media download failed: {last_error}")
            time.sleep(_retry_delay(attempt, retry_base))
            continue
        if written == 0:
            partial.unlink(missing_ok=True)
            raise SessionError(f"media download returned 0 bytes: {url}")
        partial.replace(destination)
        return written

    partial.unlink(missing_ok=True)
    raise SessionError(f"media download failed after {attempts} attempts: {last_error}")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class InstagramSession:
    """A CDP connection to a tab sitting on ``instagram.com``."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 60.0,
        origin: str = INSTAGRAM_ORIGIN,
        reuse_tab: bool = True,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.origin = origin
        self.reuse_tab = reuse_tab
        self.target: dict | None = None
        self.session: cdp.CdpSession | None = None
        # CdpSession serializes all commands, including origin checks and
        # navigation, so download workers cannot consume each other's replies.

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "InstagramSession":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if not is_browser_running(self.endpoint):
            raise SessionError(
                f"no browser is listening on {self.endpoint}. Run "
                "`browser_sync.py launch` once, log in, and keep that window open."
            )
        self.target = self._pick_tab()
        self.session = cdp.CdpSession(cdp.page_websocket_url(self.target), timeout=self.timeout)
        self.session.connect()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _pick_tab(self) -> dict:
        if self.reuse_tab:
            existing = cdp.find_page(self.endpoint, url_prefix=self.origin)
            if existing:
                return existing
        target = cdp.open_tab(self.origin, endpoint=self.endpoint)
        return target

    # -- helpers ---------------------------------------------------------

    def evaluate(self, expression: str, timeout: float | None = None) -> Any:
        if self.session is None:
            raise SessionError("session is not connected")
        return self.session.evaluate(expression, timeout=timeout or self.timeout)

    def origin_ready(self) -> bool:
        """Make sure the tab is on instagram.com so relative fetches work."""
        if self.session is None:
            raise SessionError("session is not connected")
        current = self.session.evaluate("location.origin", await_promise=False)
        if current == self.origin.rstrip("/"):
            return True
        self.session.navigate(self.origin)
        return True

    def whoami(self) -> dict:
        self.origin_ready()
        return self.evaluate(js_prelude() + _JS_WHOAMI)

    def collections(self) -> list[dict]:
        self.origin_ready()
        payload = self.evaluate(js_prelude() + _JS_LIST_COLLECTIONS)
        if not isinstance(payload, list):
            raise SessionError("unexpected collections response from the page")
        return payload

    def collection_items(
        self,
        collection_id: str,
        max_items: int = 5000,
        delay_ms: int = 400,
        timeout: float | None = None,
    ) -> dict:
        self.origin_ready()
        script = js_prelude() + _JS_COLLECTION_ITEMS % {
            "collection_id": json.dumps(str(collection_id)),
            "max_items": int(max_items),
            "delay_ms": int(delay_ms),
        }
        payload = self.evaluate(script, timeout=timeout or max(self.timeout, 600.0))
        if not isinstance(payload, dict):
            raise SessionError("unexpected collection response from the page")
        return payload

    def saved_items(
        self,
        max_items: int = 5000,
        delay_ms: int = 400,
        timeout: float | None = None,
    ) -> dict:
        self.origin_ready()
        script = js_prelude() + _JS_SAVED_ALL_ITEMS % {
            "max_items": int(max_items),
            "delay_ms": int(delay_ms),
        }
        payload = self.evaluate(script, timeout=timeout or max(self.timeout, 600.0))
        if not isinstance(payload, dict):
            raise SessionError("unexpected saved-items response from the page")
        return payload

    def media_info(
        self, pk: str, delay_ms: int = 250, timeout: float | None = None
    ) -> dict:
        """Fetch one media's full description straight from Instagram's API.

        The listing endpoints describe a reel by its cover image only, so the
        direct video URL has to be asked for per post before it can be
        streamed. Returns the same projected shape the listings produce.
        """
        self.origin_ready()
        script = js_prelude() + _JS_MEDIA_INFO % {
            "pk": json.dumps(str(pk)),
            "delay_ms": int(delay_ms),
            "fetch_timeout_ms": min(20000, max(100, int((timeout or self.timeout) * 500))),
        }
        for attempt in range(3):
            try:
                payload = self.evaluate(script, timeout=timeout or max(self.timeout, 60.0))
                break
            except cdp.CdpError as exc:
                if "HTTP 429 for " in str(exc):
                    raise RateLimitedError(str(exc)) from exc
                # A rejected fetch has no HTTP response. Retry this idempotent
                # read briefly; it is not evidence of an exhausted quota.
                transient = any(marker in str(exc) for marker in (
                    "TypeError: Failed to fetch", "Request timed out for ",
                    "HTTP 500 for ", "HTTP 502 for ", "HTTP 503 for ", "HTTP 504 for ",
                ))
                if not transient or attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))
        if not isinstance(payload, dict) or not payload.get("pk"):
            raise SessionError(f"no media info for {pk}")
        return payload

    def perf_urls(self, timeout: float | None = None) -> list[str]:
        """API paths the current page asked for; used by ``diagnose``."""
        if self.session is None:
            raise SessionError("session is not connected")
        payload = self.evaluate(_JS_PERF_URLS, timeout=timeout or self.timeout)
        return [str(value) for value in (payload or [])]

    def navigate(self, url: str, timeout: float | None = None) -> None:
        """Point the tab at ``url`` and wait for the document to be ready."""
        if self.session is None:
            raise SessionError("session is not connected")
        self.session.navigate(url, timeout=timeout or self.timeout)

    # -- replaying the page's own API calls ------------------------------

    def replay(
        self,
        request: dict,
        body: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Re-issue one of the page's own requests from inside the page.

        Cookies and CSRF state come from the page itself, so nothing is ever
        exported. The captured headers are replayed verbatim except for the
        ones a ``fetch`` must set itself.
        """
        if self.session is None:
            raise SessionError("session is not connected")
        url = str(request.get("url") or "")
        if not url.startswith(self.origin):
            raise SessionError(f"refusing to replay {url} outside {self.origin}")
        headers = {
            str(key): str(value)
            for key, value in (request.get("headers") or {}).items()
            if not str(key).startswith(":")
            and str(key).lower() not in REPLAY_DROPPED_HEADERS
        }
        expression = _JS_REPLAY % {
            "url": json.dumps(url),
            "method": json.dumps(str(request.get("method") or "GET").upper()),
            "headers": json.dumps(headers, ensure_ascii=False),
            "body": json.dumps(body) if body is not None else "null",
        }
        result = self.evaluate(expression, timeout=timeout or max(self.timeout, 180.0))
        if not isinstance(result, dict):
            raise SessionError("the page did not return a replay result")
        status = int(result.get("status") or 0)
        text = strip_graphql_prefix(str(result.get("text") or ""))
        if status >= 400:
            raise SessionError(f"replaying {url} answered HTTP {status}")
        if not text.startswith("{"):
            raise SessionError(
                f"replaying {url} answered HTTP {status} without JSON "
                "(the page needs to be reloaded on the collection)"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SessionError(f"replaying {url} returned invalid JSON") from exc

    def _feed_request(
        self, capture: "NetworkCapture", collection_id: str | None
    ) -> dict | None:
        """Pick the page's own saved-feed query out of its traffic."""
        candidates = [
            request
            for request in capture.requests
            if request.get("post_data") and "/graphql" in str(request.get("url") or "")
        ]
        if collection_id:
            for request in reversed(candidates):
                decoded = urllib.parse.unquote(str(request.get("post_data") or ""))
                if f'"collection_id":"{collection_id}"' in decoded:
                    return request
            return None
        # no id (the whole saved list): prefer a saved-feed friendly name
        for request in reversed(candidates):
            decoded = urllib.parse.unquote(str(request.get("post_data") or ""))
            if "collection_id" not in decoded and "Saved" in decoded:
                return request
        return candidates[-1] if candidates else None

    def collection_feed(
        self,
        url: str,
        collection_id: str | None = None,
        max_items: int = 5000,
        page_size: int = 50,
        delay_ms: int = 150,
        settle: float = 2.0,
        log=None,
        is_known=None,
    ) -> dict:
        """Enumerate a saved collection by replaying its own feed query.

        Deterministic and complete: the page's query is replayed with only its
        ``variables`` swapped, then paginated by ``page_info.end_cursor``.

        ``is_known``, when given, decides whether a shortcode is already
        archived. Saved collections are ordered newest-first, so once a whole
        page holds nothing new the rest cannot hold anything new either, and
        the walk stops — a routine sync costs one or two queries per
        collection instead of one per twelve items.
        """
        if self.session is None:
            raise SessionError("session is not connected")
        capture = NetworkCapture(self.session)
        capture.start()
        try:
            self.navigate(url)
            deadline = time.monotonic() + max(settle, 1.0)
            while time.monotonic() < deadline:
                time.sleep(0.4)
                capture.drain()
            request = self._feed_request(capture, collection_id or None)
            if not request:
                return {"items": [], "pages": 0, "request": None, "query": ""}
            form = urllib.parse.parse_qs(str(request.get("post_data") or ""))
            raw_variables = (form.get("variables") or ["{}"])[0]
            variables = json.loads(raw_variables)
            query = (form.get("fb_api_req_friendly_name") or [""])[0]
            variables["first"] = max(1, int(page_size))
            items: list[dict] = []
            seen: set[str] = set()
            cursor: str | None = None
            pages = 0
            # Pagination is silent otherwise: a 20-page collection replays
            # one query per page with nothing printed between them, which is
            # indistinguishable from a hang from the terminal watching it.
            emit = log or (lambda _message: None)
            while len(items) < max_items:
                variables["after"] = cursor
                body = swap_form_field(
                    str(request.get("post_data") or ""),
                    "variables",
                    json.dumps(variables),
                )
                payload = self.replay(request, body=body)
                limited = rate_limit_message(payload)
                if limited:
                    raise RateLimitedError(
                        f"Instagram is rate-limiting the saved-collection query "
                        f"({limited}); wait for the quota window to reset"
                    )
                pages += 1
                emit(f"page {pages}: {len(items)} item(s) so far")
                page_items = extract_media([payload])
                fresh = [
                    item
                    for item in page_items
                    if item_shortcode(item) and item_shortcode(item) not in seen
                ]
                seen.update(
                    code for code in (item_shortcode(item) for item in page_items) if code
                )
                items.extend(fresh)
                info = find_page_info(payload)
                cursor = info.get("end_cursor") or None
                codes = [item_shortcode(item) for item in page_items if item_shortcode(item)]
                if is_known is not None and codes and all(is_known(code) for code in codes):
                    emit(
                        f"page {pages}: {len(items)} item(s), the whole page is "
                        "already archived — stopping (incremental)"
                    )
                    return {
                        "items": items[:max_items],
                        "pages": pages,
                        "request": request,
                        "query": query,
                        "stopped_early": True,
                    }
                if not info.get("has_next_page") or not cursor or not fresh:
                    break
                if delay_ms:
                    time.sleep(delay_ms / 1000.0)
            return {
                "items": items[:max_items],
                "pages": pages,
                "request": request,
                "query": query,
            }
        finally:
            capture.stop()

    def capture_page(
        self,
        url: str,
        rounds: int = 16,
        delay_ms: int = 1200,
        max_items: int = 5000,
        timeout: float | None = None,
    ) -> dict:
        """Scroll a page while reading the JSON *it* fetched.

        This is the endpoint-agnostic path: Instagram retires REST routes
        (``/api/v1/collections/list/`` now answers 404 with the SPA shell), so
        rather than replaying guessed paths we let the page make its own calls
        and read the responses off the ``Network`` domain. Scrolling happens in
        small chunks so each body is collected while CDP still buffers it.
        """
        if self.session is None:
            raise SessionError("session is not connected")
        if not url.startswith(self.origin):
            raise SessionError(
                f"capture only works on {self.origin}; refusing to open {url}"
            )
        capture = NetworkCapture(self.session)
        capture.start()
        links: list[str] = []
        dom_collections: list[dict] = []
        api_urls: list[str] = []
        try:
            self.session.navigate(url, timeout=self.timeout)
            self.evaluate(_JS_RESET_SCROLL, timeout=self.timeout)
            capture.drain()
            chunk = max(1, min(4, int(rounds)))
            done = 0
            while done < rounds:
                step = min(chunk, rounds - done)
                result = self.evaluate(
                    _JS_SCROLL % {"rounds": step, "delay_ms": int(delay_ms)},
                    timeout=timeout or max(self.timeout, 600.0),
                )
                done += step
                capture.drain()
                if isinstance(result, dict):
                    links = [str(value) for value in (result.get("links") or [])]
                    dom_collections = [
                        item
                        for item in (result.get("collections") or [])
                        if isinstance(item, dict) and item.get("id")
                    ]
                    if result.get("stable", 0) >= 3 and result.get("rounds", 0) > 3:
                        break
                items = capture.media
                if len(items) >= max_items or len(links) >= max_items:
                    break
            try:
                api_urls = self.perf_urls()
            except (cdp.CdpError, SessionError):
                api_urls = []
        finally:
            capture.stop()
        collections = capture.collections
        known = {str(item.get("id")) for item in collections}
        collections += [item for item in dom_collections if str(item.get("id")) not in known]
        return {
            "url": url,
            "items": capture.media,
            "collections": collections,
            "links": links,
            "api_urls": api_urls,
            "payloads": len(capture.payloads),
        }

    def collections_via_capture(
        self,
        username: str,
        rounds: int = 10,
        delay_ms: int = 1200,
        timeout: float | None = None,
    ) -> list[dict]:
        """Read saved collections out of the saved page's own requests."""
        page = self.capture_page(
            collection_url(None, username=username, origin=self.origin),
            rounds=rounds,
            delay_ms=delay_ms,
            timeout=timeout,
        )
        return page["collections"]

    def items_via_capture(
        self,
        url: str,
        rounds: int = 16,
        delay_ms: int = 1200,
        max_items: int = 5000,
        timeout: float | None = None,
    ) -> dict:
        """Enumerate a saved page from the requests the page itself made."""
        page = self.capture_page(
            url, rounds=rounds, delay_ms=delay_ms, max_items=max_items, timeout=timeout
        )
        items = project_items(page["items"])
        return {
            "items": items,
            "pages": page["payloads"],
            "errors": [],
            "truncated": len(items) >= max_items,
            "links": page["links"],
            "api_urls": page["api_urls"],
        }

    def dom_links(
        self,
        url: str,
        rounds: int = 60,
        delay_ms: int = 1200,
        timeout: float | None = None,
    ) -> list[str]:
        """Scroll a rendered page and harvest post links (API fallback)."""
        if self.session is None:
            raise SessionError("session is not connected")
        if not url.startswith(self.origin):
            raise SessionError(
                f"DOM fallback only works on {self.origin}; refusing to open {url}"
            )
        self.session.navigate(url, timeout=self.timeout)
        script = _JS_DOM_LINKS % {"rounds": int(rounds), "delay_ms": int(delay_ms)}
        payload = self.evaluate(script, timeout=timeout or max(self.timeout, 600.0))
        urls = payload.get("urls") if isinstance(payload, dict) else None
        return [str(value) for value in (urls or [])]


# --------------------------------------------------------------------------
# Inventory projection
# --------------------------------------------------------------------------


def collection_url(
    collection_id: str | None = None,
    username: str = "",
    origin: str = INSTAGRAM_ORIGIN,
) -> str:
    """Build the saved-collection URL for a collection id or the "all" page."""
    base = f"{origin}{username}/saved" if username else str(origin)
    if collection_id:
        return f"{base.rstrip('/')}/_/{collection_id}/"
    return f"{base.rstrip('/')}/"


def match_collection(collections: list[dict], query: str) -> dict:
    """Resolve a collection by exact id, then exact name, then substring.

    Raises :class:`SessionError` with the available names when nothing matches,
    so a typo never silently syncs the wrong collection.
    """
    wanted = (query or "").strip()
    if not wanted:
        raise SessionError("a collection id or name is required")
    for collection in collections:
        if str(collection.get("id")) == wanted:
            return collection
    for collection in collections:
        if str(collection.get("name")).casefold() == wanted.casefold():
            return collection
    matches = [
        collection
        for collection in collections
        if wanted.casefold() in str(collection.get("name")).casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(
        f"{collection.get('name')} ({collection.get('id')})" for collection in collections
    )
    raise SessionError(
        f"no unique collection matches {wanted!r}; available: {available or 'none'}"
    )


#: A body is retried across this many drains before being abandoned. CDP does
#: not retain every response body (Instagram streams some of them), so the
#: replay path below does not depend on this at all — it only feeds the
#: scroll-and-capture fallback.
BODY_ATTEMPTS = 8

#: Instagram answers some GraphQL endpoints with an anti-JSON-hijacking prefix.
GRAPHQL_PREFIXES = ("for (;;);", "for(;;);", "while(1);")

#: Headers a page-side ``fetch`` must let the browser set for itself.
REPLAY_DROPPED_HEADERS = frozenset(
    {
        "content-length", "host", "connection", "accept-encoding", "accept-language",
        "cookie", "origin", "referer", "user-agent", "priority",
        "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
    }
)

_JS_REPLAY = """
(async () => {
  const response = await fetch(%(url)s, {
    method: %(method)s,
    headers: %(headers)s,
    body: %(body)s,
    credentials: "include",
  });
  return {status: response.status, text: await response.text()};
})()
"""


def strip_graphql_prefix(text: str) -> str:
    """Drop Instagram's anti-JSON-hijacking prefix, if present."""
    stripped = (text or "").lstrip()
    for prefix in GRAPHQL_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip()
    return stripped


def swap_form_field(post_data: str, field: str, value: str) -> str:
    """Replace one field of an urlencoded body, leaving every other byte alone.

    Rebuilding a GraphQL POST from scratch does not work: Instagram validates
    ``fb_dtsg``, ``lsd`` and ``fb_api_req_friendly_name`` together with the
    ``doc_id`` and answers a hand-rolled body with the SPA shell. Only the
    ``variables`` field may change.
    """
    encoded = f"{field}={urllib.parse.quote(value, safe='')}"
    parts = (post_data or "").split("&")
    replaced = False
    for index, part in enumerate(parts):
        if part.startswith(f"{field}="):
            parts[index] = encoded
            replaced = True
    if not replaced:
        parts.append(encoded)
    return "&".join(part for part in parts if part)


def find_page_info(payload: Any) -> dict:
    """Return the first GraphQL ``page_info`` object in a payload."""
    for node in _walk(payload):
        if isinstance(node, dict) and "has_next_page" in node:
            return node
    return {}

MEDIA_MARKERS = ("video_versions", "image_versions2", "carousel_media", "carousel_media_edits")


_PLAYABLE_CODEC_RE = re.compile(r"(avc1|hvc1|hev1)", re.IGNORECASE)
_UNPLAYABLE_CODEC_RE = re.compile(r"(vp09|vp9|av01)", re.IGNORECASE)


def _codec_tier(entry: dict) -> int:
    """0 for H.264/HEVC, 1 for unmarked, 2 for VP9/AV1."""
    marker = str(entry.get("type") or entry.get("mime_type") or "")
    if _PLAYABLE_CODEC_RE.search(marker):
        return 0
    if _UNPLAYABLE_CODEC_RE.search(marker):
        return 2
    return 1


def _pick_playable(entries: Any) -> dict | None:
    """Pick the widest video version QuickTime can actually decode.

    Instagram lists several ``video_versions`` and the widest is sometimes a
    VP9 or AV1 stream: it downloads fine and then refuses to open in
    QuickTime Player, Photos and Quick Look. Prefer avc1/hevc entries, then
    unmarked ones, and only fall back to VP9/AV1 when nothing else exists.
    """
    winner: tuple[int, int, dict] | None = None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        rank = _codec_tier(entry)
        width = 0
        for key in ("width", "config_width", "max_width"):
            try:
                width = int(entry.get(key) or 0)
            except (TypeError, ValueError):
                width = 0
            if width:
                break
        if winner is None or rank < winner[0] or (rank == winner[0] and width > winner[1]):
            winner = (rank, width, entry)
    return winner[2] if winner else None


def _pick_widest(entries: Any) -> dict | None:
    winner: tuple[int, dict] | None = None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        width = 0
        for key in ("width", "config_width", "max_width"):
            try:
                width = int(entry.get(key) or 0)
            except (TypeError, ValueError):
                width = 0
            if width:
                break
        if winner is None or width > winner[0]:
            winner = (width, entry)
    return winner[1] if winner else None


def project_media(media: Any) -> dict | None:
    """Project a raw media object, mirroring the page-side ``project()``.

    The REST fast path projects inside the page; payloads captured from the
    Network domain arrive raw, so the same mapping has to exist in Python.
    """
    if not isinstance(media, dict):
        return None
    children = media.get("carousel_media") or media.get("carousel_media_edits") or []
    if not any(marker in media for marker in MEDIA_MARKERS):
        return None
    pk = media.get("pk") or media.get("id") or ""
    code = media.get("code") or media.get("shortcode") or ""
    if not pk and not code:
        return None
    video = _pick_playable(media.get("video_versions"))
    image = _pick_widest(
        (media.get("image_versions2") or {}).get("candidates")
        or media.get("display_resources")
    )
    user = media.get("user") or media.get("owner") or {}
    caption = media.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    return {
        "pk": str(pk),
        "code": str(code),
        "media_type": media.get("media_type"),
        "product_type": media.get("product_type") or "",
        "taken_at": media.get("taken_at")
        or media.get("taken_at_timestamp")
        or media.get("device_timestamp")
        or 0,
        "username": (user.get("username") if isinstance(user, dict) else "")
        or media.get("username")
        or "",
        "caption": caption or media.get("caption_text") or "",
        "duration": media.get("video_duration"),
        "video_url": (video or {}).get("url") or (video or {}).get("src") or "",
        "image_url": (image or {}).get("url") or (image or {}).get("src") or "",
        "children": [
            child
            for child in (project_media(entry) for entry in children)
            if child is not None
        ],
    }


def _walk(node: Any):
    """Yield every JSON node in document order, depth first."""
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def extract_media(payloads: list[Any]) -> list[dict]:
    """Pull every media object out of arbitrary API/GraphQL payloads.

    A carousel is one item: its ``carousel_media`` children are folded into the
    parent, never emitted as posts of their own.
    """
    found: dict[str, dict] = {}
    for payload in payloads:
        stack: list[Any] = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if any(marker in node for marker in MEDIA_MARKERS):
                    item = project_media(node)
                    if item:
                        code = item_shortcode(item)
                        if code and code not in found:
                            found[code] = item
                    # do not descend into a carousel's children
                    stack.extend(
                        value
                        for key, value in reversed(list(node.items()))
                        if key not in ("carousel_media", "carousel_media_edits")
                    )
                else:
                    stack.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                stack.extend(reversed(node))
    return list(found.values())


def extract_collections(payloads: list[Any]) -> list[dict]:
    """Pull saved-collection descriptors out of the page's own payloads."""
    found: dict[str, dict] = {}
    for payload in payloads:
        for node in _walk(payload):
            if not isinstance(node, dict):
                continue
            raw_id = node.get("collection_id") or node.get("id")
            name = node.get("collection_name") or node.get("name")
            marked = (
                node.get("collection_type") is not None
                or node.get("media_count") is not None
                or node.get("collection_media_count") is not None
            )
            if not raw_id or not name or not marked:
                continue
            collection_id = str(raw_id).split(":")[-1]
            if not collection_id.isdigit() or not str(name).strip():
                continue
            count = node.get("media_count")
            if count is None:
                count = node.get("collection_media_count")
            found.setdefault(collection_id, {
                "id": collection_id,
                "name": str(name).strip(),
                "type": str(node.get("collection_type") or ""),
                "count": count,
                "source": "browser-network",
            })
    return list(found.values())


class NetworkCapture:
    """Read the JSON responses the page fetched for itself.

    Instagram renames and retires REST endpoints (``/api/v1/collections/list/``
    answers 404 with the SPA shell now), so guessing paths is fragile. Instead
    the page is allowed to make its own calls and the CDP ``Network`` domain
    hands us the bodies; :func:`extract_media` then harvests whatever shape the
    API returns, REST or GraphQL.
    """

    def __init__(
        self,
        session: "cdp.CdpSession",
        patterns: tuple[str, ...] = ("/api/v1/", "/graphql", "/api/graphql"),
    ) -> None:
        self.session = session
        self.patterns = tuple(patterns)
        self.payloads: list[Any] = []
        self.urls: list[str] = []
        #: raw request descriptors (url/method/headers/post_data) for the API
        #: calls the page made; the replay path needs the exact body
        self.requests: list[dict] = []
        self._pending: dict[str, str] = {}
        #: how many drains a pending body may survive before it is given up on
        self._attempts: dict[str, int] = {}
        #: request ids whose response body is known to be complete
        self._finished: set[str] = set()
        self._enabled = False

    def start(self) -> None:
        self.session.call(
            "Network.enable",
            {"maxTotalBufferSize": 128 << 20, "maxResourceBufferSize": 64 << 20},
        )
        self._enabled = True

    def stop(self) -> None:
        if self._enabled:
            try:
                self.session.call("Network.disable")
            except cdp.CdpError:
                pass
            self._enabled = False

    def interested(self, url: str) -> bool:
        return any(pattern in url for pattern in self.patterns)

    def _body(self, request_id: str) -> str | None:
        try:
            result = self.session.call(
                "Network.getResponseBody", {"requestId": request_id}, timeout=10.0
            )
        except cdp.CdpError:
            return None
        body = result.get("body")
        if body is None:
            return None
        if result.get("base64Encoded"):
            try:
                return base64.b64decode(body).decode("utf-8", "replace")
            except (ValueError, TypeError):
                return None
        return str(body)

    def drain(self) -> None:
        """Consume buffered events and fetch bodies for matching responses."""
        for event in self.session.drain_events():
            method = event.get("method")
            params = event.get("params") or {}
            request_id = str(params.get("requestId") or "")
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                url = str(request.get("url") or "")
                if (
                    params.get("type") in ("XHR", "Fetch")
                    and url
                    and self.interested(url)
                ):
                    self.requests.append(
                        {
                            "url": url,
                            "method": str(request.get("method") or "GET"),
                            "headers": dict(request.get("headers") or {}),
                            "post_data": request.get("postData") or "",
                        }
                    )
                continue
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                mime = str(response.get("mimeType") or "")
                # no mime check: Instagram serves JSON as text/javascript, so
                # requiring "json" silently dropped the collection feed itself
                if (
                    params.get("type") in ("XHR", "Fetch")
                    and request_id
                    and self.interested(url)
                ):
                    self._pending[request_id] = url
            elif method == "Network.loadingFailed":
                self._pending.pop(request_id, None)
                self._attempts.pop(request_id, None)
                self._finished.discard(request_id)
            elif method == "Network.loadingFinished" and request_id in self._pending:
                # the body is complete now: start its retry budget here rather
                # than dropping it (which would lose the whole feed response)
                self._finished.add(request_id)
                self._attempts[request_id] = 0
        for request_id, url in list(self._pending.items()):
            count = self._attempts.get(request_id, 0)
            if request_id in self._finished and count >= BODY_ATTEMPTS:
                self._pending.pop(request_id, None)
                self._attempts.pop(request_id, None)
                self._finished.discard(request_id)
                continue
            self._attempts[request_id] = count + 1
            body = self._body(request_id)
            if not body:
                # the response is still streaming: keep it pending and try
                # again on the next drain instead of dropping a payload that
                # may be the entire collection feed
                continue
            self._pending.pop(request_id, None)
            self._attempts.pop(request_id, None)
            self._finished.discard(request_id)
            try:
                parsed = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                continue
            self.urls.append(url)
            self.payloads.append(parsed)

    @property
    def media(self) -> list[dict]:
        return extract_media(self.payloads)

    @property
    def collections(self) -> list[dict]:
        return extract_collections(self.payloads)


def project_items(raw_items: list[dict]) -> list[dict]:
    """Normalize projected API items and drop anything without media."""
    result: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        code = item_shortcode(raw)
        if not code or code in seen:
            continue
        if not item_media(raw):
            continue
        seen.add(code)
        result.append(raw)
    return result


def build_inventory(
    name: str,
    items: list[dict],
    url: str = "",
    platform: str = "instagram",
    source: str = "browser-session",
) -> dict:
    """Build the inventory JSON shape ``sync_common.load_inventory`` accepts."""
    return {
        "version": 1,
        "platform": platform,
        "source": source,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collections": [
            {
                "name": name,
                "platform": platform,
                "url": url,
                "posts": [item_page_url(item) for item in items],
            }
        ],
    }
