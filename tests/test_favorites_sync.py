"""One-command favorites sync: harvest through the browser, then download.

Two layers, mirroring ``test_browser_session.py``:

* pure helpers — token-bearing URL building, folder de-duplication, Douyin
  media mining, the Douyin direct download and the CLI wiring;
* an end-to-end test that drives a real headless Chromium against a mock
  Xiaohongshu 收藏 page, proving the pre-document hook, the scrolling capture,
  the shared incremental state and a resumable second pass all work together.

Set ``INS2ANKI_SKIP_BROWSER_TESTS=1`` to skip the browser layer.
"""

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
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
BROWSER_SESSION = load("browser_session")
CDP = load("cdp")
FAVORITES = load("favorites_sync")
SYNC_COMMON = load("sync_common")


XHS_IDS = [
    "0a1b2c3d4e5f60718293a4b1",
    "0a1b2c3d4e5f60718293a4b2",
    "0a1b2c3d4e5f60718293a4b3",
    "0a1b2c3d4e5f60718293a4b4",
    "0a1b2c3d4e5f60718293a4b5",
]
XHS_TOKEN = "ABQ7kZocAvMAGCF1MnOCV1ZD-oXKWx74yD5nAUz3LZS1I="
FOLDER_ID = "ffee00112233445566778899"


def xhs_harvest(pages: int = 3) -> dict:
    """A capture shaped the way the injected hook writes it."""
    notes = XHS_IDS[: pages + 2]
    return {
        "tokens": {note_id: XHS_TOKEN for note_id in notes},
        "folders": [{"id": FOLDER_ID, "name": "英语"}, {"id": FOLDER_ID, "name": "英语"}],
        "awemes": {},
        "api": ["/api/sns/web/v1/note/collect/page?cursor=0"],
    }


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


class HarvestMappingTest(unittest.TestCase):
    def test_xhs_items_carry_a_url_encoded_token(self):
        urls = FAVORITES.xhs_items(xhs_harvest())
        self.assertEqual(len(urls), len(XHS_IDS))
        self.assertTrue(all("xsec_token=ABQ7kZoc" in url for url in urls))
        self.assertTrue(all("xsec_source=pc_user" in url for url in urls))
        # the base64 padding and slashes must survive as valid query data
        self.assertIn("%3D", urls[0])
        self.assertNotIn(" ", urls[0])

    def test_xhs_items_skip_ids_that_are_not_note_ids(self):
        harvest = {"tokens": {"not-a-note": XHS_TOKEN, XHS_IDS[0]: XHS_TOKEN}}
        urls = FAVORITES.xhs_items(harvest)
        self.assertEqual(len(urls), 1)
        self.assertIn(XHS_IDS[0], urls[0])

    def test_xhs_items_without_a_token_stay_bare(self):
        urls = FAVORITES.xhs_items({"tokens": {XHS_IDS[0]: ""}})
        self.assertEqual(urls, [f"https://www.xiaohongshu.com/explore/{XHS_IDS[0]}"])

    def test_xhs_items_tolerate_a_broken_capture(self):
        for broken in ({}, {"tokens": None}, {"tokens": "nope"}, "not a dict"):
            self.assertEqual(FAVORITES.xhs_items(broken), [])

    def test_folders_are_deduplicated(self):
        self.assertEqual(
            FAVORITES.folders_of(xhs_harvest()), [{"id": FOLDER_ID, "name": "英语"}]
        )

    def test_discovered_preserves_the_token_for_xhs(self):
        discovered = FAVORITES.discovered_for("xiaohongshu", xhs_harvest())
        self.assertEqual(len(discovered), len(XHS_IDS))
        platform, item_id, url = discovered[0]
        self.assertEqual(platform, "xiaohongshu")
        self.assertEqual(item_id, XHS_IDS[0])
        self.assertIn("xsec_token=", url)

    def test_awemes_keep_only_items_with_media(self):
        harvest = {"awemes": {
            "7312345678901234567": {"id": "7312345678901234567", "videos": ["https://v/1.mp4"]},
            "7312345678901234568": {"id": "7312345678901234568", "videos": [], "images": []},
            "nope": {"id": "nope", "videos": ["https://v/3.mp4"]},
        }}
        records = FAVORITES.awemes_of(harvest)
        self.assertEqual(list(records), ["7312345678901234567"])

    def test_douyin_discovery_uses_aweme_urls(self):
        harvest = {"awemes": {
            "7312345678901234567": {
                "id": "7312345678901234567",
                "videos": ["https://v/1.mp4"],
                "url": "https://www.douyin.com/video/7312345678901234567",
            },
        }}
        self.assertEqual(
            FAVORITES.discovered_for("douyin", harvest),
            [("douyin", "7312345678901234567", "https://www.douyin.com/video/7312345678901234567")],
        )


