"""Keep the full-size Xiaohongshu image and drop its preview twin.

The payloads below are trimmed copies of real ``*.info.json`` entries: every
note image is listed twice, once as ``!nd_dft_...`` (full) and once as
``!nd_prv_...`` (preview), with identical ``width``/``height``. That is why the
variant suffix — not the resolution, and not the file size — is what decides.
"""

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
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MEDIA = load("download_media")
CLEAN = load("clean_images")


def thumbnail(tid: str, token: str, variant: str, width: int = 1620, height: int = 2160) -> dict:
    return {
        "id": tid,
        "url": f"http://sns-webpic-qc.xhscdn.com/202610031542/deadbeef/{token}!{variant}_jpg_3",
        "width": width,
        "height": height,
    }


#: A = preview then full, B = full then preview, C = only the full one was saved.
PAIR_PAYLOAD = [
    thumbnail("0", "imageA", "nd_prv_wlteh"),
    thumbnail("1", "imageA", "nd_dft_wlteh"),
    thumbnail("2", "imageB", "nd_dft_wlteh"),
    thumbnail("3", "imageB", "nd_prv_wlteh"),
    thumbnail("4", "imageC", "nd_dft_wlteh"),
]


class ImageIdentityTest(unittest.TestCase):
    def test_identity_is_the_token_before_the_variant_suffix(self):
        url = "http://cdn/202610031542/abc123/spectrum/01029c01kv!nd_dft_wlteh_jpg_3"
        self.assertEqual(MEDIA.image_identity(url), "01029c01kv")

    def test_identity_ignores_the_cdn_directory(self):
        first = "http://cdn/202610031542/13b9b1fe/token9!nd_dft_wlteh_jpg_3"
        second = "http://cdn/202610031542/f4bb965f/token9!nd_prv_wlteh_jpg_3"
        self.assertEqual(MEDIA.image_identity(first), MEDIA.image_identity(second))

    def test_empty_url_has_no_identity(self):
        self.assertEqual(MEDIA.image_identity(""), "")

    def test_full_size_outranks_every_preview(self):
        ranks = [
            MEDIA.variant_rank(f"http://cdn/x/token!{name}_jpg_3")
            for name in ("nd_dft_wlteh", "nd_dft_wgth", "nd_dft", "nd_prv_wlteh", "nd_prv_wgth")
        ]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(ranks[0], ranks[-1])

    def test_an_unknown_variant_loses_to_a_known_full_one(self):
        self.assertLess(
            MEDIA.variant_rank("http://cdn/x/token!nd_dft_wlteh_jpg_3"),
            MEDIA.variant_rank("http://cdn/x/token!nd_zzz_wlteh_jpg_3"),
        )


class PartitionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, index: str, size: int) -> Path:
        path = self.dir / f"note.{index}.jpg"
        path.write_bytes(b"x" * size)
        return path

    def test_previews_are_the_lower_quality_twin_of_a_kept_image(self):
        for index in range(5):
            self.write(str(index), 100 if index % 2 == 0 else 500)
        keep, previews = MEDIA.partition_image_files(self.dir, PAIR_PAYLOAD)
        self.assertEqual([p.name for p in keep], ["note.1.jpg", "note.2.jpg", "note.4.jpg"])
        self.assertEqual([p.name for p in previews], ["note.0.jpg", "note.3.jpg"])

    def test_a_lone_image_is_never_dropped(self):
        payload = [thumbnail("7", "imageD", "nd_prv_wlteh")]
        self.write("7", 100)
        keep, previews = MEDIA.partition_image_files(self.dir, payload)
        self.assertEqual([p.name for p in keep], ["note.7.jpg"])
        self.assertEqual(previews, [])

    def test_a_missing_original_leaves_the_preview_alone(self):
        self.write("0", 100)  # the full-size file never arrived
        payload = [thumbnail("0", "imageA", "nd_prv_wlteh"), thumbnail("1", "imageA", "nd_dft_wlteh")]
        keep, previews = MEDIA.partition_image_files(self.dir, payload)
        self.assertEqual(keep, [])
        self.assertEqual(previews, [])

    def test_three_variants_of_one_image_collapse_to_the_best(self):
        payload = [
            thumbnail("0", "imageE", "nd_prv_wlteh"),
            thumbnail("1", "imageE", "nd_dft_wlteh"),
            thumbnail("2", "imageE", "nd_dft_wlteh"),
        ]
        for index in range(3):
            self.write(str(index), 100)
        keep, previews = MEDIA.partition_image_files(self.dir, payload)
        self.assertEqual([p.name for p in keep], ["note.1.jpg"])
        self.assertEqual([p.name for p in previews], ["note.0.jpg", "note.2.jpg"])

    def test_entries_without_an_id_or_url_are_ignored(self):
        payload = [{"width": 100, "height": 100}, {"id": "3", "url": ""}]
        self.write("3", 100)
        keep, previews = MEDIA.partition_image_files(self.dir, payload)
        self.assertEqual(keep, [])
        self.assertEqual(previews, [])

    def test_drop_unlinks_the_previews(self):
        for index in range(5):
            self.write(str(index), 100)
        removed = MEDIA.drop_image_previews(self.dir, PAIR_PAYLOAD)
        self.assertEqual(sorted(p.name for p in removed), ["note.0.jpg", "note.3.jpg"])
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["note.1.jpg", "note.2.jpg", "note.4.jpg"],
        )


