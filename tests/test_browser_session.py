"""Tests for the CDP browser-session sync path.

Two layers:

* pure helpers (shortcode math, media selection, file names, manifests, CLI
  wiring) which need no browser at all;
* an end-to-end test that drives a real headless Chromium against a mock
  Instagram API, proving the in-page fetch, pagination, signed-URL download,
  manifest layout and incremental state all work together.

Set ``INS2ANKI_SKIP_BROWSER_TESTS=1`` to skip the browser layer, or point
``INS2ANKI_TEST_BROWSER`` at a browser executable to override discovery.
"""

import argparse
import http.server
import importlib.util
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name: str):
    """Import a script module, sharing one instance with normal imports."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
CDP = load("cdp")
BROWSER_SESSION = load("browser_session")
BROWSER_SYNC = load("browser_sync")
SYNC_COMMON = load("sync_common")


# --------------------------------------------------------------------------
# Mock Instagram
# --------------------------------------------------------------------------


def _media_item(origin, pk, code, username, taken_at, kind="video", children=None):
    item = {
        "pk": pk,
        "code": code,
        "media_type": 2 if kind == "video" else 1,
        "product_type": "clips",
        "taken_at": taken_at,
        "user": {"username": username},
        "caption": {"text": f"caption for {code}"},
        "video_duration": 12.5,
        "video_versions": [
            {"width": 640, "url": f"{origin}/media/{code}_low.mp4"},
            {"width": 1080, "url": f"{origin}/media/{code}_high.mp4"},
        ],
        "image_versions2": {
            "candidates": [{"width": 640, "url": f"{origin}/media/{code}.jpg"}]
        },
    }
    if kind == "image":
        item["video_versions"] = []
    # the page-side projection flattens user.username to the top level
    item["username"] = username
    item["video_url"] = (item["video_versions"][-1]["url"] if item["video_versions"] else "")
    item["image_url"] = item["image_versions2"]["candidates"][0]["url"]
    if children:
        item["media_type"] = 8
        item["carousel_media"] = children
    return item


def _child(origin, pk, code, username, taken_at, kind="video"):
    return _media_item(origin, pk, code, username, taken_at, kind=kind)


SAVED_PAGE_JS = """
<h1>saved</h1>
<nav>%(collection_links)s</nav>
<div id="grid"></div>
<div style="height: 4000px"></div>
<script>
const target = %(collection_id)s;
const base = target
  ? `/graphql/query/collection-items/?id=${target}`
  : "/graphql/query/saved-items/";
// the collection feed is a POST to /api/graphql whose body carries doc_id plus
// a variables blob; Instagram answers with data.fetch__MediaCollection.media
const feedBody = (after) => new URLSearchParams({
  doc_id: "29192959536956308",
  variables: JSON.stringify({after: after, collection_id: target, first: 12}),
  fb_api_req_friendly_name: "PolarisSavedCollectionPageWWWQuery",
  lsd: "mock-lsd",
  fb_dtsg: "mock-dtsg",
  jazoest: "26149",
}).toString();
const nodes = (payload) => {
  const collection = payload && payload.data && payload.data.fetch__MediaCollection;
  if (collection && collection.media) {
    return (collection.media.edges || []).map((edge) => edge.node);
  }
  return (payload && payload.data && payload.data.items) || [];
};
const render = (data) => {
  const grid = document.getElementById("grid");
  for (const node of nodes(data)) {
    const code = node.code || "";
    if (!code) continue;
    const anchor = document.createElement("a");
    anchor.setAttribute("href", `/p/${code}/`);
    anchor.textContent = code;
    grid.appendChild(anchor);
  }
};
const get = async (url) => {
  try {
    const response = await fetch(url);
    return response.ok ? await response.json() : null;
  } catch (error) {
    return null;
  }
};
const post = async (url, body) => {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {"content-type": "application/x-www-form-urlencoded"},
      body: body,
    });
    return response.ok ? await response.json() : null;
  } catch (error) {
    return null;
  }
};
const feedPage = (after) => post("/api/graphql", feedBody(after));
(async () => {
  const first = await feedPage(null);
  if (first) {
    render(first);
    window.__ins2ankiLoadedFirstPage = true;
  }
  if (!target && %(with_collections)s) {
    window.__ins2ankiCollections = await get("/graphql/query/collections/");
  }
  let page = 1;
  const onScroll = async () => {
    if (page >= 2) return;
    page += 1;
    const next = await feedPage(first && first.data && first.data.fetch__MediaCollection
      && first.data.fetch__MediaCollection.media
      && first.data.fetch__MediaCollection.media.page_info.end_cursor);
    if (next) render(next);
    window.removeEventListener("scroll", onScroll);
  };
  window.addEventListener("scroll", onScroll);
})();
</script>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    origin = "http://127.0.0.1:0"
    collection_id = "1234567890123456"
    media_bytes = b"\x11" * 2048
    # when False, the saved-collection REST routes answer 404 with the SPA
    # shell, exactly like Instagram does after retiring an endpoint, while the
    # account route keeps working and the page keeps fetching its own data.
    rest_available = True
    collections_available = True
    collection_links = True

    @classmethod
    def page_one(cls):
        origin = cls.origin
        return {
            "items": [
                _media_item(origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000),
                _media_item(origin, "3546164236708408292", "DDef456", "demo_user", 1751500001, kind="image"),
            ],
            "more_available": True,
            "next_max_id": "CURSOR1",
            "status": "ok",
        }

    @classmethod
    def page_two(cls):
        origin = cls.origin
        return {
            "items": [
                _media_item(
                    origin,
                    "3700000000000000000",
                    "DGhi789",
                    "someone",
                    1751500004,
                    children=[
                        _child(origin, "3600000000000000001", "DCh1", "someone", 1751500002),
                        _child(origin, "3600000000000000002", "DCh2", "someone", 1751500003, kind="image"),
                    ],
                )
            ],
            "more_available": False,
            "next_max_id": "",
            "status": "ok",
        }

    def do_POST(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = urllib.parse.parse_qs(raw)
        if parsed.path != "/api/graphql":
            return self.send_error(404)
        # a hand-rolled body (no fb_dtsg/lsd) is answered with the SPA shell,
        # exactly like Instagram does; only the page's own body is accepted
        if not form.get("fb_dtsg") or not form.get("lsd"):
            return self.send_spa_shell()
        variables = json.loads((form.get("variables") or ["{}"])[0])
        wanted = str(variables.get("collection_id") or "")
        if wanted and wanted != self.collection_id:
            return self.send_json({"data": {"fetch__MediaCollection": None}})
        after = variables.get("after")
        payload = self.page_two() if after else self.page_one()
        self.server.media_requests = getattr(self.server, "media_requests", 0) + 1
        return self.send_json({"data": {"fetch__MediaCollection": {
            "name": "自然",
            "media": {
                "edges": [{"node": item} for item in payload["items"]],
                "page_info": {
                    "has_next_page": bool(payload["more_available"]),
                    "end_cursor": payload["next_max_id"] or None,
                },
            },
        }}})

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/v1/collections/list/":
            if not self.rest_available:
                return self.send_spa_shell()
            return self.send_json({
                "status": "ok",
                "items": [{
                    "collection_id": self.collection_id,
                    "collection_name": "自然",
                    "collection_type": "REGULAR",
                    "media_count": 3,
                }],
            })
        if path == f"/api/v1/feed/collection/{self.collection_id}/posts/":
            if not self.rest_available:
                return self.send_spa_shell()
            return self.send_json(self.page_two() if query.get("max_id") else self.page_one())
        if path == "/api/v1/feed/saved/posts/":
            if not self.rest_available:
                return self.send_spa_shell()
            return self.send_json(self.page_two())
        if path == "/api/v1/accounts/edit/web_form_data/":
            return self.send_json({"status": "ok", "form_data": {"username": "demo_user"}})

        # the SPA's own GraphQL calls: what the page fetches for itself
        if path == "/graphql/query/collections/":
            return self.send_json({"data": {"collections": [{
                "id": f"collection:{self.collection_id}",
                "name": "自然",
                "collection_type": "REGULAR",
                "media_count": 3,
            }]}})
        if path == "/graphql/query/collection-items/":
            if query.get("id", [""])[0] != self.collection_id:
                return self.send_error(404)
            payload = self.page_two() if query.get("page") else self.page_one()
            return self.send_json({"data": {"items": payload["items"]}})
        if path == "/graphql/query/saved-items/":
            payload = self.page_two() if query.get("page") else self.page_one()
            return self.send_json({"data": {"items": payload["items"]}})

        if path.startswith("/media/") and "nope" not in path:
            # every media route can misbehave: the CDN drops connections
            remaining = getattr(self.server, "flaky_remaining", 0)
            if remaining > 0:
                self.server.flaky_remaining = remaining - 1
                return self.serve_truncated()
            return self.serve_media()
        if path in ("/", "/demo_user/saved/") or path.startswith("/demo_user/saved/_/"):
            collection_id = ""
            if path.startswith("/demo_user/saved/_/"):
                collection_id = path.rstrip("/").rsplit("/", 1)[-1]
            body = (
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                "<title>mock instagram</title></head><body>"
                + (
                    SAVED_PAGE_JS
                    % {
                        "collection_id": json.dumps(collection_id),
                        "with_collections": "true" if self.collections_available else "false",
                        "collection_links": (
                            f'<a href="/demo_user/saved/_/{self.collection_id}/">自然</a>'
                            if self.collection_links
                            else ""
                        ),
                    }
                )
                + "</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "csrftoken=MOCKCSRF; Path=/")
            self.send_header("Set-Cookie", "ds_user_id=42; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def serve_truncated(self):
        """Declare the full length, send 64 bytes, then drop the connection."""
        if True:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(self.media_bytes)))
            self.end_headers()
            self.wfile.write(self.media_bytes[:64])
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

    def serve_media(self):
        """Serve the body, honouring Range so a resume can finish the file."""
        start = 0
        header = self.headers.get("Range") or ""
        if header.startswith("bytes="):
            self.server.range_requests = getattr(self.server, "range_requests", 0) + 1
            try:
                start = int(header[len("bytes="):].split("-")[0])
            except ValueError:
                start = 0
        body = self.media_bytes[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if start:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(self.media_bytes) - 1}/{len(self.media_bytes)}",
            )
        self.end_headers()
        self.wfile.write(body)

    def send_spa_shell(self):
        body = b'<!DOCTYPE html><html class="_9dls _ar44" lang="zh-cn"><body>SPA</body></html>'
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockInstagram:
    """A threaded HTTP server that behaves like the Instagram web app."""

    def __init__(
        self,
        rest: bool = True,
        collections: bool = True,
        collection_links: bool = True,
        flaky: int = 0,
    ):
        """``flaky`` truncates that many media transfers before behaving."""
        self.port = free_port()
        # one handler subclass per server: two mocks must never share flags
        handler = type("Handler", (_Handler,), {
            "origin": self.origin,
            "rest_available": rest,
            "collections_available": collections,
            "collection_links": collection_links,
        })
        socketserver.TCPServer.allow_reuse_address = True
        self.server = socketserver.TCPServer(("127.0.0.1", self.port), handler)
        self.server.flaky_remaining = flaky
        self.server.range_requests = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> "MockInstagram":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