class DouyinDownloadTest(unittest.TestCase):
    record = {
        "id": "7312345678901234567",
        "url": "https://www.douyin.com/video/7312345678901234567",
        "title": "标题",
        "author": "someone",
        "videos": ["https://cdn.example/play.mp4"],
        "images": [],
    }

    def test_video_is_saved_with_a_manifest(self):
        def fake(url, destination, headers=None, **_kwargs):
            self.assertIn("douyin.com", headers["Referer"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"video")
            return 5

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            FAVORITES.browser_session, "download_url", side_effect=fake
        ):
            directory = Path(tmp) / self.record["id"]
            ok, detail = FAVORITES.douyin_download(self.record, directory)
            self.assertTrue(ok, detail)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "video")
            self.assertEqual(manifest["platform"], "douyin")
            self.assertTrue(SYNC_COMMON.valid_download(directory))

    def test_image_note_saves_every_image(self):
        record = dict(self.record, videos=[], images=["https://cdn/1.jpg", "https://cdn/2.jpg"])

        def fake(_url, destination, headers=None, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"jpg")
            return 3

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            FAVORITES.browser_session, "download_url", side_effect=fake
        ):
            directory = Path(tmp) / record["id"]
            ok, _detail = FAVORITES.douyin_download(record, directory)
            self.assertTrue(ok)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "images")
            self.assertEqual(len(manifest["media"]), 2)

    def test_no_media_is_a_failure_not_a_cover(self):
        record = dict(self.record, videos=[], images=[])
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = FAVORITES.douyin_download(record, Path(tmp) / "x")
        self.assertFalse(ok)
        self.assertIn("neither a video nor image", detail)

    def test_engine_integration_maps_urls_to_records(self):
        writes: list[str] = []

        def fake(url, destination, headers=None, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"video")
            writes.append(url)
            return 5

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            FAVORITES.browser_session, "download_url", side_effect=fake
        ):
            records = {self.record["url"]: self.record}
            output = Path(tmp) / "douyin"
            code = SYNC_COMMON.sync_items(
                FAVORITES.discovered_for("douyin", {"awemes": {self.record["id"]: self.record}}),
                output_dir=output,
                state_file=output / "sync-state.json",
                downloader=FAVORITES.DOWNLOADER,
                download_fn=FAVORITES.douyin_download_fn(records),
                source_label="test",
                stream=StringIO(),
            )
            self.assertEqual(code, 0)
            self.assertEqual(writes, self.record["videos"])
            state = json.loads((output / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["items"][self.record["id"]]["status"], "completed")


class HarvestStopRulesTest(unittest.TestCase):
    """The scroll loop must not give up while the page is still loading.

    A real Douyin sync stopped at the first page (19 of 234 items): two empty
    rounds while the collection tab hydrated ate the stability budget, and a
    background tab never fired the lazy loads at all. The rules now are: an
    empty round never counts as stable (a slow boot gets its chance), nothing
    counts before the first item appears beyond a hard cap, and the tab is
    brought to the front so its timers actually run.
    """

    class _Stub:
        """A session-less FavoritesSession replaying scripted round counts."""

        script: list
        brought_to_front = False
        timeout = 5  # harvest() reads self.timeout for every evaluate()

        def __init__(self, script):
            self.script = script
            self.rounds = 0

        def harvest(self, **kwargs):
            kwargs.setdefault("rounds", 60)
            kwargs.setdefault("delay_ms", 1)
            return self._fs.FavoritesSession.harvest(self, **kwargs)

        def install_hook(self):
            pass

        def call(self, method, params=None, timeout=None):
            if method == "Page.bringToFront":
                HarvestStopRulesTest._Stub.brought_to_front = True
            return {}

        def evaluate(self, expression, timeout=None):
            if "scrollTop" in expression:  # SCROLL_JS
                counts = self.script[min(self.rounds, len(self.script) - 1)]
                self.rounds += 1
                return {"tokens": 0, "awemes": counts, "folders": 0}
            if "clear" in expression:
                return None
            peak = max(self.script) if self.script else 0
            return {"awemes": {str(n): {} for n in range(1, peak + 1)}}

    def test_slow_first_page_is_not_abandoned(self):
        stub = self._Stub([0, 0, 5, 5, 5])
        harvest = self._fs_harvest(stub)
        self.assertEqual(len(harvest.get("awemes", {})), 5)
        self.assertTrue(self._Stub.brought_to_front)

    def test_growth_resets_and_stable_triples_stop(self):
        stub = self._Stub([3, 3, 9, 15, 15, 15])
        self._fs_harvest(stub)
        # rounds 5-7 repeat 15 three times: stop on the 7th scroll
        self.assertEqual(stub.rounds, 7)

    def test_dead_page_gives_up_after_the_empty_cap(self):
        stub = self._Stub([0] * 30)
        self._fs_harvest(stub)
        self.assertEqual(stub.rounds, 10)

    def test_zero_after_items_is_not_stability(self):
        stub = self._Stub([5] + [0] * 12)
        self._fs_harvest(stub)
        self.assertEqual(stub.rounds, 11)

    def _fs_harvest(self, stub):
        real_sleep = FAVORITES.time.sleep
        FAVORITES.time.sleep = lambda _seconds: None
        try:
            return FAVORITES.FavoritesSession.harvest(stub)
        finally:
            FAVORITES.time.sleep = real_sleep


class CliTest(unittest.TestCase):
    def test_sync_defaults_to_xiaohongshu(self):
        args = FAVORITES.build_parser().parse_args(["sync", "--output-dir", "/tmp/x"])
        self.assertIs(args.func, FAVORITES.command_sync)
        self.assertEqual(args.platform, "xiaohongshu")
        self.assertIsNone(args.folder)
        self.assertEqual(args.jobs, 1)
        self.assertFalse(args.launch)
        self.assertEqual(args.endpoint, CDP.DEFAULT_ENDPOINT)

    def test_platform_choices_are_the_supported_ones(self):
        for platform in ("xiaohongshu", "douyin"):
            args = FAVORITES.build_parser().parse_args(
                ["check", "--platform", platform]
            )
            self.assertEqual(args.platform, platform)

    def test_instagram_is_rejected_with_a_pointer_to_the_other_tool(self):
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            FAVORITES.build_parser().parse_args(["check", "--platform", "instagram"])

    def test_favorites_url_override_skips_account_discovery(self):
        args = FAVORITES.build_parser().parse_args([
            "check", "--favorites-url", "http://127.0.0.1:9/explore",
        ])
        self.assertEqual(
            FAVORITES.favorites_url(args, None), "http://127.0.0.1:9/explore"
        )

    def test_douyin_favorites_url_is_the_collection_tab(self):
        args = FAVORITES.build_parser().parse_args(["check", "--platform", "douyin"])
        self.assertEqual(FAVORITES.favorites_url(args, None), FAVORITES.DOUYIN_FAVORITES_URL)

    def test_sync_reports_a_missing_session_instead_of_crashing(self):
        out, err = StringIO(), StringIO()
        with mock.patch.object(
            FAVORITES.browser_session, "is_browser_running", return_value=False
        ), redirect_stdout(out), redirect_stderr(err):
            code = FAVORITES.main(["sync", "--output-dir", "/tmp/never"])
        self.assertEqual(code, 2)
        self.assertIn("no browser is listening", err.getvalue())


# --------------------------------------------------------------------------
# Mock Xiaohongshu
# --------------------------------------------------------------------------

MOCK_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>收藏</title></head>
<body>
<div style="height:8000px"></div>
<script>
window.__INITIAL_STATE__ = %(state)s;
const PAGES = %(pages)s;
PAGES.forEach((payload, index) => {
  setTimeout(() => {
    fetch("/api/sns/web/v1/note/collect/page?cursor=" + index)
      .then((response) => response.json())
      .then(() => {
        const marker = document.createElement("div");
        marker.textContent = "page-" + index;
        document.body.appendChild(marker);
      })
      .catch(() => {});
  }, index * 700);
});
</script>
</body></html>
"""


def note_payload(note_id: str) -> dict:
    return {"note_id": note_id, "xsec_token": XHS_TOKEN, "note_card": {"display_title": note_id}}


class _Handler(http.server.BaseHTTPRequestHandler):
    pages: list = []
    state: dict = {}
    api_paths: list = []

    def log_message(self, *_args):  # keep the test output readable
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/api/"):
            type(self).api_paths.append(self.path)
            index = 0
            if "cursor=" in self.path:
                index = int(self.path.split("cursor=")[1].split("&")[0] or 0)
            payload = self.pages[index] if index < len(self.pages) else {"notes": [], "folders": []}
            self._send(json.dumps({"success": True, "data": payload}), "application/json")
            return
        self._send(
            MOCK_PAGE % {"pages": json.dumps(self.pages), "state": json.dumps(self.state)},
            "text/html",
        )

    def _send(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockXiaohongshu:
    """A threaded server that behaves like the 收藏 page of Xiaohongshu.

    The note list arrives two ways on the real site, and both are exercised
    here: the first page is server-rendered into the boot state, later pages
    come from the list API as the page scrolls.
    """

    def __init__(self, pages: list | None = None):
        self.port = free_port()
        self.pages = pages or [
            {"notes": [note_payload(note_id) for note_id in XHS_IDS[:3]],
             "folders": [{"folder_id": FOLDER_ID, "folder_name": "英语"}]},
            {"notes": [note_payload(XHS_IDS[4])], "folders": []},
            {"notes": [], "folders": []},
        ]
        self.state = {"data": {"notes": [note_payload(XHS_IDS[3])]}}
        handler = type("Handler", (_Handler,), {
            "pages": self.pages,
            "state": self.state,
            "api_paths": [],
        })
        self.handler = handler
        socketserver.TCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def favorites_url(self) -> str:
        return f"{self.origin}explore"

    def __enter__(self) -> "MockXiaohongshu":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


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
class FavoritesBrowserTest(unittest.TestCase):
    """Drive headless Chromium against the mock 收藏 page, end to end."""

    @classmethod
    def setUpClass(cls):
        executable = browser_executable()
        if not executable:
            raise unittest.SkipTest("no Chromium-based browser found")
        cls.mock = MockXiaohongshu()
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

    def session_argv(self) -> list[str]:
        return [
            "--endpoint", self.endpoint,
            "--origin", self.mock.origin,
            "--favorites-url", self.mock.favorites_url,
            "--scroll-delay-ms", "250",
            "--scroll-rounds", "20",
        ]

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = FAVORITES.main(argv)
        return code, out.getvalue(), err.getvalue()

    def fake_download(self, downloader, url, output_dir, cookies, cookies_from_browser):
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        media = directory / "note.jpg"
        media.write_bytes(b"image")
        (directory / "manifest.json").write_text(json.dumps({
            "source_url": url,
            "platform": "xiaohongshu",
            "kind": "images",
            "media": [str(media)],
            "metadata": [],
        }, ensure_ascii=False), encoding="utf-8")
        return True, "ok"

    def test_check_reports_the_capture(self):
        code, out, _err = self.run_cli(["check", *self.session_argv(), "--verbose"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["logged_in"])
        self.assertGreaterEqual(payload["found"], 3)
        self.assertIn("英语", payload["folders"])

    def test_sync_downloads_every_note_with_a_token_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "收藏"
            argv = ["sync", *self.session_argv(), "--output-dir", str(output)]
            with mock.patch.object(
                FAVORITES.sync_common, "run_download", side_effect=self.fake_download
            ):
                code, out, _err = self.run_cli(argv)
                self.assertEqual(code, 0)
                summary = json.loads(out)
                self.assertEqual(summary["discovered"], len(XHS_IDS))
                self.assertEqual(summary["downloaded"], len(XHS_IDS))
                self.assertEqual(summary["failed"], 0)

                # a fresh pass finds nothing pending: the state is incremental
                code, out, _err = self.run_cli([*argv, "--dry-run"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["pending"], 0)

            state = json.loads((output / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["source"], self.mock.favorites_url)
            for note_id in XHS_IDS:
                self.assertEqual(state["items"][note_id]["status"], "completed")
                self.assertIn("xsec_token=", state["items"][note_id]["url"])
                self.assertTrue(SYNC_COMMON.valid_download(output / note_id))
            # one note came from the server-rendered boot state, the rest from
            # the list API as the page was scrolled
            self.assertIn(XHS_IDS[3], state["items"])
            self.assertIn(XHS_IDS[4], state["items"])

    def test_sync_reports_when_nothing_was_captured(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.mock.handler, "pages", [{"notes": [], "folders": []}]
        ), mock.patch.object(self.mock.handler, "state", {"data": {"notes": []}}):
            code, _out, err = self.run_cli([
                "sync", *self.session_argv(), "--output-dir", str(Path(tmp) / "empty"),
            ])
        self.assertEqual(code, 2)
        self.assertIn("no favorites", err)
        # the API paths the page called are the only evidence left; they must
        # survive even though nothing was mined from them
        self.assertIn("/api/sns/web/v1/note/collect/page", err)

    def test_diagnose_lists_the_api_paths_one_capture_costs(self):
        code, out, _err = self.run_cli(["diagnose", *self.session_argv()])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertGreaterEqual(payload["notes_with_token"], 3)
        self.assertTrue(any("/api/sns/web" in path for path in payload["api_paths_called"]))


if __name__ == "__main__":
    unittest.main()
