import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts/sync_instagram_saved.py"
SPEC = importlib.util.spec_from_file_location("sync_instagram_saved", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

COLLECTIONS_SCRIPT = (
    Path(__file__).parents[1]
    / "scripts/sync_instagram_collections.py"
)
COLLECTIONS_SPEC = importlib.util.spec_from_file_location(
    "sync_instagram_collections", COLLECTIONS_SCRIPT
)
COLLECTIONS_MODULE = importlib.util.module_from_spec(COLLECTIONS_SPEC)
assert COLLECTIONS_SPEC.loader
COLLECTIONS_SPEC.loader.exec_module(COLLECTIONS_MODULE)


class SyncInstagramSavedTests(unittest.TestCase):
    def test_normalize_and_deduplicate_urls(self):
        urls = MODULE.unique_urls([
            "https://www.instagram.com/reel/ABC_123/?utm_source=x",
            "https://instagram.com/p/ABC_123/",
            "https://www.instagram.com/p/XYZ-9/",
            "not a URL",
        ])
        self.assertEqual(urls, [
            ("ABC_123", "https://www.instagram.com/p/ABC_123/"),
            ("XYZ-9", "https://www.instagram.com/p/XYZ-9/"),
        ])

    def test_valid_download_requires_manifest_and_nonempty_media(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self.assertFalse(MODULE.valid_download(directory))
            media = directory / "clip.mp4"
            media.write_bytes(b"video")
            (directory / "manifest.json").write_text(
                json.dumps({"media": [str(media)]}), encoding="utf-8"
            )
            self.assertTrue(MODULE.valid_download(directory))

    def test_state_write_is_round_trippable(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested/state.json"
            data = {"version": 1, "items": {"ABC": {"status": "failed"}}}
            MODULE.write_json_atomic(path, data)
            self.assertEqual(MODULE.load_state(path), data)

    def test_second_run_skips_a_completed_download(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            urls_file = root / "urls.txt"
            output_dir = root / "downloads"
            urls_file.write_text(
                "https://www.instagram.com/reel/ABC_123/\n", encoding="utf-8"
            )

            def fake_download(_downloader, _url, destination, _cookies, _browser):
                destination.mkdir(parents=True, exist_ok=True)
                media = destination / "clip.mp4"
                media.write_bytes(b"video")
                (destination / "manifest.json").write_text(
                    json.dumps({"media": [str(media)]}), encoding="utf-8"
                )
                return True, "ok"

            argv = [
                "sync_instagram_saved.py",
                "--urls-file", str(urls_file),
                "--output-dir", str(output_dir),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                MODULE, "run_download", side_effect=fake_download
            ) as first_download, redirect_stdout(StringIO()):
                self.assertEqual(MODULE.main(), 0)
                first_download.assert_called_once()

            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                MODULE, "run_download"
            ) as second_download, redirect_stdout(StringIO()):
                self.assertEqual(MODULE.main(), 0)
                second_download.assert_not_called()

            (output_dir / "ABC_123/clip.mp4").unlink()
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                MODULE, "run_download", side_effect=fake_download
            ) as replacement_download, redirect_stdout(StringIO()):
                self.assertEqual(MODULE.main(), 0)
                replacement_download.assert_called_once()


class SyncInstagramCollectionsTests(unittest.TestCase):
    def test_safe_directory_names_and_duplicate_names(self):
        collections = [
            {"name": " 英语/口语 ", "url": "https://instagram.com/u/saved/_/123456789/"},
            {"name": "英语/口语", "url": "https://instagram.com/u/saved/_/987654321/"},
        ]
        self.assertEqual(
            COLLECTIONS_MODULE.assign_directories(collections),
            ["英语_口语", "英语_口语-87654321"],
        )

    def test_inventory_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "collections.json"
            path.write_text(json.dumps({"collections": [
                {"name": "英语", "url": "https://example.test", "posts": [
                    "https://www.instagram.com/reel/ABC/"
                ]}
            ]}), encoding="utf-8")
            self.assertEqual(COLLECTIONS_MODULE.load_inventory(path)[0]["name"], "英语")


if __name__ == "__main__":
    unittest.main()
