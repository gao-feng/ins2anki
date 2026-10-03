"""Tests for the shared multi-platform sync engine, downloader and adapters."""

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name: str):
    """Import a script module, sharing one instance with normal imports.

    Registering in ``sys.modules`` keeps ``from platforms import ...`` inside
    the scripts bound to the same object the tests patch.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
PLATFORMS = load("platforms")
SYNC_SAVED = load("sync_saved")
DOWNLOAD_MEDIA = load("download_media")
SYNC_COMMON = load("sync_common")


def write_manifest(directory: Path, filenames: tuple[str, ...]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    media = []
    for filename in filenames:
        path = directory / filename
        path.write_bytes(b"data")
        media.append(str(path))
    (directory / "manifest.json").write_text(
        json.dumps({"media": media}), encoding="utf-8"
    )


class PlatformAdapterTests(unittest.TestCase):
    def test_detect_platform(self):
        self.assertEqual(
            PLATFORMS.detect_platform("https://www.instagram.com/reel/ABC_1/"),
            "instagram",
        )
        self.assertEqual(
            PLATFORMS.detect_platform(
                "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
            ),
            "xiaohongshu",
        )
        self.assertEqual(
            PLATFORMS.detect_platform("https://www.douyin.com/video/6961737553342991651"),
            "douyin",
        )
        self.assertIsNone(PLATFORMS.detect_platform("https://example.com/p/1"))

    def test_short_links_are_detected_but_not_normalized(self):
        self.assertTrue(PLATFORMS.is_short_link("https://xhslink.com/aBcDeF"))
        self.assertTrue(PLATFORMS.is_short_link("https://v.douyin.com/iABC123/"))
        self.assertIsNone(PLATFORMS.normalize("https://xhslink.com/aBcDeF"))
        self.assertEqual(PLATFORMS.detect_platform("https://v.douyin.com/iABC123/"), "douyin")

    def test_xiaohongshu_keeps_xsec_token(self):
        note_id, url = PLATFORMS.normalize(
            "https://www.xiaohongshu.com/explore/674051740000000007027a15"
            "?xsec_token=CBgeL8&xsec_source=pc_feed&utm_source=share"
        )
        self.assertEqual(note_id, "674051740000000007027a15")
        self.assertIn("xsec_token=CBgeL8", url)
        self.assertIn("xsec_source=pc_feed", url)
        self.assertNotIn("utm_source", url)

    def test_xiaohongshu_discovery_and_douyin_note_forms(self):
        self.assertEqual(
            PLATFORMS.normalize(
                "https://www.xiaohongshu.com/discovery/item/6411cf99000000001300b6d9"
            ),
            ("6411cf99000000001300b6d9",
             "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"),
        )
        self.assertEqual(
            PLATFORMS.normalize("https://www.douyin.com/note/7351234567890123456"),
            ("7351234567890123456", "https://www.douyin.com/note/7351234567890123456"),
        )
        self.assertEqual(
            PLATFORMS.normalize(
                "https://www.iesdouyin.com/share/video/6961737553342991651"
            ),
            ("6961737553342991651", "https://www.douyin.com/video/6961737553342991651"),
        )

    def test_normalize_many_dedupes_by_platform_and_id(self):
        result = PLATFORMS.normalize_many([
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=A",
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=B",
            "https://www.douyin.com/video/6961737553342991651",
            "junk",
        ])
        self.assertEqual(len(result), 2)
        # The first occurrence wins, so the usable token is retained.
        self.assertEqual(result[0][0], "xiaohongshu")
        self.assertIn("xsec_token=A", result[0][2])

    def test_collection_suffix_for_each_platform(self):
        self.assertEqual(
            PLATFORMS.collection_suffix("https://instagram.com/u/saved/_/123456789/"),
            "23456789",
        )
        self.assertEqual(
            PLATFORMS.collection_suffix(
                "https://www.xiaohongshu.com/user/profile/5c31698d0000000007018a31"
            ),
            "07018a31",
        )
        self.assertEqual(PLATFORMS.collection_suffix("https://www.douyin.com/video/1"), "")


class CollectionDirectoryTests(unittest.TestCase):
    def test_duplicate_names_across_platforms_stay_unique(self):
        collections = [
            {"name": "英语", "platform": "xiaohongshu", "posts": []},
            {"name": "英语", "platform": "douyin", "posts": []},
        ]
        self.assertEqual(
            SYNC_COMMON.assign_directories(collections), ["英语", "英语-2"]
        )

    def test_inventory_validates_platform_field(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "collections.json"
            path.write_text(json.dumps({"collections": [
                {"name": "收藏", "platform": 7, "posts": []}
            ]}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                SYNC_COMMON.load_inventory(path)


class GenericSyncEngineTests(unittest.TestCase):
    def _run(self, urls: list[str], output_dir: Path, download_fn, extra=None):
        urls_file = output_dir.parent / "urls.txt"
        urls_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
        argv = [
            "--urls-file", str(urls_file),
            "--output-dir", str(output_dir),
        ] + (extra or [])
        with mock.patch.object(sys, "argv", ["sync_saved.py"] + argv), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return SYNC_SAVED.run(argv, download_fn=download_fn)

    def test_mixed_platforms_are_recorded_and_dispatched_per_url(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "out"
            output_dir.mkdir()
            seen: list[str] = []

            def fake_download(_downloader, url, destination, _cookies, _browser):
                seen.append(url)
                write_manifest(destination, ("clip.mp4",))
                return True, "ok"

            code = self._run([
                "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=A",
                "https://www.douyin.com/video/6961737553342991651",
                "https://www.instagram.com/reel/ABC_123/",
                "https://example.com/not-supported",
            ], output_dir, fake_download)

            self.assertEqual(code, 0)
            self.assertEqual(len(seen), 3)
            state = json.loads((output_dir / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["items"]["6411cf99000000001300b6d9"]["platform"], "xiaohongshu"
            )
            self.assertEqual(
                state["items"]["6961737553342991651"]["platform"], "douyin"
            )
            self.assertEqual(state["items"]["ABC_123"]["platform"], "instagram")
            self.assertNotIn("not-supported", state["items"])

    def test_second_run_skips_and_records_platform_per_item(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "out"
            output_dir.mkdir()
            calls: list[str] = []

            def fake_download(_downloader, url, destination, _cookies, _browser):
                calls.append(url)
                write_manifest(destination, ("note.jpg",))
                return True, "ok"

            urls = ["https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"]
            self.assertEqual(self._run(urls, output_dir, fake_download), 0)
            self.assertEqual(self._run(urls, output_dir, fake_download), 0)
            self.assertEqual(len(calls), 1)

    def test_failed_items_are_retried_unless_disabled(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "out"
            output_dir.mkdir()
            attempts: list[int] = []

            def failing(_downloader, _url, _destination, _cookies, _browser):
                attempts.append(1)
                return False, "boom"

            urls = ["https://www.douyin.com/video/6961737553342991651"]
            self.assertEqual(self._run(urls, output_dir, failing), 2)
            self.assertEqual(self._run(urls, output_dir, failing), 2)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(
                self._run(urls, output_dir, failing, ["--no-retry-failed"]), 0
            )
            self.assertEqual(len(attempts), 2)

    def test_dry_run_discovers_without_downloading(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "out"
            output_dir.mkdir()
            calls: list[str] = []

            def fake_download(_downloader, url, _destination, _cookies, _browser):
                calls.append(url)
                return True, "ok"

            code = self._run(
                ["https://www.douyin.com/video/6961737553342991651"],
                output_dir,
                fake_download,
                ["--dry-run"],
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls, [])
            self.assertTrue((output_dir / "sync-state.json").is_file())

    def test_xiaohongshu_collection_url_is_rejected_with_guidance(self):
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw) / "out"
            argv = [
                "--collection-url",
                "https://www.xiaohongshu.com/user/profile/5c31698d0000000007018a31",
                "--output-dir", str(output_dir),
            ]
            stderr = StringIO()
            with redirect_stderr(stderr), redirect_stdout(StringIO()):
                code = SYNC_SAVED.run(argv, download_fn=lambda *a: (False, ""))
            self.assertEqual(code, 2)
            self.assertIn("export_collection.js", stderr.getvalue())

    def test_short_links_are_resolved_before_normalizing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "out"
            output_dir.mkdir()
            seen: list[str] = []

            def fake_download(_downloader, url, destination, _cookies, _browser):
                seen.append(url)
                write_manifest(destination, ("clip.mp4",))
                return True, "ok"

            with mock.patch.object(
                PLATFORMS,
                "resolve_short_link",
                return_value="https://www.douyin.com/video/6961737553342991651",
            ):
                code = self._run(
                    ["https://v.douyin.com/iABC123/"], output_dir, fake_download
                )
            self.assertEqual(code, 0)
            self.assertEqual(seen, ["https://www.douyin.com/video/6961737553342991651"])


class DownloadStrategyTests(unittest.TestCase):
    """The probe must pick video vs. image gallery without false positives."""

    def _run_download(self, url, payload, platform=None):
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if "--dump-single-json" in cmd:
                stdout = json.dumps(payload) if payload is not None else ""
                return mock.Mock(returncode=0 if payload is not None else 1,
                                 stdout=stdout, stderr="probe failed")
            out_dir = Path(cmd[cmd.index("--output") + 1]).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            names = ("n1.jpg", "n2.jpg") if "--write-all-thumbnails" in cmd else ("clip.mp4",)
            for name in names:
                (out_dir / name).write_bytes(b"data")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = Path(temp.name) / "item"
        argv = [url, "--output-dir", str(out)]
        if platform:
            argv += ["--platform", platform]
        with mock.patch.object(DOWNLOAD_MEDIA.shutil, "which", return_value="/usr/bin/true"), \
                mock.patch.object(DOWNLOAD_MEDIA.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = DOWNLOAD_MEDIA.run(argv)
        manifest = None
        if (out / "manifest.json").is_file():
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        return code, manifest, calls

    def test_video_formats_use_the_video_pass(self):
        code, manifest, calls = self._run_download(
            "https://www.douyin.com/video/6961737553342991651",
            {"id": "x", "formats": [{"url": "http://v"}], "thumbnails": [{"url": "http://c"}]},
        )
        self.assertEqual(code, 0)
        self.assertEqual(manifest["kind"], "video")
        media_calls = [c for c in calls if "--dump-single-json" not in c]
        self.assertEqual(len(media_calls), 1)
        self.assertIn("--write-description", media_calls[0])
        self.assertNotIn("--write-all-thumbnails", media_calls[0])

    def test_xiaohongshu_image_note_uses_the_gallery_pass(self):
        code, manifest, calls = self._run_download(
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            {"id": "x", "formats": [], "thumbnails": [{"url": "http://i1"}, {"url": "http://i2"}]},
            platform="xiaohongshu",
        )
        self.assertEqual(code, 0)
        self.assertEqual(manifest["kind"], "images")
        self.assertEqual(len(manifest["media"]), 2)
        media_calls = [c for c in calls if "--dump-single-json" not in c]
        self.assertIn("--write-all-thumbnails", media_calls[0])

    def test_douyin_cover_is_not_mistaken_for_note_images(self):
        code, manifest, calls = self._run_download(
            "https://www.douyin.com/note/7351234567890123456",
            {"id": "x", "formats": [], "thumbnails": [{"url": "http://cover"}]},
            platform="douyin",
        )
        self.assertEqual(code, 1)
        self.assertIsNone(manifest)
        # Only the probe ran; no media pass was attempted.
        self.assertEqual(len(calls), 1)

    def test_probe_failure_falls_back_to_a_plain_download(self):
        code, manifest, calls = self._run_download(
            "https://www.instagram.com/p/ABC_123/",
            None,
            platform="instagram",
        )
        self.assertEqual(code, 0)
        self.assertEqual(manifest["kind"], "video")
        self.assertEqual(manifest["platform"], "instagram")


if __name__ == "__main__":
    unittest.main()
