#!/usr/bin/env python3
"""One command to mirror Xiaohongshu and Douyin favorites (收藏) locally.

Neither platform exposes an enumerable favorites feed to ``yt-dlp``, and their
web APIs are signed, so the list can only be produced by a logged-in page. This
first runs that path for you:

1. reuse the dedicated browser profile (``~/.ins2anki/browser-profile``) over
   CDP — no cookies are exported, so macOS never asks for Keychain access;
2. install a small harvest hook *before* the favorites page loads, so the JSON
   the page fetches for itself is captured as it scrolls;
3. read the note ids plus their freshly signed ``xsec_token`` (Xiaohongshu) or
   the play/image URLs (Douyin) out of that capture;
4. hand the result to the shared incremental engine (``sync_common``), which
   skips what is already on disk and retries what failed.

One-time setup is a login in the window ``launch`` opens; after that the whole
sync is::

    python3 scripts/favorites_sync.py sync --platform xiaohongshu \
        --output-dir xhs-saved/收藏

Why the token matters: Xiaohongshu only renders a note URL that carries a fresh
``xsec_token``. Cookies do not substitute for it — the same note fetched with the
browser's cookies but without a token comes back as an empty shell — while a
token-bearing URL works with no cookies at all.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_session  # noqa: E402
import cdp  # noqa: E402
import sync_common  # noqa: E402
from platforms import normalize_many  # noqa: E402

DEFAULT_ENDPOINT = cdp.DEFAULT_ENDPOINT
DEFAULT_PROFILE_DIR = browser_session.DEFAULT_PROFILE_DIR
DOWNLOADER = Path(__file__).with_name("download_media.py")

XHS_ORIGIN = "https://www.xiaohongshu.com"
XHS_FAVORITES_URL = f"{XHS_ORIGIN}/explore"
XHS_NOTE_URL = f"{XHS_ORIGIN}/explore/{{note_id}}"
DOUYIN_ORIGIN = "https://www.douyin.com"
DOUYIN_FAVORITES_URL = f"{DOUYIN_ORIGIN}/user/self?showTab=collection"

#: Session-scoped store the injected hook writes to. ``sessionStorage`` survives
#: the SPA's internal navigations, which is what lets one harvest span folders.
HARVEST_KEY = "__ins2anki_favorites_v1"
NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
AWEME_ID_RE = re.compile(r"^\d{8,25}$")

#: Cookie names that only appear once the platform is logged in. Used as a
#: cheap hint only — Xiaohongshu's session cookie is httpOnly, so the reliable
#: proof of a live session is the harvest returning records.
LOGIN_COOKIES = {
    "xiaohongshu": ("web_session", "web_session_id"),
    "douyin": ("sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token"),
}

SCRIPT_URL = {
    "xiaohongshu": XHS_FAVORITES_URL,
    "douyin": DOUYIN_FAVORITES_URL,
}

#: Instagram has its own enumerable feed and its own tool (``browser_sync.py``);
#: this one exists for the two platforms that have neither.
FAVORITES_PLATFORMS = ("xiaohongshu", "douyin")


# --------------------------------------------------------------------------
# Injected page JavaScript
# --------------------------------------------------------------------------

# The hook has to be installed before the page's own bundle runs, otherwise the
# first list response is already gone (the same trap the console exporter's
# comment warns about). ``Page.addScriptToEvaluateOnNewDocument`` gives us that,
# and everything is written to sessionStorage so a reload or an in-app route
# change does not lose what was collected.
HOOK_JS = r"""
(() => {
  const KEY = "__ins2anki_favorites_v1";
  const MAX_API = 200;
  // Xiaohongshu serves /api/sns/web/..., Douyin /aweme/v1/web/...; everything
  // else in the page is an asset and is ignored.
  const API_RE = /(\/api\/|\/aweme\/)/i;
  const SKIP_RE = /\.(js|css|png|jpe?g|webp|ico|svg|woff2?)(\?|$)/i;
  const NOTE_RE = /^[0-9a-f]{24}$/i;
  const AWEME_RE = /^\d{8,25}$/;
  const FOLDER_HINT = /(folder|notebook|collect)/i;
  const ID_KEYS = ["note_id", "noteId", "id", "aweme_id", "awemeId"];
  const TOKEN_KEYS = ["xsec_token", "xsecToken"];

  const state = (() => {
    try {
      const raw = sessionStorage.getItem(KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && typeof parsed === "object") return parsed;
    } catch (err) { /* start clean */ }
    return {};
  })();
  state.tokens = state.tokens || {};
  state.awemes = state.awemes || {};
  state.folders = state.folders || [];
  state.api = state.api || [];

  let dirty = false;
  const persist = () => {
    if (!dirty) return;
    dirty = false;
    try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (err) { /* quota */ }
  };

  const str = (value) => (typeof value === "string" ? value : "");

  function noteObject(node) {
    const id = ID_KEYS.map((key) => str(node[key])).find((value) => NOTE_RE.test(value));
    if (!id) return;
    for (const key of TOKEN_KEYS) {
      const token = str(node[key]);
      if (token.length > 16 && state.tokens[id] !== token) {
        state.tokens[id] = token;
        dirty = true;
      }
    }
  }

  function folderObject(node) {
    const name = str(node.name || node.folder_name || node.title || node.notebook_name);
    const id = str(node.id || node.folder_id || node.notebook_id || node.collection_id);
    if (!name || name.length > 60) return;
    if (!NOTE_RE.test(id) && !AWEME_RE.test(id)) return;
    const key = `${id}:${name}`;
    if (state.folders.some((item) => `${item.id}:${item.name}` === key)) return;
    state.folders.push({ id, name });
    dirty = true;
  }

  function urlList(value) {
    if (!value || typeof value !== "object") return [];
    const list = value.url_list || value.urlList || [];
    return (Array.isArray(list) ? list : []).filter((url) => typeof url === "string");
  }

  function awemeObject(node) {
    const id = str(node.aweme_id || node.awemeId);
    if (!AWEME_RE.test(id)) return;
    const video = node.video || {};
    const videos = [];
    for (const rate of video.bit_rate || video.bitRate || []) {
      for (const url of urlList(rate.play_addr || rate.playAddr)) videos.push(url);
    }
    for (const url of urlList(video.play_addr || video.playAddr)) videos.push(url);
    for (const url of urlList(video.download_addr || video.downloadAddr)) videos.push(url);
    const images = [];
    for (const image of node.images || node.image_list || []) {
      const urls = urlList(image);
      if (urls.length) images.push(urls[urls.length - 1]);
    }
    if (!videos.length && !images.length) return;
    const author = node.author || node.author_user_info || {};
    const record = {
      id,
      title: str(node.desc || node.title).slice(0, 200),
      author: str(author.nickname || author.unique_id || node.nickname),
      url: `https://www.douyin.com/${images.length && !videos.length ? "note" : "video"}/${id}`,
      videos: [...new Set(videos)],
      images: [...new Set(images)],
    };
    const previous = state.awemes[id];
    if (previous && previous.videos && previous.videos.length >= record.videos.length) return;
    state.awemes[id] = record;
    dirty = true;
  }

  function walk(node, depth) {
    if (!node || typeof node !== "object" || depth > 12) return;
    if (Array.isArray(node)) {
      for (const item of node) walk(item, depth + 1);
      return;
    }
    noteObject(node);
    awemeObject(node);
    const keys = Object.keys(node);
    if (keys.some((key) => FOLDER_HINT.test(key))) folderObject(node);
    for (const key of keys) walk(node[key], depth + 1);
  }

  function scan(text, url) {
    if (!text || text.length < 20) return;
    if (url && state.api.length < MAX_API && !state.api.includes(url)) {
      state.api.push(url);
      // Persist API paths even when nothing was mined: they are the only
      // evidence left when a harvest comes back empty.
      dirty = true;
    }
    let parsed = null;
    try { parsed = JSON.parse(text); } catch (err) { /* not JSON */ }
    if (parsed) walk(parsed, 0);
    else {
      // Proximity pairing for the rare non-JSON payload.
      const re = /"xsec_?[Tt]oken"\s*:\s*"([^"]{16,})"/g;
      let match;
      while ((match = re.exec(text))) {
        const window = text.slice(Math.max(0, match.index - 3000), match.index);
        const ids = [...window.matchAll(/"(?:note_?[Ii]d|id)"\s*:\s*"([0-9a-f]{24})"/gi)];
        if (ids.length) {
          state.tokens[ids[ids.length - 1][1]] = match[1];
          dirty = true;
        }
      }
    }
    if (dirty) persist();
  }

  const looksLikeApi = (url) =>
    typeof url === "string" && API_RE.test(url) && !SKIP_RE.test(url);

  // XHR is what the favorites list itself uses on both platforms.
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__ins2ankiUrl = String(url);
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        const url = this.__ins2ankiUrl || this.responseURL || "";
        if (looksLikeApi(url)) scan(this.responseText, url);
      } catch (err) { /* never break the page */ }
    });
    return send.apply(this, args);
  };

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      try {
        const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
        if (looksLikeApi(url)) response.clone().text().then((text) => scan(text, url)).catch(() => {});
      } catch (err) { /* never break the page */ }
      return response;
    };
  }

  globalThis.__ins2anki_harvest = state;
  // Both platforms also server-render their first page into the boot state, so
  // the same walker is exposed for the caller to run over it.
  globalThis.__ins2anki_scan_value = (value) => {
    try { walk(value, 0); if (dirty) persist(); } catch (err) { /* never break the page */ }
    return true;
  };
  globalThis.__ins2anki_harvest_clear = () => {
    state.tokens = {}; state.awemes = {}; state.folders = []; state.api = [];
    dirty = true; persist();
    return true;
  };
})();
"""

#: Scroll every scrollable container, then report how much has been captured.
#: Returning counts (not the payload) keeps each round cheap; the full store is
#: read once at the end.
SCROLL_JS = r"""
(() => {
  const state = globalThis.__ins2anki_harvest || {};
  // The first page of 收藏 is server-rendered into the boot state, so mine it
  // too instead of waiting for a request that may never happen.
  try {
    if (globalThis.__ins2anki_scan_value) {
      for (const key of ["__INITIAL_STATE__", "_ROUTER_DATA", "RENDER_DATA", "__NEXT_DATA__"]) {
        const value = globalThis[key];
        if (value && typeof value === "object") globalThis.__ins2anki_scan_value(value);
      }
      const rendered = document.getElementById("RENDER_DATA");
      if (rendered && rendered.textContent) {
        globalThis.__ins2anki_scan_value(JSON.parse(decodeURIComponent(rendered.textContent)));
      }
    }
  } catch (err) { /* the boot state is a bonus, not the source of truth */ }
  for (const el of document.querySelectorAll("div, main, section, ul")) {
    if (el.scrollHeight > el.clientHeight + 200) el.scrollTop = el.scrollHeight;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return {
    tokens: Object.keys(state.tokens || {}).length,
    awemes: Object.keys(state.awemes || {}).length,
    folders: (state.folders || []).length,
    api: (state.api || []).length,
    ready: document.readyState,
  };
})()
"""

READ_HARVEST_JS = r"""
(() => {
  // The live object is authoritative (and survives a storage quota failure);
  // sessionStorage is the fallback after a reload.
  const live = globalThis.__ins2anki_harvest;
  if (live && typeof live === "object") return live;
  try {
    const raw = sessionStorage.getItem("__ins2anki_favorites_v1");
    return raw ? JSON.parse(raw) : {};
  } catch (err) { return {}; }
})()
"""

CLEAR_HARVEST_JS = r"""
(() => {
  try { sessionStorage.removeItem("__ins2anki_favorites_v1"); } catch (err) {}
  if (globalThis.__ins2anki_harvest_clear) globalThis.__ins2anki_harvest_clear();
  return true;
})()
"""

#: Find the logged-in account id without calling a signed API: the profile link
#: is in the page's own DOM, and the id also appears in the boot state.
PROBE_UID_JS = r"""
(() => {
  const ids = new Set();
  const add = (value) => { if (/^[0-9a-f]{24}$/i.test(String(value))) ids.add(String(value)); };
  for (const anchor of document.querySelectorAll('a[href*="/user/profile/"]')) {
    const match = (anchor.getAttribute("href") || "").match(/\/user\/profile\/([0-9a-fA-F]{24})/);
    if (match) ids.add(match[1]);
  }
  const scan = (node, depth) => {
    if (!node || typeof node !== "object" || depth > 8) return;
    for (const [key, value] of Object.entries(node)) {
      if (typeof value === "string") { if (/user_?id$/i.test(key)) add(value); }
      else scan(value, depth + 1);
    }
  };
  try { scan(globalThis.__INITIAL_STATE__, 0); } catch (err) {}
  try {
    for (const key of Object.keys(localStorage)) {
      const value = localStorage.getItem(key);
      if (typeof value === "string" && value.length < 50000 && value.includes("user")) {
        try { scan(JSON.parse(value), 0); } catch (err) {}
      }
    }
  } catch (err) {}
  return Array.from(ids);
})()
"""

LOGIN_PROBE_JS = r"""
(() => {
  const cookies = document.cookie || "";
  const names = %(names)s;
  const found = names.filter((name) => cookies.includes(name + "="));
  return { cookies: found, url: location.href };
})()
"""


# --------------------------------------------------------------------------
# Harvest -> items
# --------------------------------------------------------------------------


def xhs_items(harvest: dict) -> list[str]:
    """Return token-bearing note URLs for every captured note id."""
    tokens = harvest.get("tokens") if isinstance(harvest, dict) else None
    if not isinstance(tokens, dict):
        return []
    urls: list[str] = []
    for note_id, token in tokens.items():
        if not NOTE_ID_RE.match(str(note_id)):
            continue
        suffix = ""
        if isinstance(token, str) and token:
            suffix = "?" + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        urls.append(XHS_NOTE_URL.format(note_id=note_id) + suffix)
    return sorted(urls)


def folders_of(harvest: dict) -> list[dict]:
    """Return the capture's folder candidates, deduplicated by (id, name)."""
    raw = harvest.get("folders") if isinstance(harvest, dict) else None
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        folder_id = str(item.get("id") or "").strip()
        if not name or (folder_id, name) in seen:
            continue
        seen.add((folder_id, name))
        result.append({"id": folder_id, "name": name})
    return result


def awemes_of(harvest: dict) -> dict[str, dict]:
    """Return Douyin media records keyed by aweme id."""
    raw = harvest.get("awemes") if isinstance(harvest, dict) else None
    result: dict[str, dict] = {}
    for aweme_id, item in (raw or {}).items():
        if not AWEME_ID_RE.match(str(aweme_id)) or not isinstance(item, dict):
            continue
        videos = [url for url in item.get("videos") or [] if isinstance(url, str)]
        images = [url for url in item.get("images") or [] if isinstance(url, str)]
        if not videos and not images:
            continue
        result[str(aweme_id)] = {
            "id": str(aweme_id),
            "url": str(item.get("url") or f"{DOUYIN_ORIGIN}/video/{aweme_id}"),
            "title": str(item.get("title") or ""),
            "author": str(item.get("author") or ""),
            "videos": videos,
            "images": images,
        }
    return result


def discovered_for(platform: str, harvest: dict) -> list[tuple[str, str, str]]:
    """Build the ``(platform, item_id, url)`` list the sync engine consumes."""
    if platform == "xiaohongshu":
        return normalize_many(xhs_items(harvest), "xiaohongshu")
    awemes = awemes_of(harvest)
    return [(platform, aweme["id"], aweme["url"]) for aweme in awemes.values()]


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class FavoritesSession:
    """A CDP connection to a tab sitting on one platform's origin."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 60.0,
        origin: str = XHS_ORIGIN,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.origin = origin
        self.target: dict | None = None
        self.session: cdp.CdpSession | None = None
        self._hook_installed = False

    def __enter__(self) -> "FavoritesSession":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if not browser_session.is_browser_running(self.endpoint):
            raise browser_session.SessionError(
                f"no browser is listening on {self.endpoint}. Run "
                "`favorites_sync.py launch` once, log in, and keep that window open."
            )
        self.target = cdp.find_page(self.endpoint, url_prefix=self.origin) or cdp.open_tab(
            self.origin, endpoint=self.endpoint
        )
        self.session = cdp.CdpSession(
            cdp.page_websocket_url(self.target), timeout=self.timeout
        )
        self.session.connect()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def evaluate(self, expression: str, timeout: float | None = None) -> Any:
        if self.session is None:
            raise browser_session.SessionError("session is not connected")
        return self.session.evaluate(expression, timeout=timeout or self.timeout)

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if self.session is None:
            raise browser_session.SessionError("session is not connected")
        return self.session.call(method, params, timeout=timeout or self.timeout)

    # -- harvest ---------------------------------------------------------

    def install_hook(self) -> None:
        """Make the hook run before every future document in this tab."""
        if self._hook_installed:
            return
        self.call("Page.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS})
        self._hook_installed = True

    def harvest(
        self,
        url: str | None = None,
        rounds: int = 120,
        delay_ms: int = 1000,
        stable_rounds: int = 3,
        reset: bool = True,
        log=None,
    ) -> dict:
        """Scroll the favorites list and return the captured store.

        ``url`` is loaded first when given; omit it to keep working in the page
        you are already on (that is how a sidebar 收藏夹 is harvested after it
        was clicked).
        """
        emit = log or (lambda _message: None)
        self.install_hook()
        if reset:
            self.evaluate(CLEAR_HARVEST_JS)
        if url:
            self.session.navigate(url, timeout=self.timeout)
        time.sleep(max(delay_ms, 200) / 1000.0)

        stable = 0
        previous = -1
        counts: dict = {}
        for index in range(1, max(rounds, 1) + 1):
            counts = self.evaluate(SCROLL_JS, timeout=self.timeout) or {}
            total = int(counts.get("tokens", 0)) + int(counts.get("awemes", 0))
            emit(
                f"scroll {index}: {counts.get('tokens', 0)} note(s), "
                f"{counts.get('awemes', 0)} douyin item(s), "
                f"{counts.get('folders', 0)} folder candidate(s)"
            )
            if total == previous:
                stable += 1
                if stable >= stable_rounds:
                    break
            else:
                stable = 0
            previous = total
            time.sleep(max(delay_ms, 200) / 1000.0)

        harvest = self.evaluate(READ_HARVEST_JS, timeout=self.timeout) or {}
        return harvest if isinstance(harvest, dict) else {}

    def resolve_uid(self) -> str:
        """Return the logged-in Xiaohongshu account id, if the page exposes it."""
        candidates = self.evaluate(PROBE_UID_JS, timeout=self.timeout) or []
        for value in candidates if isinstance(candidates, list) else []:
            if NOTE_ID_RE.match(str(value)):
                return str(value)
        return ""


# --------------------------------------------------------------------------
# Douyin media download
# --------------------------------------------------------------------------


def douyin_download(record: dict, directory: Path) -> tuple[bool, str]:
    """Save a Douyin item's media straight from the page's own URLs.

    Douyin refuses anonymous extraction by ``yt-dlp``, but the URLs the page
    itself received are plain CDN links; they only want the site's referer.
    """
    headers = {
        "User-Agent": browser_session.USER_AGENT,
        "Referer": f"{DOUYIN_ORIGIN}/",
        "Accept": "*/*",
    }
    author = browser_session.safe_filename(record.get("author") or "douyin")
    directory.mkdir(parents=True, exist_ok=True)
    media: list[str] = []

    if record.get("videos"):
        target = directory / f"{record['id']}_{author}.mp4"
        try:
            browser_session.download_url(record["videos"][0], target, headers=headers)
        except browser_session.SessionError as exc:
            return False, str(exc)
        media.append(str(target))
    elif record.get("images"):
        for index, url in enumerate(record["images"], 1):
            target = directory / f"{record['id']}_{author}_{index}.jpg"
            try:
                browser_session.download_url(url, target, headers=headers)
            except browser_session.SessionError as exc:
                return False, str(exc)
            media.append(str(target))

    if not media:
        return False, "the page exposed neither a video nor image url for this item"

    manifest = {
        "source_url": record.get("url", ""),
        "platform": "douyin",
        "kind": "video" if record.get("videos") else "images",
        "media": media,
        "metadata": [{
            "id": record.get("id"),
            "title": record.get("title"),
            "uploader": record.get("author"),
            "webpage_url": record.get("url"),
        }],
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True, f"{len(media)} file(s)"


def douyin_download_fn(records: dict[str, dict]):
    """Adapt :func:`douyin_download` to the shared engine's download hook."""

    def download(_downloader, url, output_dir, _cookies, _cookies_from_browser):
        record = records.get(url)
        if record is None:
            return False, f"no captured media for {url}"
        return douyin_download(record, Path(output_dir))

    return download


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def open_session(args: argparse.Namespace) -> FavoritesSession:
    if not browser_session.is_browser_running(args.endpoint):
        if not args.launch:
            raise browser_session.SessionError(
                f"no browser is listening on {args.endpoint}. Run "
                "`favorites_sync.py launch` once, or pass --launch."
            )
        browser_session.launch_browser(
            browser=args.browser,
            profile_dir=Path(args.profile_dir),
            port=args.port,
            url=SCRIPT_URL[args.platform],
        )
    origin = args.origin or SCRIPT_URL[args.platform]
    return FavoritesSession(endpoint=args.endpoint, timeout=args.timeout, origin=origin)


def favorites_url(args: argparse.Namespace, session: FavoritesSession) -> str:
    """Resolve the 收藏 page for this account."""
    if args.favorites_url:
        return args.favorites_url
    if args.platform == "douyin":
        return DOUYIN_FAVORITES_URL
    session.session.navigate(XHS_FAVORITES_URL, timeout=args.timeout)
    time.sleep(1.0)
    uid = session.resolve_uid()
    if not uid:
        raise browser_session.SessionError(
            "cannot find your Xiaohongshu account id; open your 收藏 page and pass "
            "it explicitly with --favorites-url 'https://www.xiaohongshu.com/user/profile/<uid>?tab=fav&subTab=note'"
        )
    return f"{XHS_ORIGIN}/user/profile/{uid}?tab=fav&subTab=note"


def login_hint(args: argparse.Namespace, session: FavoritesSession) -> dict:
    names = LOGIN_COOKIES.get(args.platform, ())
    probe = session.evaluate(LOGIN_PROBE_JS % {"names": json.dumps(list(names))})
    return probe if isinstance(probe, dict) else {}


def click_folder(session: FavoritesSession, name: str, timeout: float) -> bool:
    """Click a 收藏夹 in the page's sidebar by its visible text."""
    script = """
    (() => {
      const wanted = %(name)s;
      const nodes = Array.from(document.querySelectorAll("div, li, a, span"));
      const hit = nodes.find(
        (node) => (node.textContent || "").trim() === wanted &&
          node.children.length === 0
      );
      if (!hit) return false;
      let target = hit;
      for (let depth = 0; depth < 4 && target.parentElement; depth++) {
        target = target.parentElement;
        if (target.tagName === "A" || target.getAttribute("role") === "button") break;
      }
      target.click();
      return true;
    })()
    """ % {"name": json.dumps(name)}
    return bool(session.evaluate(script, timeout=timeout))


def command_launch(args: argparse.Namespace) -> int:
    process, endpoint = browser_session.launch_browser(
        browser=args.browser,
        profile_dir=Path(args.profile_dir),
        port=args.port,
        url=SCRIPT_URL[args.platform],
    )
    print(json.dumps({
        "endpoint": endpoint,
        "profile_dir": str(Path(args.profile_dir).expanduser()),
        "pid": process.pid,
        "next": [
            f"log into {args.platform} in the window that just opened",
            "keep it open, then run: favorites_sync.py sync --platform "
            f"{args.platform} --output-dir <dir>",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def command_check(args: argparse.Namespace) -> int:
    with open_session(args) as session:
        hint = login_hint(args, session)
        url = favorites_url(args, session)
        harvest = session.harvest(
            url,
            rounds=min(args.scroll_rounds, 6),
            delay_ms=args.scroll_delay_ms,
            stable_rounds=2,
            log=log if args.verbose else None,
        )
    items = discovered_for(args.platform, harvest)
    folders = folders_of(harvest)
    payload = {
        "platform": args.platform,
        "favorites_url": url,
        "session_cookies": hint.get("cookies", []),
        "logged_in": bool(items) or bool(hint.get("cookies")),
        "found": len(items),
        "folders": [folder["name"] for folder in folders],
        "api_paths": harvest.get("api", [])[:20],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["logged_in"] else 2


def command_diagnose(args: argparse.Namespace) -> int:
    with open_session(args) as session:
        url = favorites_url(args, session)
        harvest = session.harvest(
            url, rounds=args.scroll_rounds, delay_ms=args.scroll_delay_ms, log=log
        )
    payload = {
        "favorites_url": url,
        "notes_with_token": len(harvest.get("tokens", {}) or {}),
        "douyin_items": len(harvest.get("awemes", {}) or {}),
        "folders": folders_of(harvest),
        "api_paths_called": harvest.get("api", []),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if (payload["notes_with_token"] or payload["douyin_items"]) else 2


def command_sync(args: argparse.Namespace) -> int:
    with open_session(args) as session:
        url = favorites_url(args, session)
        harvest = session.harvest(
            url, rounds=args.scroll_rounds, delay_ms=args.scroll_delay_ms, log=log
        )
        if args.folder:
            if click_folder(session, args.folder, args.timeout):
                log(f"opened 收藏夹 「{args.folder}」")
                time.sleep(1.5)
                harvest = session.harvest(
                    rounds=args.scroll_rounds,
                    delay_ms=args.scroll_delay_ms,
                    reset=False,
                    log=log,
                )
            else:
                log(f"warning: no sidebar entry named 「{args.folder}」; syncing the current list")

    discovered = discovered_for(args.platform, harvest)
    if not discovered:
        print(json.dumps({
            "error": "the page exposed no favorites; is this profile logged in?",
            "favorites_url": url,
            "api_paths_called": harvest.get("api", [])[:20],
            "hint": "run `favorites_sync.py check` after logging in, or `diagnose` for the API paths",
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    log(f"captured {len(discovered)} item(s); starting the incremental download")
    records = awemes_of(harvest) if args.platform == "douyin" else {}
    download_fn = (
        douyin_download_fn({record["url"]: record for record in records.values()})
        if args.platform == "douyin"
        else sync_common.run_download
    )
    output_dir = Path(args.output_dir)
    state_file = Path(args.state_file) if args.state_file else output_dir / "sync-state.json"
    return sync_common.sync_items(
        discovered,
        output_dir=output_dir,
        state_file=state_file,
        downloader=DOWNLOADER,
        download_fn=download_fn,
        source_label=url,
        cookies=None,
        cookies_from_browser=None,
        retry_failed=not args.no_retry_failed,
        dry_run=args.dry_run,
        limit=args.limit,
        jobs=max(args.jobs, 1),
    )


def add_session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="DevTools endpoint")
    parser.add_argument(
        "--platform",
        choices=FAVORITES_PLATFORMS,
        default="xiaohongshu",
        help="which 收藏 to mirror",
    )
    parser.add_argument("--origin", help="page origin override (advanced/testing)")
    parser.add_argument("--timeout", type=float, default=60.0, help="CDP timeout in seconds")
    parser.add_argument("--launch", action="store_true", help="start the browser if needed")
    parser.add_argument("--browser", help="browser executable (default: first found)")
    parser.add_argument("--port", type=int, default=9222, help="debug port for --launch")
    parser.add_argument(
        "--profile-dir", default=str(DEFAULT_PROFILE_DIR), help="dedicated profile directory"
    )
    parser.add_argument(
        "--favorites-url",
        help="收藏 page URL (skips account-id discovery; handy for tests)",
    )
    parser.add_argument(
        "--scroll-rounds", type=int, default=120, help="max scroll rounds (stops early)"
    )
    parser.add_argument("--scroll-delay-ms", type=int, default=1000)
    parser.add_argument("--verbose", action="store_true", help="print every scroll round")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mirror Xiaohongshu or Douyin favorites through a live browser session"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="open the dedicated browser and log in")
    add_session_arguments(launch)
    launch.set_defaults(func=command_launch)

    check = subparsers.add_parser("check", help="report login state and what was found")
    add_session_arguments(check)
    check.set_defaults(func=command_check)

    diagnose = subparsers.add_parser("diagnose", help="dump the API paths the page called")
    add_session_arguments(diagnose)
    diagnose.set_defaults(func=command_diagnose)

    sync = subparsers.add_parser("sync", help="download the favorites incrementally")
    add_session_arguments(sync)
    sync.add_argument("--output-dir", required=True, type=Path)
    sync.add_argument("--state-file", type=Path)
    sync.add_argument("--folder", help="a named 收藏夹 in the sidebar (default: current list)")
    sync.add_argument("--limit", type=int, help="maximum downloads this run")
    sync.add_argument("--jobs", type=int, default=1, help="parallel downloads")
    sync.add_argument("--no-retry-failed", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sync.set_defaults(func=command_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except browser_session.SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except cdp.CdpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