class ReplayHelperTest(unittest.TestCase):
    """The page's GraphQL body must be replayed byte-for-byte."""

    def test_swap_form_field_replaces_only_variables(self):
        original = (
            "av=17841400143213132&__d=www&__user=0&__a=1"
            "&fb_api_req_friendly_name=PolarisSavedCollectionPageWWWQuery"
            "&fb_dtsg=NAfyPMYiW6vHQ9tPHbAQdMEH9-9_3yKA0BQvTlvKuC7KryL4lIgsGxA%3A17843683195144578%3A1791008573"
            "&jazoest=26149&lsd=bRcTLk8tT3R4CWhuv6e-wk"
            "&doc_id=29192959536956308&server_timestamps=true"
            "&variables=%7B%22after%22%3Anull%2C%22collection_id%22%3A%2218243648742304045%22%2C%22first%22%3A12%7D"
        )
        swapped = BROWSER_SESSION.swap_form_field(
            original, "variables", json.dumps({"after": "CURSOR1", "collection_id": "18243648742304045", "first": 12})
        )
        # every other parameter survives untouched, in order
        self.assertEqual(swapped.split("&")[:-1], original.split("&")[:-1])
        self.assertTrue(swapped.endswith(
            "variables=%7B%22after%22%3A%20%22CURSOR1%22%2C%20%22collection_id%22%3A%20%2218243648742304045%22%2C%20%22first%22%3A%2012%7D"
        ))
        # and Instagram still sees a valid variables blob
        parsed = urllib.parse.parse_qs(swapped)
        self.assertEqual(json.loads(parsed["variables"][0])["after"], "CURSOR1")

    def test_swap_form_field_appends_when_missing(self):
        self.assertIn("x=1", BROWSER_SESSION.swap_form_field("x=1", "variables", "{}"))
        self.assertEqual(
            urllib.parse.parse_qs(BROWSER_SESSION.swap_form_field("", "variables", '{"a": 1}'))["variables"],
            ['{"a": 1}'],
        )

    def test_strip_graphql_prefix(self):
        self.assertEqual(BROWSER_SESSION.strip_graphql_prefix('{"data": 1}'), '{"data": 1}')
        self.assertEqual(BROWSER_SESSION.strip_graphql_prefix('for (;;);{"data": 1}'), '{"data": 1}')
        self.assertEqual(BROWSER_SESSION.strip_graphql_prefix('  while(1);{"data": 1}'), '{"data": 1}')
        self.assertEqual(BROWSER_SESSION.strip_graphql_prefix(""), "")

    def test_find_page_info_locates_the_cursor(self):
        payload = {"data": {"fetch__MediaCollection": {"media": {
            "edges": [],
            "page_info": {"has_next_page": True, "end_cursor": "CURSOR9"},
        }}}}
        info = BROWSER_SESSION.find_page_info(payload)
        self.assertTrue(info["has_next_page"])
        self.assertEqual(info["end_cursor"], "CURSOR9")
        self.assertEqual(BROWSER_SESSION.find_page_info({"data": {}}), {})

    def test_extract_media_reads_the_fetch_media_collection_envelope(self):
        payload = {"data": {"fetch__MediaCollection": {"name": "fun", "media": {
            "edges": [
                {"node": _media_item("https://x/", "1", "DAbc123", "demo_user", 1751500000)},
                {"node": _media_item("https://x/", "2", "DDef456", "demo_user", 1751500001, kind="image")},
            ],
            "page_info": {"has_next_page": True, "end_cursor": "C1"},
        }}}}
        items = BROWSER_SESSION.extract_media([payload])
        self.assertEqual(sorted(item["code"] for item in items), ["DAbc123", "DDef456"])


class _NullCdp:
    """Just enough CDP session for a NetworkCapture to start and drain."""

    def call(self, _method, _params=None, timeout=None):
        return {}

    def drain_events(self):
        return []


class RateLimitTest(unittest.TestCase):
    def test_the_complaint_is_recognized_by_code_and_by_text(self):
        self.assertEqual(
            BROWSER_SESSION.rate_limit_message(
                {"errors": [{"code": 1675004, "message": "Rate limit exceeded"}]}
            ),
            "Rate limit exceeded",
        )
        self.assertEqual(
            BROWSER_SESSION.rate_limit_message(
                {"errors": [{"message": "rate limit exceeded, slow down"}]}
            ),
            "rate limit exceeded, slow down",
        )
        self.assertIsNone(BROWSER_SESSION.rate_limit_message({"data": {"ok": 1}}))
        self.assertIsNone(
            BROWSER_SESSION.rate_limit_message(
                {"errors": [{"code": 100, "message": "not a limit"}]}
            )
        )

    def test_collection_feed_raises_instead_of_returning_nothing(self):
        session = BROWSER_SESSION.InstagramSession.__new__(BROWSER_SESSION.InstagramSession)
        session.session = _NullCdp()
        with mock.patch.object(session, "navigate"), mock.patch.object(
            session,
            "_feed_request",
            return_value={
                "url": "https://www.instagram.com/api/graphql",
                "method": "POST",
                "headers": {},
                "post_data": "variables=%7B%7D",
            },
        ), mock.patch.object(
            session,
            "replay",
            return_value={"errors": [{"code": 1675004, "message": "Rate limit exceeded"}]},
        ):
            with self.assertRaises(BROWSER_SESSION.RateLimitedError):
                session.collection_feed(
                    "https://www.instagram.com/u/saved/_/1/",
                    collection_id="1",
                    settle=0.05,
                )

    def test_enumerate_items_does_not_swallow_a_rate_limit(self):
        """A spent quota must abort, not fall through to the capture route.

        The capture reads the global saved feed when the collection query is
        throttled, and the sync would file those few newest items into every
        collection it walks.
        """
        session = mock.Mock(spec=BROWSER_SESSION.InstagramSession)
        session.collection_items.side_effect = BROWSER_SESSION.SessionError("no rest route")
        session.collection_feed.side_effect = BROWSER_SESSION.RateLimitedError(
            "Instagram is rate-limiting the saved-collection query"
        )
        args = argparse.Namespace(
            capture=True, no_replay=False, max_items=10, delay_ms=1,
            username="u", origin="https://www.instagram.com/",
            dom_fallback=False, dom_rounds=1, dom_delay_ms=1,
        )
        with self.assertRaises(BROWSER_SESSION.RateLimitedError):
            BROWSER_SYNC.enumerate_items(session, {"id": "1", "url": "https://x/"}, args)