class DownloaderWiringTest(unittest.TestCase):
    """``run()`` must prune before it writes the manifest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_the_manifest_lists_only_full_size_images(self):
        payload = {"formats": [], "thumbnails": PAIR_PAYLOAD}

        def fake_download(_exe, _url, out, _cookies, _browser):
            for index in range(5):
                (out / f"note.{index}.jpg").write_bytes(b"x" * 100)
            return 0, ""

        argv = [
            "https://www.xiaohongshu.com/explore/6533ae30000000002402f0ad?xsec_token=T&xsec_source=pc_user",
            "--output-dir",
            str(self.out),
            "--platform",
            "xiaohongshu",
        ]
        with mock.patch.object(MEDIA.shutil, "which", return_value="/usr/bin/yt-dlp"), \
                mock.patch.object(MEDIA, "probe", return_value=(payload, "")), \
                mock.patch.object(MEDIA, "download_images", side_effect=fake_download), \
                redirect_stderr(StringIO()):
            code = MEDIA.run(argv)

        self.assertEqual(code, 0)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [Path(p).name for p in manifest["media"]],
            ["note.1.jpg", "note.2.jpg", "note.4.jpg"],
        )
        self.assertEqual(manifest["kind"], "images")


class CleanImagesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "xhs-saved"
        self.item = self.root / "收藏" / "6533ae30"
        self.item.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        (self.item / "6533ae30_显微镜.info.json").write_text(
            json.dumps({"id": "6533ae30", "thumbnails": PAIR_PAYLOAD}), encoding="utf-8"
        )
        for index in range(5):
            (self.item / f"6533ae30_显微镜.{index}.jpg").write_bytes(b"x" * (100 + index))
        # a manifest from before the checkout moved: stale absolute paths
        (self.item / "manifest.json").write_text(
            json.dumps(
                {
                    "source_url": "https://www.xiaohongshu.com/explore/6533ae30",
                    "platform": "xiaohongshu",
                    "kind": "images",
                    "media": [f"/Users/someone/old-repo/{self.item.name}.{i}.jpg" for i in range(5)],
                    "metadata": [],
                }
            ),
            encoding="utf-8",
        )
        # a second item that has nothing to clean
        self.clean_item = self.root / "收藏" / "ffffffff"
        self.clean_item.mkdir()
        (self.clean_item / "ffffffff_x.info.json").write_text(
            json.dumps({"thumbnails": [thumbnail("0", "imageF", "nd_dft_wlteh")]}),
            encoding="utf-8",
        )
        (self.clean_item / "ffffffff_x.0.jpg").write_bytes(b"x" * 100)
        (self.clean_item / "manifest.json").write_text(
            json.dumps({"kind": "images", "media": [str(self.clean_item / "ffffffff_x.0.jpg")]}),
            encoding="utf-8",
        )
        # noise that must never be walked
        (self.item / "_previews").mkdir()
        (self.item / "_previews" / "already.0.jpg").write_bytes(b"x")
        hidden = self.root / ".stversions" / "old"
        hidden.mkdir(parents=True)
        (hidden / "old.info.json").write_text("{}", encoding="utf-8")

    def test_dry_run_touches_nothing(self):
        summary = CLEAN.clean(self.root, dry_run=True)
        self.assertEqual(summary["items_scanned"], 2)
        self.assertEqual(summary["items_changed"], 1)
        self.assertEqual(summary["files_removed"], 2)
        self.assertEqual(summary["bytes_removed"], 100 + 103)
        self.assertEqual(summary["manifests_rewritten"], 0)
        self.assertTrue((self.item / "6533ae30_显微镜.0.jpg").is_file())

    def test_quarantine_moves_the_previews_and_fixes_the_manifest(self):
        summary = CLEAN.clean(self.root)
        self.assertEqual(summary["files_removed"], 2)
        self.assertEqual(summary["manifests_rewritten"], 1)
        self.assertEqual(
            sorted(p.name for p in (self.item / CLEAN.QUARANTINE_DIR).iterdir()),
            ["6533ae30_显微镜.0.jpg", "6533ae30_显微镜.3.jpg", "already.0.jpg"],
        )
        manifest = json.loads((self.item / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [Path(p).name for p in manifest["media"]],
            ["6533ae30_显微镜.1.jpg", "6533ae30_显微镜.2.jpg", "6533ae30_显微镜.4.jpg"],
        )
        self.assertTrue(all(Path(p).is_file() for p in manifest["media"]))
        self.assertEqual(manifest["platform"], "xiaohongshu")

    def test_delete_removes_them_without_a_quarantine_dir(self):
        summary = CLEAN.clean(self.root, delete=True)
        self.assertEqual(summary["action"], "delete")
        self.assertEqual(summary["files_removed"], 2)
        self.assertEqual(
            sorted(p.name for p in (self.item / CLEAN.QUARANTINE_DIR).iterdir()),
            ["already.0.jpg"],
        )
        self.assertFalse((self.item / "6533ae30_显微镜.0.jpg").exists())

    def test_second_run_finds_nothing_left(self):
        CLEAN.clean(self.root)
        summary = CLEAN.clean(self.root)
        self.assertEqual(summary["files_removed"], 0)
        self.assertEqual(summary["items_changed"], 0)

    def test_an_item_renamed_after_its_title_is_left_alone(self):
        # Retitling renumbers the images, so the numbers are no longer the
        # thumbnail ids this pairing needs; guessing would delete a real image.
        (self.item / "manifest.json").write_text(
            json.dumps(
                {
                    "kind": "images",
                    "file_names": "title",
                    "media": [str(self.item / "显微镜.1.jpg")],
                }
            ),
            encoding="utf-8",
        )
        for index in range(5):
            (self.item / f"显微镜.{index + 1}.jpg").write_bytes(b"x" * 100)
        summary = CLEAN.clean(self.root, delete=True)
        self.assertEqual(summary["items_skipped_title_named"], 1)
        self.assertEqual(summary["files_removed"], 0)
        self.assertEqual(len(list(self.item.glob("显微镜.*.jpg"))), 5)

    def test_an_item_without_info_json_is_left_alone(self):
        lonely = self.root / "收藏" / "no-info"
        lonely.mkdir()
        (lonely / "a.jpg").write_bytes(b"x")
        CLEAN.clean(self.root)
        self.assertTrue((lonely / "a.jpg").is_file())

    def test_a_stale_manifest_is_repointed_without_dropping_anything(self):
        (self.clean_item / "manifest.json").write_text(
            json.dumps(
                {
                    "kind": "images",
                    "media": [f"/Users/someone/old-repo/{self.clean_item.name}.0.jpg"],
                }
            ),
            encoding="utf-8",
        )
        summary = CLEAN.clean(self.root)
        self.assertEqual(summary["files_removed"], 2)  # only the first item's
        self.assertEqual(summary["failures"], [])
        manifest = json.loads((self.clean_item / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [Path(p).name for p in manifest["media"]], [f"{self.clean_item.name}_x.0.jpg"]
        )

    def test_an_unchanged_manifest_is_not_rewritten(self):
        before = (self.clean_item / "manifest.json").stat().st_mtime_ns
        CLEAN.clean(self.root)
        self.assertEqual((self.clean_item / "manifest.json").stat().st_mtime_ns, before)


class CleanCliTest(unittest.TestCase):
    def test_missing_root_is_reported(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = CLEAN.main(["--root", "/nope/definitely-missing"])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err.getvalue())

    def test_summary_is_json_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = CLEAN.main(["--root", tmp, "--dry-run", "--quiet"])
            self.assertEqual(code, 0)
            summary = json.loads(out.getvalue())
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["items_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
