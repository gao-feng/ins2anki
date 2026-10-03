"""Saved items are named after their title, not after their 24-hex id.

The downloaders keep writing ``<item id>_<title>.<n>.<ext>`` into
``<output>/<item id>``; the shared engine renames both once the title is known,
which is what makes a collection browsable.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path


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
DOWNLOAD_MEDIA = load("download_media")
SYNC_COMMON = load("sync_common")
RETITLE = load("retitle_items")


NOTE_ID = "6533ae30000000002402f0ad"
TITLE = "显微镜视野如何调节为一个视野"


def write_item(
    parent: Path,
    names: tuple[str, ...],
    note_id: str = NOTE_ID,
    title: str = TITLE,
    description: str = "",
) -> Path:
    """Create a downloaded item exactly as a downloader leaves it."""
    directory = parent / note_id
    directory.mkdir(parents=True)
    media = []
    info = ""
    for name in names:
        path = directory / name
        path.write_bytes(b"data")
        if name.endswith(".info.json"):
            info = str(path)
        elif not name.endswith(".description"):
            media.append(str(path))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "source_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "platform": "xiaohongshu",
                "kind": "images",
                "media": media,
                "metadata": [
                    {
                        "id": note_id,
                        "title": title,
                        "description": description,
                        "info_json": info,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


class SafeTitleTest(unittest.TestCase):
    def test_a_long_chinese_title_is_cut_by_bytes(self):
        name = SYNC_COMMON.safe_title("中" * 100)
        self.assertLessEqual(len(name.encode("utf-8")), 120)
        self.assertEqual(name, "中" * 40)
        self.assertEqual(name, name.rstrip())  # no broken character left behind

    def test_path_separators_and_reserved_characters_are_replaced(self):
        self.assertEqual(
            SYNC_COMMON.safe_title('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i"
        )

    def test_whitespace_and_leading_dots_are_cleaned_up(self):
        self.assertEqual(SYNC_COMMON.safe_title("  ...笔记   标题...  "), "笔记 标题")

    def test_an_empty_title_stays_empty(self):
        self.assertEqual(SYNC_COMMON.safe_title("   "), "")

    def test_a_long_title_is_cut_at_a_word_boundary(self):
        title = "prefix " + "很长的标题 " * 30
        name = SYNC_COMMON.safe_title(title)
        self.assertLessEqual(len(name.encode("utf-8")), 120)
        self.assertFalse(name.endswith(" "))

    def test_the_video_by_placeholder_is_not_a_title(self):
        self.assertTrue(SYNC_COMMON.PLACEHOLDER_TITLE.match("Video by demo_user"))
        self.assertIsNone(SYNC_COMMON.PLACEHOLDER_TITLE.match("Video by yourself 教程"))
        self.assertIsNone(SYNC_COMMON.PLACEHOLDER_TITLE.match("真正的标题"))


class SplitNameTest(unittest.TestCase):
    def test_a_dotted_image_number_is_an_index(self):
        self.assertEqual(SYNC_COMMON.split_download_name("note.0.jpg"), ("0", ".jpg"))

    def test_an_underscored_image_number_is_an_index(self):
        self.assertEqual(SYNC_COMMON.split_download_name("id_作者_12.jpg"), ("12", ".jpg"))

    def test_the_info_sidecar_keeps_its_compound_suffix(self):
        self.assertEqual(
            SYNC_COMMON.split_download_name("id_title.info.json"), ("", ".info.json")
        )
        self.assertEqual(
            SYNC_COMMON.split_download_name("id_title.description"), ("", ".description")
        )

    def test_a_video_name_has_no_index(self):
        self.assertEqual(
            SYNC_COMMON.split_download_name("DAbc123_demo_user.mp4"), ("", ".mp4")
        )


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, *names):
        for name in names:
            (self.dir / name).write_bytes(b"data")

    def test_a_gapped_image_sequence_is_renumbered_from_one(self):
        self.touch("id.0.jpg", "id.2.jpg", "id.3.jpg")
        planned = SYNC_COMMON.plan_titles(self.dir, TITLE)
        self.assertEqual(
            sorted(planned.values()),
            [f"{TITLE}.1.jpg", f"{TITLE}.2.jpg", f"{TITLE}.3.jpg"],
        )

    def test_an_already_consecutive_sequence_keeps_its_numbers(self):
        self.touch("id.1.jpg", "id.2.jpg")
        planned = SYNC_COMMON.plan_titles(self.dir, TITLE)
        self.assertEqual(
            sorted(planned.values()), [f"{TITLE}.1.jpg", f"{TITLE}.2.jpg"]
        )

    def test_a_video_and_its_sidecars_all_take_the_title(self):
        self.touch("id_t.mp4", "id_t.jpg", "id_t.info.json", "id_t.description")
        planned = SYNC_COMMON.plan_titles(self.dir, TITLE)
        self.assertEqual(
            sorted(planned.values()),
            [
                f"{TITLE}.description",
                f"{TITLE}.info.json",
                f"{TITLE}.jpg",
                f"{TITLE}.mp4",
            ],
        )

    def test_the_manifest_is_never_renamed(self):
        self.touch("id_t.mp4")
        (self.dir / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertNotIn("manifest.json", SYNC_COMMON.plan_titles(self.dir, TITLE))

    def test_a_double_extension_collision_gets_a_counter(self):
        self.touch("a.jpg", "b.jpg")
        planned = SYNC_COMMON.plan_titles(self.dir, TITLE)
        self.assertEqual(sorted(planned.values()), [f"{TITLE}-2.jpg", f"{TITLE}.jpg"])


class RetitleItemTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_files_and_directory_take_the_title(self):
        item = write_item(
            self.root,
            (f"{NOTE_ID}_{TITLE}.0.jpg", f"{NOTE_ID}_{TITLE}.2.jpg", f"{NOTE_ID}_{TITLE}.info.json"),
        )
        target = SYNC_COMMON.retitle_item(item)

        self.assertEqual(target.name, TITLE)
        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            sorted(
                [
                    f"{TITLE}.1.jpg",
                    f"{TITLE}.2.jpg",
                    f"{TITLE}.info.json",
                    "manifest.json",
                ]
            ),
        )
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [Path(path).name for path in manifest["media"]],
            [f"{TITLE}.1.jpg", f"{TITLE}.2.jpg"],
        )
        self.assertEqual(
            manifest["metadata"][0]["info_json"], str(target / f"{TITLE}.info.json")
        )

    def test_running_twice_changes_nothing(self):
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        first = SYNC_COMMON.retitle_item(item)
        before = sorted(path.name for path in first.iterdir())
        second = SYNC_COMMON.retitle_item(first)
        self.assertEqual(second, first)
        self.assertEqual(sorted(path.name for path in second.iterdir()), before)

    def test_a_retitled_manifest_is_marked_as_title_named(self):
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        target = SYNC_COMMON.retitle_item(item)
        self.assertTrue(SYNC_COMMON.is_title_named(target))

    def test_a_downloader_manifest_is_not_marked(self):
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",), title="")
        self.assertFalse(SYNC_COMMON.is_title_named(item))

    def test_a_tree_renamed_before_the_marker_is_stamped_on_the_next_pass(self):
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        target = SYNC_COMMON.retitle_item(item)
        manifest = target / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.pop("file_names")
        # the files are already titled, so the guard short-circuits: only the
        # marker has to be written back
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = SYNC_COMMON.retitle_item(target)
        self.assertEqual(again, target)
        self.assertTrue(SYNC_COMMON.is_title_named(target))

    def test_an_item_without_a_title_is_left_alone(self):
        item = write_item(self.root, ("note.jpg",), title="")
        self.assertEqual(SYNC_COMMON.retitle_item(item), item)
        self.assertTrue((item / "note.jpg").is_file())

    def test_the_instagram_placeholder_keeps_the_shortcode_directory(self):
        item = write_item(self.root, ("DG0R_3gRE5T_demo_user.mp4",), title="Video by demo_user")
        self.assertEqual(SYNC_COMMON.retitle_item(item), item)
        self.assertTrue((item / "DG0R_3gRE5T_demo_user.mp4").is_file())

    def test_an_untitled_note_is_named_after_its_first_description_line(self):
        # yt-dlp reports "XiaoHongShu video #<id>" for a note with no title
        # field; the feed shows the first line of the description instead.
        junk = f"XiaoHongShu video #{NOTE_ID}"
        item = write_item(
            self.root,
            (f"{NOTE_ID}_{junk}.0.jpg",),
            title=junk,
            description="今天自己做的普通胃镜，全程很顺利。\n\n后半段正文",
        )
        target = SYNC_COMMON.retitle_item(item)
        # NFKC narrows the full-width comma, as it does for every other name
        self.assertEqual(target.name, "今天自己做的普通胃镜,全程很顺利。")
        self.assertTrue((target / "今天自己做的普通胃镜,全程很顺利。.1.jpg").is_file())

    def test_an_untitled_note_without_a_description_is_left_alone(self):
        junk = f"XiaoHongShu video #{NOTE_ID}"
        item = write_item(self.root, (f"{NOTE_ID}_{junk}.0.jpg",), title=junk)
        self.assertEqual(SYNC_COMMON.retitle_item(item), item)

    def test_the_instagram_placeholder_ignores_the_description(self):
        # Instagram keeps its shortcode even when a caption exists, so the
        # tree stays consistent instead of mixing two naming schemes.
        item = write_item(
            self.root,
            ("DG0R_3gRE5T_demo_user.mp4",),
            title="Video by demo_user",
            description="a caption",
        )
        self.assertEqual(SYNC_COMMON.retitle_item(item), item)

    def test_a_duplicate_title_gets_a_numbered_directory(self):
        first = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        other_id = "694e67a50000000022032e61"
        other = write_item(
            self.root, (f"{other_id}_{TITLE}.0.jpg",), note_id=other_id
        )
        SYNC_COMMON.retitle_item(first)
        target = SYNC_COMMON.retitle_item(other)
        self.assertEqual(target.name, f"{TITLE} (2)")

    def test_parallel_items_with_one_title_never_share_a_directory(self):
        # sync_items downloads on several threads, so a name has to be claimed
        # and taken under one lock.
        other_id = "694e67a50000000022032e61"
        first = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        second = write_item(
            self.root, (f"{other_id}_{TITLE}.0.jpg",), note_id=other_id
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(SYNC_COMMON.retitle_item, item) for item in (first, second)
            ]
            results = [future.result() for future in futures]
        self.assertEqual(
            sorted(path.name for path in results), [TITLE, f"{TITLE} (2)"]
        )
        self.assertTrue(all(path.is_dir() for path in results))

    def test_dry_run_reports_the_target_without_touching_anything(self):
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        target = SYNC_COMMON.retitle_item(item, dry_run=True)
        self.assertEqual(target.name, TITLE)
        self.assertTrue(item.is_dir())
        self.assertTrue((item / f"{NOTE_ID}_{TITLE}.0.jpg").is_file())

    def test_a_title_full_of_separators_still_lands_on_disk(self):
        title = "笔记/标题:带*号?" * 20
        item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",), title=title)
        target = SYNC_COMMON.retitle_item(item)
        self.assertTrue(target.is_dir())
        self.assertLessEqual(len(target.name.encode("utf-8")), 255)
        self.assertNotIn("/", target.name)


class SyncItemsRetitleTest(unittest.TestCase):
    """The shared engine must store the renamed path in its state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_state_points_at_the_title_directory_and_the_item_is_not_redone(self):
        calls: list[str] = []

        def fake_download(_downloader, url, destination, _cookies, _browser):
            calls.append(url)
            media = destination / f"{NOTE_ID}_{TITLE}.0.jpg"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"image")
            (destination / "manifest.json").write_text(
                json.dumps(
                    {
                        "source_url": url,
                        "platform": "xiaohongshu",
                        "kind": "images",
                        "media": [str(media)],
                        "metadata": [{"id": NOTE_ID, "title": TITLE}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return True, "ok"

        url = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        discovered = [("xiaohongshu", NOTE_ID, url)]
        state_file = self.root / "sync-state.json"

        code = SYNC_COMMON.sync_items(
            discovered,
            output_dir=self.root,
            state_file=state_file,
            downloader=Path("downloader.py"),
            download_fn=fake_download,
            source_label="test",
            stream=StringIO(),
        )
        self.assertEqual(code, 0)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            state["items"][NOTE_ID]["output_dir"],
            str(self.root.resolve() / TITLE),
        )
        self.assertTrue((self.root / TITLE / f"{TITLE}.1.jpg").is_file())

        with redirect_stderr(StringIO()):
            SYNC_COMMON.sync_items(
                discovered,
                output_dir=self.root,
                state_file=state_file,
                downloader=Path("downloader.py"),
                download_fn=fake_download,
                source_label="test",
                stream=StringIO(),
            )
        self.assertEqual(len(calls), 1)


class RetitleTreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "收藏"
        self.root.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self.item = write_item(self.root, (f"{NOTE_ID}_{TITLE}.0.jpg",))
        other_id = "694e67a50000000022032e61"
        self.untitled = write_item(
            self.root, (f"{other_id}_note.jpg",), note_id=other_id, title=""
        )
        self.state_file = self.root / "sync-state.json"
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": {
                        NOTE_ID: {
                            "url": f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
                            "status": "completed",
                            # the path from before the checkout was renamed
                            "output_dir": f"/Users/someone/old-repo/收藏/{NOTE_ID}",
                        },
                        "694e67a50000000022032e61": {
                            "url": "https://www.xiaohongshu.com/explore/694e67a5",
                            "status": "completed",
                            "output_dir": str(self.untitled),
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_dry_run_lists_the_rename_and_writes_nothing(self):
        summary = RETITLE.retitle_tree(self.root, dry_run=True)
        self.assertEqual(summary["items_scanned"], 2)
        self.assertEqual(summary["items_renamed"], 1)
        self.assertEqual(summary["items_untouched"], 1)
        self.assertEqual(summary["states_updated"], 0)
        self.assertTrue(self.item.is_dir())
        self.assertIn(f"old-repo/收藏/{NOTE_ID}", self.state_file.read_text(encoding="utf-8"))

    def test_the_tree_is_renamed_and_the_state_follows(self):
        summary = RETITLE.retitle_tree(self.root)
        self.assertEqual(summary["items_renamed"], 1)
        self.assertEqual(summary["states_updated"], 1)
        self.assertFalse(self.item.exists())
        self.assertTrue((self.root / TITLE / f"{TITLE}.1.jpg").is_file())
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            state["items"][NOTE_ID]["output_dir"], str(self.root / TITLE)
        )
        # the untitled item keeps its id directory and its state entry
        self.assertEqual(
            state["items"]["694e67a50000000022032e61"]["output_dir"],
            str(self.untitled),
        )

    def test_a_second_pass_finds_nothing_to_rename(self):
        RETITLE.retitle_tree(self.root)
        summary = RETITLE.retitle_tree(self.root)
        self.assertEqual(summary["items_renamed"], 0)
        self.assertEqual(summary["items_untouched"], 2)

    def test_the_cli_prints_a_json_summary(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = RETITLE.main(["--root", str(self.root), "--dry-run", "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["dry_run"])

    def test_a_missing_root_is_reported(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = RETITLE.main(["--root", "/nope/definitely-missing"])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err.getvalue())


if __name__ == "__main__":
    unittest.main()