class _RetiringSession:
    """An InstagramSession whose REST route 404s, counting the probes."""

    def __init__(self):
        self.probes = 0

    def collections(self):
        self.probes += 1
        raise BROWSER_SESSION.SessionError(
            "page JavaScript failed: HTTP 404 for /api/v1/collections/list/: HTML shell"
        )

    def collections_via_capture(self, username, rounds=2, delay_ms=10):
        return [{"id": "1234567890123456", "name": "自然", "url": "u"}]


class RouteHintTest(unittest.TestCase):
    """A retired endpoint must not be probed (and logged) on every run."""

    def _args(self):
        return argparse.Namespace(
            capture=True, scroll_rounds=2, scroll_delay_ms=10, username="demo_user"
        )

    def test_a_404_is_remembered_and_the_probe_is_skipped_next_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = _RetiringSession()
            first, source = BROWSER_SYNC.list_collections(session, self._args(), root)
            self.assertEqual(source, "browser-network")
            self.assertEqual(len(first), 1)
            self.assertEqual(session.probes, 1)
            self.assertTrue((root / ".route-hints.json").is_file())

            # the second run goes straight to the page: no probe, no note
            err = StringIO()
            with redirect_stderr(err):
                second, source = BROWSER_SYNC.list_collections(session, self._args(), root)
            self.assertEqual(source, "browser-network")
            self.assertEqual(session.probes, 1)
            self.assertNotIn("note:", err.getvalue())

    def test_a_stale_hint_is_probed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".route-hints.json").write_text(
                json.dumps({"collections_list_retired": "2020-01-01T00:00:00"}),
                encoding="utf-8",
            )
            session = _RetiringSession()
            with redirect_stderr(StringIO()):
                BROWSER_SYNC.list_collections(session, self._args(), root)
            self.assertEqual(session.probes, 1)

    def test_a_skipped_probe_is_still_explained_when_capture_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = _RetiringSession()

            class _Broken(_RetiringSession):
                def collections_via_capture(self, username, rounds=2, delay_ms=10):
                    raise BROWSER_SESSION.SessionError("nothing loaded")

            BROWSER_SYNC.remember_route(root, "collections_list_retired")
            broken = _Broken()
            with redirect_stderr(StringIO()), self.assertRaises(
                BROWSER_SYNC.SessionError
            ) as raised:
                BROWSER_SYNC.list_collections(broken, self._args(), root)
            self.assertIn("retired (remembered)", str(raised.exception))
            self.assertEqual(broken.probes, 0)

    def test_a_missing_root_is_not_created_just_to_park_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "not-there-yet"
            BROWSER_SYNC.remember_route(absent, "collections_list_retired")
            self.assertFalse(absent.exists())

    def test_a_working_rest_route_wins_without_writing_hints(self):
        class _Working(_RetiringSession):
            def collections(self):
                return [{"id": "1", "name": "自然", "url": "u"}]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collections, source = BROWSER_SYNC.list_collections(_Working(), self._args(), root)
            self.assertEqual(source, "api")
            self.assertFalse((root / ".route-hints.json").exists())


class _FakeSession:
    """A CdpSession stand-in that replays scripted events and body failures."""

    def __init__(self, events, body_results):
        self._events = list(events)
        self._bodies = list(body_results)
        self.calls = 0

    def drain_events(self):
        drained, self._events = self._events, []
        return drained

    def call(self, method, params=None, timeout=None):
        self.calls += 1
        value = self._bodies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class NetworkCaptureTest(unittest.TestCase):
    def _response_event(self, request_id, mime="application/json"):
        return {
            "method": "Network.responseReceived",
            "params": {
                "requestId": request_id,
                "type": "XHR",
                "response": {
                    "url": "https://www.instagram.com/api/graphql",
                    "mimeType": mime,
                },
            },
        }

    def test_text_javascript_responses_are_captured(self):
        """Instagram serves its JSON as text/javascript; requiring a json mime
        silently dropped the collection feed itself."""
        events = [
            self._response_event("r1", mime="text/javascript"),
            {"method": "Network.loadingFinished", "params": {"requestId": "r1"}},
        ]
        session = _FakeSession(events, [{"body": '{"data": {"ok": 1}}'}])
        capture = BROWSER_SESSION.NetworkCapture(session)
        capture.drain()
        self.assertEqual(capture.payloads, [{"data": {"ok": 1}}])

    def test_bodies_are_retried_until_the_response_is_complete(self):
        error = BROWSER_SESSION.cdp.CdpError("No resource with given identifier found")
        events = [self._response_event("r1")]
        session = _FakeSession(events, [error])
        capture = BROWSER_SESSION.NetworkCapture(session)
        capture.drain()
        # the body was not available yet: the request must stay pending
        self.assertEqual(capture.payloads, [])
        session._events = [{"method": "Network.loadingFinished", "params": {"requestId": "r1"}}]
        session._bodies = [error, {"body": '{"data": {"page": 1}}'}]
        capture.drain()
        capture.drain()
        self.assertEqual(capture.payloads, [{"data": {"page": 1}}])

    def test_requests_are_recorded_with_their_exact_bodies(self):
        events = [{
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "r1",
                "type": "Fetch",
                "request": {
                    "url": "https://www.instagram.com/api/graphql",
                    "method": "POST",
                    "headers": {"content-type": "application/x-www-form-urlencoded", "x-ig-app-id": "936619743392459"},
                    "postData": "doc_id=29192959536956308&variables=%7B%7D",
                },
            },
        }]
        capture = BROWSER_SESSION.NetworkCapture(_FakeSession(events, []))
        capture.drain()
        self.assertEqual(len(capture.requests), 1)
        request = capture.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["post_data"], "doc_id=29192959536956308&variables=%7B%7D")
        self.assertEqual(request["headers"]["x-ig-app-id"], "936619743392459")


class ShortcodeTest(unittest.TestCase):
    def test_roundtrip_matches_known_shortcode(self):
        code = "DG5x2I2N0vU"
        self.assertEqual(BROWSER_SESSION.pk_to_shortcode(BROWSER_SESSION.shortcode_to_pk(code)), code)

    def test_shortcode_pk_roundtrip_for_arbitrary_codes(self):
        for code in ("DAbc123", "DGhi789", "DCh1", "DCh2"):
            pk = BROWSER_SESSION.shortcode_to_pk(code)
            self.assertGreater(pk, 0)
            self.assertEqual(BROWSER_SESSION.pk_to_shortcode(pk), code)

    def test_invalid_shortcode_character(self):
        with self.assertRaises(ValueError):
            BROWSER_SESSION.shortcode_to_pk("bad!")

    def test_empty_pk(self):
        self.assertEqual(BROWSER_SESSION.pk_to_shortcode(0), "")


class MediaSelectionTest(unittest.TestCase):
    def item(self, **overrides):
        base = {
            "pk": "3546162228685415517",
            "code": "DAbc123",
            "username": "demo_user",
            "video_url": "https://cdn/high.mp4",
            "image_url": "https://cdn/cover.jpg",
            "children": [],
        }
        base.update(overrides)
        return base

    def test_video_wins_over_its_cover_image(self):
        media = BROWSER_SESSION.item_media(self.item())
        self.assertEqual([part["kind"] for part in media], ["video"])

    def test_image_only_post(self):
        media = BROWSER_SESSION.item_media(self.item(video_url=""))
        self.assertEqual([part["kind"] for part in media], ["image"])

    def test_carousel_keeps_order_and_indexes(self):
        item = self.item(children=[
            self.item(pk="1", code="A", video_url="https://cdn/a.mp4"),
            self.item(pk="2", code="B", video_url="", image_url="https://cdn/b.jpg"),
        ])
        media = BROWSER_SESSION.item_media(item)
        self.assertEqual([(part["kind"], part["index"]) for part in media], [("video", 1), ("image", 2)])

    def test_post_without_media_is_empty(self):
        self.assertEqual(BROWSER_SESSION.item_media(self.item(video_url="", image_url="")), [])

    def test_a_reel_described_by_its_cover_only_needs_enrichment(self):
        self.assertTrue(BROWSER_SESSION.video_part_missing(self.item(media_type=2, video_url="")))

    def test_a_reel_with_a_direct_url_is_left_alone(self):
        self.assertFalse(BROWSER_SESSION.video_part_missing(self.item(media_type=2)))

    def test_a_photo_never_needs_enrichment(self):
        self.assertFalse(BROWSER_SESSION.video_part_missing(self.item(media_type=1, video_url="")))


    def test_a_carousel_video_child_without_a_url_needs_enrichment(self):
        item = self.item(
            media_type=8,
            children=[
                self.item(pk="1", code="A", media_type=1, video_url=""),
                self.item(pk="2", code="B", media_type=2, video_url=""),
            ],
        )
        self.assertTrue(BROWSER_SESSION.video_part_missing(item))

    def test_project_items_drops_empty_and_duplicates(self):
        raw = [
            self.item(),
            self.item(),
            self.item(code="", pk="", video_url="", image_url=""),
            {"code": "DNoMedia", "pk": "9", "video_url": "", "image_url": ""},
        ]
        self.assertEqual([item["code"] for item in BROWSER_SESSION.project_items(raw)], ["DAbc123"])


class VideoVersionSelectionTest(unittest.TestCase):
    """A video_version list must be narrowed to what QuickTime can decode."""

    @staticmethod
    def version(url: str, width: int, codec: str | None = None) -> dict:
        entry = {"url": url, "width": width}
        if codec is not None:
            entry["type"] = f'video/mp4; codecs="{codec}"'
        return entry

    def test_avc_wins_even_when_vp9_is_wider(self):
        entry = BROWSER_SESSION._pick_playable([
            self.version("https://cdn/vp9.mp4", 1080, "vp09.00.31.08"),
            self.version("https://cdn/avc.mp4", 720, "avc1.64001F"),
        ])
        self.assertEqual((entry or {}).get("url"), "https://cdn/avc.mp4")

    def test_widest_avc_wins_among_avc(self):
        entry = BROWSER_SESSION._pick_playable([
            self.version("https://cdn/small.mp4", 480, "avc1.42E01E"),
            self.version("https://cdn/big.mp4", 1080, "avc1.64001F"),
        ])
        self.assertEqual((entry or {}).get("url"), "https://cdn/big.mp4")

    def test_unmarked_entries_rank_between_avc_and_vp9(self):
        self.assertEqual(
            (BROWSER_SESSION._pick_playable([
                self.version("https://cdn/vp9.mp4", 1080, "vp09.00.31.08"),
                self.version("https://cdn/plain.mp4", 720),
            ]) or {}).get("url"),
            "https://cdn/plain.mp4",
        )
        self.assertEqual(
            (BROWSER_SESSION._pick_playable([
                self.version("https://cdn/avc.mp4", 480, "avc1.64001F"),
                self.version("https://cdn/plain.mp4", 1080),
            ]) or {}).get("url"),
            "https://cdn/avc.mp4",
        )

    def test_vp9_as_the_only_option_still_downloads(self):
        entry = BROWSER_SESSION._pick_playable([
            self.version("https://cdn/only.mp4", 1080, "vp09.00.31.08"),
        ])
        self.assertEqual((entry or {}).get("url"), "https://cdn/only.mp4")

    def test_hevc_counts_as_playable(self):
        entry = BROWSER_SESSION._pick_playable([
            self.version("https://cdn/vp9.mp4", 1080, "vp09.00.31.08"),
            self.version("https://cdn/hevc.mp4", 720, "hvc1.2.4.L153"),
        ])
        self.assertEqual((entry or {}).get("url"), "https://cdn/hevc.mp4")


class FileNamingTest(unittest.TestCase):
    def item(self, **overrides):
        base = {"pk": "1", "code": "DAbc123", "username": "goo/kergu:家"}
        base.update(overrides)
        return base

    def test_single_file_has_no_index(self):
        name = BROWSER_SESSION.item_filename(
            self.item(), {"kind": "video", "url": "https://cdn/x.mp4", "index": 1}, False
        )
        self.assertEqual(name, "DAbc123_goo_kergu_家.mp4")

    def test_multiple_files_are_indexed(self):
        name = BROWSER_SESSION.item_filename(
            self.item(), {"kind": "image", "url": "https://cdn/x.jpg", "index": 2}, True
        )
        self.assertTrue(name.endswith("_2.jpg"))

    def test_unknown_extension_defaults_by_kind(self):
        self.assertEqual(
            BROWSER_SESSION.media_extension("https://cdn/video?v=1", "video"), ".mp4"
        )
        self.assertEqual(
            BROWSER_SESSION.media_extension("https://cdn/photo.webp", "image"), ".webp"
        )

    def test_missing_shortcode_falls_back_to_pk(self):
        item = {"pk": "3546162228685415517", "username": "x"}
        self.assertEqual(BROWSER_SESSION.item_shortcode(item), "DE2f8I0s6Rd")
        name = BROWSER_SESSION.item_filename(
            item, {"kind": "video", "url": "https://cdn/x.mp4", "index": 1}, False
        )
        self.assertEqual(name, "DE2f8I0s6Rd_x.mp4")


class PythonProjectionTest(unittest.TestCase):
    """The capture path needs the page's projection in Python; keep them equal."""

    def test_video_and_cover_image(self):
        raw = _Handler.page_one()["items"][0]
        item = BROWSER_SESSION.project_media(raw)
        self.assertEqual(item["code"], "DAbc123")
        self.assertEqual(item["username"], "demo_user")
        self.assertTrue(item["video_url"].endswith("DAbc123_high.mp4"))
        self.assertTrue(item["image_url"].endswith("DAbc123.jpg"))
        self.assertEqual(item["caption"], "caption for DAbc123")

    def test_image_only_post_has_no_video(self):
        raw = _Handler.page_one()["items"][1]
        item = BROWSER_SESSION.project_media(raw)
        self.assertEqual(item["video_url"], "")
        self.assertTrue(item["image_url"].endswith("DDef456.jpg"))

    def test_carousel_children_are_projected(self):
        raw = _Handler.page_two()["items"][0]
        item = BROWSER_SESSION.project_media(raw)
        self.assertEqual([child["code"] for child in item["children"]], ["DCh1", "DCh2"])
        self.assertEqual(item["children"][0]["media_type"], 2)

    def test_non_media_nodes_are_rejected(self):
        self.assertIsNone(BROWSER_SESSION.project_media({"pk": "1", "code": "DAbc123"}))
        self.assertIsNone(BROWSER_SESSION.project_media({"collection_id": "1"}))

    def test_extract_media_walks_graphql_payloads(self):
        payload = {"data": {"items": _Handler.page_one()["items"] + _Handler.page_two()["items"]}}
        items = BROWSER_SESSION.extract_media([payload])
        self.assertEqual([item["code"] for item in items], ["DAbc123", "DDef456", "DGhi789"])
        # the same media appearing on two pages must not be emitted twice
        self.assertEqual(len(BROWSER_SESSION.extract_media([payload, payload])), 3)

    def test_extract_collections_accepts_rest_and_graphql_shapes(self):
        rest = {"items": [{
            "collection_id": "1234567890123456",
            "collection_name": "自然",
            "collection_type": "REGULAR",
            "media_count": 3,
        }]}
        graphql = {"data": {"collections": [{
            "id": "collection:1234567890123456",
            "name": "自然",
            "collection_type": "REGULAR",
            "media_count": 3,
        }]}}
        for payload in (rest, graphql):
            collections = BROWSER_SESSION.extract_collections([payload])
            self.assertEqual(collections, [{
                "id": "1234567890123456",
                "name": "自然",
                "type": "REGULAR",
                "count": 3,
                "source": "browser-network",
            }])

    def test_extract_collections_ignores_unmarked_names(self):
        noise = {"data": {"user": {"id": "42", "name": "demo_user"}}}
        self.assertEqual(BROWSER_SESSION.extract_collections([noise]), [])


class ManifestTest(unittest.TestCase):
    def test_manifest_matches_the_yt_dlp_shape(self):
        item = {
            "pk": "3546162228685415517",
            "code": "DAbc123",
            "username": "demo_user",
            "taken_at": 1751500000,
            "duration": 12.5,
            "caption": "hello",
            "video_url": "https://cdn/high.mp4",
            "image_url": "https://cdn/cover.jpg",
            "children": [],
        }
        directory = Path("/tmp/ins2anki-manifest")
        manifest = BROWSER_SESSION.build_manifest(
            item, [directory / "DAbc123_demo_user.mp4"], directory
        )
        self.assertEqual(manifest["platform"], "instagram")
        self.assertEqual(manifest["kind"], "video")
        self.assertEqual(manifest["source_url"], "https://www.instagram.com/p/DAbc123/")
        metadata = manifest["metadata"][0]
        self.assertEqual(metadata["uploader"], "demo_user")
        self.assertEqual(metadata["description"], "hello")
        self.assertEqual(metadata["webpage_url"], manifest["source_url"])
        self.assertIn("session", manifest)

    def test_inventory_matches_the_browser_exporter_shape(self):
        inventory = BROWSER_SESSION.build_inventory(
            "自然", [{"code": "DAbc123", "children": []}], url="https://x/saved"
        )
        self.assertEqual(inventory["version"], 1)
        collection = inventory["collections"][0]
        self.assertEqual(collection["name"], "自然")
        self.assertEqual(collection["posts"], ["https://www.instagram.com/p/DAbc123/"])
        # the shared loader must accept it unchanged
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inv.json"
            path.write_text(json.dumps(inventory), encoding="utf-8")
            loaded = SYNC_COMMON.load_inventory(path)
        self.assertEqual(loaded[0]["platform"], "instagram")


class StateDiscoveryTest(unittest.TestCase):
    """An existing output tree is a local inventory: no API needed."""

    def make_root(self, tmp: str) -> Path:
        root = Path(tmp) / "instagram-saved"
        for name, source, status in (
            ("自然", "https://www.instagram.com/demo_user/saved/_/1234567890123456/", "completed"),
            ("fun", "https://www.instagram.com/demo_user/saved/fun/18243648742304045/", None),
            ("全部收藏", "https://www.instagram.com/demo_user/saved/", "completed"),
        ):
            folder = root / name
            folder.mkdir(parents=True)
            (folder / "sync-state.json").write_text(
                json.dumps({"source": source, "items": {"DAbc123": {"status": status}}}),
                encoding="utf-8",
            )
        (root / "坏文件").mkdir()
        (root / "坏文件" / "sync-state.json").write_text("{not json", encoding="utf-8")
        return root

    def test_recovers_both_url_forms_and_skips_the_all_saved_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = BROWSER_SYNC.collections_from_state(self.make_root(tmp))
        self.assertEqual(
            [(item["name"], item["id"]) for item in found],
            [("fun", "18243648742304045"), ("自然", "1234567890123456")],
        )
        self.assertEqual(found[1]["url"], "https://www.instagram.com/demo_user/saved/_/1234567890123456/")
        for item in found:
            self.assertNotEqual(item["name"], "全部收藏")

    def test_missing_root_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(BROWSER_SYNC.collections_from_state(Path(tmp) / "nope"), [])


class CollectionMatchingTest(unittest.TestCase):
    collections = [
        {"id": "1", "name": "自然"},
        {"id": "2", "name": "英语"},
        {"id": "3", "name": "英语进阶"},
    ]

    def test_by_id(self):
        self.assertEqual(BROWSER_SESSION.match_collection(self.collections, "2")["name"], "英语")

    def test_by_exact_name(self):
        self.assertEqual(BROWSER_SESSION.match_collection(self.collections, "英语")["id"], "2")

    def test_by_unique_substring(self):
        self.assertEqual(BROWSER_SESSION.match_collection(self.collections, "自然")["id"], "1")

    def test_ambiguous_substring_lists_candidates(self):
        with self.assertRaises(BROWSER_SESSION.SessionError) as ctx:
            BROWSER_SESSION.match_collection(self.collections, "英")
        self.assertIn("英语 (2)", str(ctx.exception))

    def test_unknown_name(self):
        with self.assertRaises(BROWSER_SESSION.SessionError):
            BROWSER_SESSION.match_collection(self.collections, "数学")

    def test_collection_url_forms(self):
        self.assertEqual(
            BROWSER_SESSION.collection_url("1234567890123456", username="demo_user"),
            "https://www.instagram.com/demo_user/saved/_/1234567890123456/",
        )
        self.assertEqual(
            BROWSER_SESSION.collection_url(None, username="demo_user"),
            "https://www.instagram.com/demo_user/saved/",
        )


class DownloadTest(unittest.TestCase):
    def test_download_writes_and_renames(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            size = BROWSER_SESSION.download_url(
                f"{mock_api.origin}media/DAbc123_high.mp4", target
            )
            self.assertEqual(size, len(_Handler.media_bytes))
            self.assertEqual(target.read_bytes(), _Handler.media_bytes)
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_download_failure_leaves_no_partial_file(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "missing.mp4"
            with self.assertRaises(BROWSER_SESSION.SessionError):
                BROWSER_SESSION.download_url(f"{mock_api.origin}media/nope.mp4", target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())


class DownloadResilienceTest(unittest.TestCase):
    """The CDN drops connections; a drop must not fail a post."""

    def test_truncated_transfer_is_retried_and_resumed(self):
        with MockInstagram(flaky=1) as mock_api, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            size = BROWSER_SESSION.download_url(
                f"{mock_api.origin}media/flaky.mp4", target, attempts=3, retry_base=0.05
            )
            self.assertEqual(size, len(_Handler.media_bytes))
            self.assertEqual(target.read_bytes(), _Handler.media_bytes)
            self.assertGreaterEqual(mock_api.server.range_requests, 1)
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_stale_oversized_partial_is_discarded_not_appended(self):
        """A .part from another tool must never be glued onto a fresh body."""
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            partial = target.with_suffix(".mp4.part")
            partial.write_bytes(b"\x00" * (len(_Handler.media_bytes) + 1000))
            size = BROWSER_SESSION.download_url(
                f"{mock_api.origin}media/DAbc123_high.mp4", target, retry_base=0.01
            )
            self.assertEqual(size, len(_Handler.media_bytes))
            self.assertEqual(target.read_bytes(), _Handler.media_bytes)
            self.assertFalse(partial.exists())

    def test_persistent_truncation_eventually_fails_cleanly(self):
        with MockInstagram(flaky=99) as mock_api, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            with self.assertRaises(BROWSER_SESSION.SessionError) as ctx:
                BROWSER_SESSION.download_url(
                    f"{mock_api.origin}media/flaky.mp4", target, attempts=2, retry_base=0.01
                )
            self.assertIn("media download failed", str(ctx.exception))
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".mp4.part").exists())

    def test_failed_item_reports_its_reason_in_the_summary(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            item = _media_item(mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000)
            item["video_url"] = f"{mock_api.origin}media/nope.mp4"
            url = BROWSER_SESSION.item_page_url(item)
            download_fn = BROWSER_SYNC.make_session_downloader({url: item})
            output_dir = Path(tmp) / "自然"
            buffer = StringIO()
            code = SYNC_COMMON.sync_items(
                [("instagram", "DAbc123", url)],
                output_dir=output_dir,
                state_file=output_dir / "sync-state.json",
                downloader=BROWSER_SYNC.DOWNLOADER,
                download_fn=download_fn,
                source_label="test",
                stream=buffer,
            )
            self.assertEqual(code, 2)
            summary = json.loads(buffer.getvalue())
            self.assertEqual(summary["failed"], 1)
            self.assertEqual([failure["id"] for failure in summary["failures"]], ["DAbc123"])
            self.assertIn("HTTP 404", summary["failures"][0]["error"])
            # the reason survives in the state file too
            state = json.loads((output_dir / "sync-state.json").read_text(encoding="utf-8"))
            self.assertIn("HTTP 404", state["items"]["DAbc123"]["error"])

    def test_existing_file_is_not_downloaded_again(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            item = _media_item(mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000)
            url = BROWSER_SESSION.item_page_url(item)
            download_fn = BROWSER_SYNC.make_session_downloader({url: item})
            item_dir = Path(tmp) / "DAbc123"
            item_dir.mkdir(parents=True)
            existing = item_dir / "DAbc123_demo_user.mp4"
            existing.write_bytes(b"already here")
            ok, detail = download_fn(BROWSER_SYNC.DOWNLOADER, url, item_dir, None, None)
            self.assertTrue(ok, detail)
            self.assertEqual(existing.read_bytes(), b"already here")


class SessionDownloaderTest(unittest.TestCase):
    """The session downloader must satisfy the shared state machine."""

    def test_sync_items_downloads_and_skips(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            item = _media_item(mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000)
            item["video_url"] = f"{mock_api.origin}media/DAbc123_high.mp4"
            item["image_url"] = ""
            url = BROWSER_SESSION.item_page_url(item)
            download_fn = BROWSER_SYNC.make_session_downloader({url: item})
            output_dir = Path(tmp) / "自然"
            state = output_dir / "sync-state.json"
            first = SYNC_COMMON.sync_items(
                [("instagram", BROWSER_SESSION.item_shortcode(item), url)],
                output_dir=output_dir,
                state_file=state,
                downloader=BROWSER_SYNC.DOWNLOADER,
                download_fn=download_fn,
                source_label="test",
                stream=StringIO(),
            )
            self.assertEqual(first, 0)
            item_dir = output_dir / "DAbc123"
            self.assertTrue(SYNC_COMMON.valid_download(item_dir))
            manifest = json.loads((item_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["metadata"][0]["shortcode"], "DAbc123")
            self.assertTrue((item_dir / "DAbc123_demo_user.info.json").is_file())
            self.assertTrue((item_dir / "DAbc123_demo_user.description").is_file())

            # second pass: nothing to do
            second = SYNC_COMMON.sync_items(
                [("instagram", BROWSER_SESSION.item_shortcode(item), url)],
                output_dir=output_dir,
                state_file=state,
                downloader=BROWSER_SYNC.DOWNLOADER,
                download_fn=download_fn,
                source_label="test",
                dry_run=True,
                stream=StringIO(),
            )
            self.assertEqual(second, 0)
            self.assertEqual(json.loads((state).read_text())["items"]["DAbc123"]["status"], "completed")

    def test_parallel_jobs_download_everything(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            items = []
            for index in range(4):
                item = _media_item(mock_api.origin, str(1000 + index), f"DCode{index}", "demo_user", 1751500000 + index)
                item["video_url"] = f"{mock_api.origin}media/DCode{index}_high.mp4"
                items.append(item)
            media_by_url = {BROWSER_SESSION.item_page_url(item): item for item in items}
            stats: dict[str, int] = {}
            download_fn = BROWSER_SYNC.make_session_downloader(media_by_url, stats=stats)
            discovered = [
                ("instagram", BROWSER_SESSION.item_shortcode(item), BROWSER_SESSION.item_page_url(item))
                for item in items
            ]
            output_dir = Path(tmp) / "并发"
            code = SYNC_COMMON.sync_items(
                discovered,
                output_dir=output_dir,
                state_file=output_dir / "sync-state.json",
                downloader=BROWSER_SYNC.DOWNLOADER,
                download_fn=download_fn,
                source_label="test",
                stream=StringIO(),
                jobs=4,
            )
            self.assertEqual(code, 0)
            self.assertEqual(stats, {"via_session": 4})
            state = json.loads((output_dir / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(item["status"] for item in state["items"].values()),
                ["completed"] * 4,
            )
            self.assertEqual(state["jobs"], 4)
            self.assertIn("elapsed_seconds", state)
            for index in range(4):
                self.assertTrue(
                    (output_dir / f"DCode{index}" / f"DCode{index}_demo_user.mp4").is_file()
                )

    def test_no_yt_dlp_fallback_fails_instead_of_being_slow(self):
        download_fn = BROWSER_SYNC.make_session_downloader(
            {}, allow_ytdlp_fallback=False
        )
        with mock.patch.object(SYNC_COMMON, "run_download") as patched:
            ok, detail = download_fn(
                BROWSER_SYNC.DOWNLOADER, "https://www.instagram.com/p/DXy/", Path("/tmp/x"), None, None
            )
        self.assertFalse(ok)
        self.assertIn("fallback disabled", detail)
        patched.assert_not_called()

    def test_a_reel_without_a_direct_url_is_enriched_before_download(self):
        # the saved/collection listings describe a reel by its cover image only
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            cover_only = _media_item(
                mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000
            )
            cover_only["video_url"] = ""
            full = _media_item(
                mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000
            )
            url = BROWSER_SESSION.item_page_url(cover_only)
            media_by_url = {url: cover_only}
            asked: list[str] = []

            class FakeSession:
                def media_info(self, pk, **_kwargs):
                    asked.append(pk)
                    return full

            stats: dict[str, int] = {}
            download_fn = BROWSER_SYNC.make_session_downloader(
                media_by_url, stats=stats, session=FakeSession()
            )
            ok, detail = download_fn(BROWSER_SYNC.DOWNLOADER, url, Path(tmp) / "out", None, None)
            self.assertTrue(ok)
            self.assertEqual(asked, ["3546162228685415517"])
            self.assertEqual(stats, {"via_session": 1})
            self.assertTrue((Path(tmp) / "out" / "DAbc123_demo_user.mp4").is_file())
            # the cache carries the full payload, so a retry streams the video
            self.assertEqual(media_by_url[url], full)

    def test_a_failed_enrichment_falls_back_to_yt_dlp(self):
        cover_only = _media_item("https://cdn.test/", "1", "DAbc123", "demo_user", 0)
        cover_only["video_url"] = ""
        url = BROWSER_SESSION.item_page_url(cover_only)

        class BrokenSession:
            def media_info(self, pk, **_kwargs):
                raise BROWSER_SESSION.SessionError("Instagram refused media 1")

        download_fn = BROWSER_SYNC.make_session_downloader(
            {url: cover_only}, session=BrokenSession()
        )
        with mock.patch.object(
            SYNC_COMMON, "run_download", return_value=(True, "yt-dlp")
        ) as patched:
            ok, _detail = download_fn(BROWSER_SYNC.DOWNLOADER, url, Path("/tmp/x"), None, None)
        self.assertTrue(ok)
        patched.assert_called_once()

    def test_without_a_session_a_cover_only_reel_still_saves_its_cover(self):
        with MockInstagram() as mock_api, tempfile.TemporaryDirectory() as tmp:
            cover_only = _media_item(mock_api.origin, "1", "DAbc123", "demo_user", 0)
            cover_only["video_url"] = ""
            url = BROWSER_SESSION.item_page_url(cover_only)
            download_fn = BROWSER_SYNC.make_session_downloader({url: cover_only})
            ok, _detail = download_fn(
                BROWSER_SYNC.DOWNLOADER, url, Path(tmp) / "out", None, None
            )
            self.assertTrue(ok)
            self.assertTrue((Path(tmp) / "out" / "DAbc123_demo_user.jpg").is_file())

    def test_prefer_yt_dlp_delegates_to_the_old_path(self):
        item = _media_item("https://cdn.test/", "1", "DAbc123", "demo_user", 0)
        url = BROWSER_SESSION.item_page_url(item)
        download_fn = BROWSER_SYNC.make_session_downloader({url: item}, prefer_ytdlp=True)
        with mock.patch.object(
            SYNC_COMMON, "run_download", return_value=(True, "yt-dlp")
        ) as patched:
            ok, detail = download_fn(BROWSER_SYNC.DOWNLOADER, url, Path("/tmp/x"), None, None)
        self.assertTrue(ok)
        self.assertEqual(detail, "yt-dlp")
        patched.assert_called_once()

    def test_unknown_url_delegates_to_the_old_path(self):
        download_fn = BROWSER_SYNC.make_session_downloader({})
        with mock.patch.object(
            SYNC_COMMON, "run_download", return_value=(True, "yt-dlp")
        ) as patched:
            ok, _detail = download_fn(
                BROWSER_SYNC.DOWNLOADER, "https://www.instagram.com/p/DXy/", Path("/tmp/x"), None, None
            )
        self.assertTrue(ok)
        patched.assert_called_once()


class CoverRepairTest(unittest.TestCase):
    """Reels saved as their cover must be findable from the tree alone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "instagram-saved"
        self.addCleanup(self._tmp.cleanup)

    def write_item(
        self, collection: str, shortcode: str, media_type: int, files: tuple[str, ...]
    ) -> Path:
        directory = self.root / collection / shortcode
        directory.mkdir(parents=True)
        for name in files:
            (directory / name).write_bytes(b"data")
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "platform": "instagram",
                    "kind": "images" if media_type == 1 else "video",
                    "media": [str(directory / name) for name in files],
                    "metadata": [{"id": shortcode, "media_type": media_type}],
                }
            ),
            encoding="utf-8",
        )
        return directory

    def test_the_tree_is_the_source_of_truth_not_the_state(self):
        cover = self.write_item("擦边", "DAbc123", 2, ("DAbc123_user.jpg",))
        self.write_item("擦边", "DVideo01", 2, ("DVideo01_user.mp4",))
        self.write_item("自然", "DPhoto01", 1, ("DPhoto01_user.jpg",))
        # an interrupted run parks its originals next to the fresh download
        self.write_item(
            "擦边", "DAbc123.unplayable", 2, ("DAbc123_user.jpg",)
        )
        # a state file with no entries at all: two concurrent runs, or one
        # that was killed before any item was recorded
        (self.root / "擦边").mkdir(parents=True, exist_ok=True)
        (self.root / "擦边" / "sync-state.json").write_text(
            json.dumps({"version": 1, "items": {}}), encoding="utf-8"
        )

        findings = BROWSER_SYNC.find_cover_only(self.root)
        self.assertEqual([finding["id"] for finding in findings], ["DAbc123"])
        self.assertEqual(findings[0]["directory"], str(cover))
        self.assertEqual(
            findings[0]["state_file"], str(self.root / "擦边" / "sync-state.json")
        )

    def test_forget_parks_the_cover_and_drops_the_state_entry(self):
        self.write_item("擦边", "DAbc123", 2, ("DAbc123_user.jpg",))
        state_file = self.root / "擦边" / "sync-state.json"
        state_file.write_text(
            json.dumps(
                {"version": 1, "items": {"DAbc123": {"url": "https://x/", "status": "completed"}}}
            ),
            encoding="utf-8",
        )

        with redirect_stdout(StringIO()) as out:
            code = BROWSER_SYNC.command_repair(
                argparse.Namespace(
                    output_root=self.root, covers=True, forget=True
                )
            )
        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["forgotten"], 1)
        self.assertFalse((self.root / "擦边" / "DAbc123").exists())
        self.assertTrue(
            (self.root / "擦边" / "DAbc123.unplayable" / "DAbc123_user.jpg").is_file()
        )
        # the entry is gone, so the next sync downloads it again
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["items"], {})


class CliTest(unittest.TestCase):
    def test_subcommands_exist(self):
        parser = BROWSER_SYNC.build_parser()
        args = parser.parse_args([
            "sync", "--collection", "自然", "--output-dir", "/tmp/x", "--prefer-yt-dlp"
        ])
        self.assertIs(args.func, BROWSER_SYNC.command_sync)
        self.assertTrue(args.prefer_yt_dlp)
        self.assertTrue(args.include_photos)

    def test_no_photos_flag(self):
        parser = BROWSER_SYNC.build_parser()
        args = parser.parse_args([
            "sync", "--collection", "1", "--output-dir", "/tmp/x", "--no-include-photos"
        ])
        self.assertFalse(args.include_photos)

    def test_jobs_defaults_to_parallel(self):
        parser = BROWSER_SYNC.build_parser()
        args = parser.parse_args(["sync", "--collection", "1", "--output-dir", "/tmp/x"])
        self.assertEqual(args.jobs, 4)
        self.assertFalse(args.no_yt_dlp_fallback)

    def test_all_collections_flags(self):
        parser = BROWSER_SYNC.build_parser()
        args = parser.parse_args([
            "sync", "--all-collections", "--output-root", "/tmp/root"
        ])
        self.assertTrue(args.all_collections)
        self.assertIsNone(args.collection)
        self.assertEqual(str(args.output_root), "/tmp/root")

    def test_launch_flag_defaults_off(self):
        parser = BROWSER_SYNC.build_parser()
        args = parser.parse_args(["collections"])
        self.assertFalse(args.launch)
        self.assertEqual(args.endpoint, BROWSER_SESSION.DEFAULT_ENDPOINT)


# --------------------------------------------------------------------------
# Real browser integration
# --------------------------------------------------------------------------


def browser_executable() -> str | None:
    candidate = os.environ.get("INS2ANKI_TEST_BROWSER")
    try:
        return BROWSER_SESSION.find_browser(candidate)
    except BROWSER_SESSION.SessionError:
        return None


@unittest.skipIf(os.environ.get("INS2ANKI_SKIP_BROWSER_TESTS") == "1", "browser tests disabled")
class BrowserIntegrationTest(unittest.TestCase):
    """Drive headless Chromium against the mock API, end to end."""

    @classmethod
    def setUpClass(cls):
        executable = browser_executable()
        if not executable:
            raise unittest.SkipTest("no Chromium-based browser found")
        cls.mock = MockInstagram()
        cls.mock.__enter__()
        cls.profile = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.endpoint = BROWSER_SESSION.endpoint_url(cls.port)
        extra = ["--headless=new", "--disable-gpu", "--disable-breakpad", "--no-first-run"]
        extra += [part for part in os.environ.get("INS2ANKI_CHROME_ARGS", "").split() if part]
        cls.process, _ = BROWSER_SESSION.launch_browser(
            browser=executable,
            profile_dir=Path(cls.profile.name) / "profile",
            port=cls.port,
            url=cls.mock.origin,
            extra_args=extra,
            wait=30.0,
        )

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except Exception:  # pragma: no cover - best effort cleanup
            cls.process.kill()
        cls.profile.cleanup()
        cls.mock.__exit__(None, None, None)

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = BROWSER_SYNC.main(argv)
        return code, out.getvalue(), err.getvalue()

    def session_argv(self, origin: str | None = None) -> list[str]:
        return ["--endpoint", self.endpoint, "--origin", origin or self.mock.origin]

    def test_check_reports_the_logged_in_account(self):
        code, out, _err = self.run_cli(["check", *self.session_argv()])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["logged_in"])
        self.assertEqual(payload["username"], "demo_user")
        self.assertEqual(payload["collections"], 1)

    def test_probe_says_safe_when_a_collection_feed_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _err = self.run_cli([
                "probe", *self.session_argv(), "--output-root", tmp,
            ])
        self.assertEqual(code, 0)
        self.assertIn("可以同步", out)
        self.assertIn("自然", out)

    def test_collections_lists_names_and_ids(self):
        code, out, _err = self.run_cli(["collections", *self.session_argv()])
        self.assertEqual(code, 0)
        self.assertIn("自然", out)
        self.assertIn(_Handler.collection_id, out)

    def test_inventory_paginates_and_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inventory.json"
            code, out, _err = self.run_cli([
                "inventory", *self.session_argv(),
                "--collection", "自然", "--delay-ms", "10", "--output", str(target),
            ])
            self.assertEqual(code, 0)
            inventory = json.loads(out)
            # two API pages, three posts: the carousel counts once
            self.assertEqual(len(inventory["collections"][0]["posts"]), 3)
            self.assertEqual(json.loads(target.read_text()), inventory)

    def test_sync_downloads_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "自然"
            argv = [
                "sync", *self.session_argv(), "--collection", "自然",
                "--delay-ms", "10", "--output-dir", str(output),
            ]
            code, out, _err = self.run_cli([*argv, "--jobs", "3"])
            self.assertEqual(code, 0)
            summary = json.loads(out)
            self.assertEqual(summary["downloaded"], 3)
            self.assertEqual(summary["failed"], 0)
            # every item went through the direct CDN path, none through yt-dlp
            self.assertEqual(summary["via_session"], 3)
            self.assertEqual(summary["via_ytdlp"], 0)
            self.assertEqual(summary["jobs"], 3)
            self.assertEqual(summary["failures"], [])

            video = output / "DAbc123" / "DAbc123_demo_user.mp4"
            self.assertEqual(video.read_bytes(), _Handler.media_bytes)
            carousel = sorted(path.name for path in (output / "DGhi789").iterdir())
            self.assertIn("DGhi789_someone_1.mp4", carousel)
            self.assertIn("DGhi789_someone_2.jpg", carousel)

            # the shared validator must accept what the session wrote
            self.assertTrue(SYNC_COMMON.valid_download(output / "DAbc123"))

            # second run is incremental: nothing pending
            code, out, _err = self.run_cli([*argv, "--dry-run"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["pending"], 0)

            # an item dropped from the state (repair --forget) is discovered
            # again and downloaded once more, so the refresh always lands
            state_file = output / "sync-state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            removed = state["items"].pop("DAbc123")
            state_file.write_text(json.dumps(state), encoding="utf-8")
            (output / "DAbc123" / "DAbc123_demo_user.mp4").unlink()
            code, out, _err = self.run_cli([*argv, "--dry-run"])
            self.assertEqual(code, 0)
            pending = json.loads(out)
            self.assertEqual(pending["pending"], 1)
            self.assertEqual(pending["pending_urls"], [removed["url"]])
            code, out, _err = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["downloaded"], 1)

            # the account name was detected, so the state points at a real URL
            state = json.loads((output / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["source"], f"{self.mock.origin}demo_user/saved/_/{_Handler.collection_id}/")

    def test_the_retirement_note_is_one_clean_line(self):
        with MockInstagram(rest=False) as mock_api, tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli([
                "sync", *self.session_argv(mock_api.origin), "--all-collections",
                "--delay-ms", "10", "--output-root", tmp,
            ])
            self.assertEqual(code, 0, err)
            self.assertIn("HTML shell", err)
            self.assertNotIn("<!DOCTYPE", err)
            self.assertNotIn("<html", err)

    def test_sync_all_collections_writes_one_folder_per_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _err = self.run_cli([
                "sync", *self.session_argv(), "--all-collections",
                "--delay-ms", "10", "--output-root", tmp,
            ])
            self.assertEqual(code, 0)
            summary = json.loads(out)
            self.assertEqual(summary["collections"], 1)
            self.assertEqual(summary["downloaded"], 3)
            self.assertEqual(summary["errors"], 0)
            self.assertTrue((Path(tmp) / "自然" / "sync-state.json").is_file())
            self.assertTrue((Path(tmp) / "自然" / "DGhi789" / "manifest.json").is_file())

    def test_retired_rest_routes_fall_back_to_page_capture(self):
        """The real failure: collections/list answers 404, the page still works."""
        with MockInstagram(rest=False) as mock_api, tempfile.TemporaryDirectory() as tmp:
            argv = self.session_argv(mock_api.origin)
            slow = ["--scroll-rounds", "6", "--scroll-delay-ms", "40"]

            code, out, _err = self.run_cli(["check", *argv, *slow])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["logged_in"])
            self.assertEqual(payload["collections_source"], "browser-network")
            self.assertEqual(payload["collection_names"], ["自然"])
            self.assertNotIn("collections_error", {k: v for k, v in payload.items() if k == "collections_error"})

            code, out, _err = self.run_cli(["collections", *argv, *slow])
            self.assertEqual(code, 0)
            self.assertIn("自然", out)
            self.assertIn(_Handler.collection_id, out)

            output = Path(tmp) / "自然"
            code, out, err = self.run_cli([
                "sync", *argv, "--collection", "自然", "--output-dir", str(output),
                "--jobs", "3", *slow,
            ])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["via_session"], 3)
            self.assertEqual(summary["via_ytdlp"], 0)
            self.assertEqual(summary["downloaded"], 3)
            self.assertTrue((output / "DGhi789" / "manifest.json").is_file())
            self.assertEqual(
                (output / "DAbc123" / "DAbc123_demo_user.mp4").read_bytes(),
                _Handler.media_bytes,
            )

    def test_collections_are_harvested_from_the_rendered_page(self):
        """No JSON collection list at all: the sidebar links still name them."""
        with MockInstagram(rest=False, collections=False) as mock_api:
            argv = self.session_argv(mock_api.origin)
            slow = ["--scroll-rounds", "6", "--scroll-delay-ms", "40"]
            code, out, err = self.run_cli(["collections", *argv, *slow])
            self.assertEqual(code, 0, err)
            self.assertIn("自然", out)
            self.assertIn(_Handler.collection_id, out)
            code, out, err = self.run_cli(["check", *argv, *slow])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["collections_source"], "browser-network")

    def test_all_collections_survives_an_unreadable_collection_list(self):
        """Worst case: even the page's own list is unusable, sync everything."""
        with MockInstagram(rest=False, collections=False, collection_links=False) as mock_api, tempfile.TemporaryDirectory() as tmp:
            argv = self.session_argv(mock_api.origin)
            slow = ["--scroll-rounds", "6", "--scroll-delay-ms", "40"]
            code, out, err = self.run_cli([
                "sync", *argv, "--all-collections", "--output-root", tmp, "--jobs", "3", *slow,
            ])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["collections"], 1)
            self.assertEqual(summary["downloaded"], 3)
            self.assertEqual(summary["via_session"], 3)
            self.assertEqual(summary["via_ytdlp"], 0)
            self.assertTrue((Path(tmp) / "全部收藏" / "DAbc123" / "manifest.json").is_file())

    def test_diagnose_explains_a_retired_endpoint(self):
        with MockInstagram(rest=False) as mock_api:
            code, out, err = self.run_cli([
                "diagnose", *self.session_argv(mock_api.origin),
                "--scroll-rounds", "6", "--scroll-delay-ms", "40",
            ])
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertTrue(report["logged_in"])
            self.assertFalse(report["rest_feed"]["ok"])
            self.assertGreaterEqual(report["saved_page"]["json_responses"], 1)
            self.assertGreater(report["saved_page"]["media_items"], 0)
            self.assertTrue(
                any("/graphql/" in url for url in report["api_paths_called"]),
                report["api_paths_called"],
            )

    def test_python_projection_matches_the_page_projection(self):
        raw = _Handler.page_two()["items"][0]
        with BROWSER_SESSION.InstagramSession(
            endpoint=self.endpoint, origin=self.mock.origin, timeout=30
        ) as session:
            script = BROWSER_SESSION.js_prelude() + f"__ins2anki.project({json.dumps(raw)})"
            page_item = session.evaluate(script)
        self.assertEqual(page_item, BROWSER_SESSION.project_media(raw))

    def test_all_collections_continues_in_the_existing_folders(self):
        """The migration case: keep downloading into the folders already there."""
        with MockInstagram(rest=False, collections=False, collection_links=False) as mock_api, \
                tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            argv = self.session_argv(mock_api.origin)
            slow = ["--scroll-rounds", "6", "--scroll-delay-ms", "40"]

            # seed one already-completed item in 自然/, as the earlier runs did
            item = _media_item(mock_api.origin, "3546162228685415517", "DAbc123", "demo_user", 1751500000)
            item_dir = root / "自然" / "DAbc123"
            item_dir.mkdir(parents=True)
            media = item_dir / "DAbc123_demo_user.mp4"
            media.write_bytes(b"old bytes")
            (item_dir / "manifest.json").write_text(json.dumps(BROWSER_SESSION.build_manifest(
                item, [media], item_dir
            )), encoding="utf-8")
            (root / "自然" / "sync-state.json").write_text(json.dumps({
                "source": f"{mock_api.origin}demo_user/saved/_/{_Handler.collection_id}/",
                "items": {"DAbc123": {"status": "completed", "output_dir": str(item_dir)}},
            }), encoding="utf-8")

            code, out, err = self.run_cli([
                "sync", *argv, "--all-collections", "--output-root", str(root), "--jobs", "3", *slow,
            ])
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            self.assertEqual(summary["collections"], 1)
            # the completed item was not fetched again, the other two were
            self.assertEqual(summary["downloaded"], 2)
            self.assertEqual(media.read_bytes(), b"old bytes")
            self.assertEqual(
                (root / "自然" / "DDef456" / "DDef456_demo_user.jpg").read_bytes(),
                _Handler.media_bytes,
            )
            self.assertFalse((root / "全部收藏").exists())

    def test_dom_fallback_harvests_rendered_links(self):
        with BROWSER_SESSION.InstagramSession(
            endpoint=self.endpoint, origin=self.mock.origin, timeout=30
        ) as session:
            links = session.dom_links(self.mock.origin, rounds=3, delay_ms=10)
        self.assertEqual(links, [])


if __name__ == "__main__":
    unittest.main()
