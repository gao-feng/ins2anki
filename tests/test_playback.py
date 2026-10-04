"""QuickTime playability: AVFoundation decodes H.264/HEVC, not VP9 or AV1.

Instagram's own progressive ``video_versions`` are H.264, but anything that went
through yt-dlp (its default preference is often VP9) lands as a perfectly valid
mp4 that QuickTime Player, Photos and Quick Look simply refuse to open. These
tests pin the two defences: the yt-dlp format selector, and the ``repair``
command that finds and forgets such files so the session path re-fetches them.
"""

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
DOWNLOAD = load("download_media")
SYNC_COMMON = load("sync_common")
BROWSER_SYNC = load("browser_sync")


class PartialFileTest(unittest.TestCase):
    """Interrupted runs leave *.part behind; they must never be resumed blindly."""

    def test_find_partials_covers_the_shapes_yt_dlp_leaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            (root / "fun" / "DX4a7cvOM4J").mkdir(parents=True)
            (root / "fun" / "DX4a7cvOM4J" / "DX4a7cvOM4J_Video.fdash-999v.mp4.part").write_bytes(b"x" * 10)
            (root / "fun" / "DYRCqO1mca2").mkdir(parents=True)
            (root / "fun" / "DYRCqO1mca2" / "clip.mp4.ytdl").write_bytes(b"y" * 5)
            (root / "fun" / "DYRCqO1mca2" / "clip.mp4").write_bytes(b"z" * 5)
            found = BROWSER_SYNC.find_partials(root)
        self.assertEqual(
            sorted(path.name for path in found),
            ["DX4a7cvOM4J_Video.fdash-999v.mp4.part", "clip.mp4.ytdl"],
        )

    def test_repair_parts_reports_then_deletes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            item_dir = root / "英语" / "DAbc123"
            item_dir.mkdir(parents=True)
            partial = item_dir / "DAbc123.mp4.part"
            partial.write_bytes(b"x" * 2048)
            (root / "英语" / "sync-state.json").write_text(
                json.dumps({"source": "https://www.instagram.com/demo_user/saved/_/1/", "items": {}}),
                encoding="utf-8",
            )

            report = StringIO()
            with redirect_stdout(report):
                self.assertEqual(
                    BROWSER_SYNC.command_repair(argparse.Namespace(output_root=root, forget=False)), 0
                )
            payload = json.loads(report.getvalue())
            self.assertEqual(payload["partial_files"], 1)
            self.assertEqual(payload["partial_mib"], 0.0)
            self.assertTrue(partial.is_file())

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    BROWSER_SYNC.command_repair(
                        argparse.Namespace(output_root=root, forget=False, parts=True)
                    ),
                    0,
                )
            self.assertFalse(partial.exists())
            self.assertTrue(item_dir.is_dir())


class YtDlpFormatTest(unittest.TestCase):
    def test_selector_prefers_h264_then_mp4(self):
        selector = DOWNLOAD.AVC_FIRST_FORMAT
        self.assertIn("vcodec^=avc1", selector)
        self.assertIn("acodec^=mp4a", selector)
        self.assertTrue(selector.endswith("/b"))
        # H.264 must be tried before the generic fallbacks
        self.assertLess(selector.index("avc1"), selector.index("ext=mp4"))

    def test_download_video_asks_for_avc_and_mp4_merge(self):
        with mock.patch.object(
            DOWNLOAD.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
        ) as run:
            code, _detail = DOWNLOAD.download_video(
                "/usr/bin/yt-dlp", "https://www.instagram.com/p/DAbc123/", Path("/tmp/out"), None, None
            )
        self.assertEqual(code, 0)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--format") + 1], DOWNLOAD.AVC_FIRST_FORMAT)
        self.assertEqual(cmd[cmd.index("--merge-output-format") + 1], "mp4")


def by_name(path: Path) -> str:
    """Stand-in for ffprobe: the fixture encodes the codec in the filename."""
    return "vp9" if "vp9" in path.name else "h264"


def with_fake_probe():
    """Patch ``find_unplayable`` to use the filename probe (no ffprobe needed)."""
    original = BROWSER_SYNC.find_unplayable
    return mock.patch.object(
        BROWSER_SYNC, "find_unplayable", side_effect=lambda root: original(root, probe=by_name)
    )


class RepairTest(unittest.TestCase):
    def seed(self, root: Path) -> None:
        for collection, items in (
            ("自然", [("Dvp9", "vp9"), ("Dh264", "h264"), ("Dpending", "vp9")]),
            ("英语", [("Dalso-vp9", "vp9")]),
        ):
            state_file = root / collection / "sync-state.json"
            state_file.parent.mkdir(parents=True)
            payload = {"source": f"https://www.instagram.com/demo_user/saved/_/{collection}/", "items": {}}
            for item_id, codec in items:
                directory = state_file.parent / item_id
                directory.mkdir()
                (directory / f"{item_id}_{codec}.mp4").write_bytes(b"\x00" * 32)
                payload["items"][item_id] = {
                    "status": "completed" if item_id != "Dpending" else None,
                    "output_dir": str(directory),
                }
            state_file.write_text(json.dumps(payload), encoding="utf-8")

    def test_reports_only_completed_unplayable_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            findings = BROWSER_SYNC.find_unplayable(root, probe=by_name)
        self.assertEqual(
            {(item["collection"], item["id"], item["codec"]) for item in findings},
            {("英语", "Dalso-vp9", "vp9"), ("自然", "Dvp9", "vp9")},
        )

    def test_report_mode_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            args = argparse.Namespace(output_root=root, forget=False)
            out = StringIO()
            with with_fake_probe():
                with redirect_stdout(out):
                    self.assertEqual(BROWSER_SYNC.command_repair(args), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["unplayable"], 2)
            self.assertEqual(report["by_codec"], {"vp9": 2})
            self.assertEqual(report["items"], ["Dalso-vp9", "Dvp9"])
            self.assertTrue((root / "自然" / "Dvp9" / "Dvp9_vp9.mp4").is_file())

    def test_forget_removes_folders_and_state_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            args = argparse.Namespace(output_root=root, forget=True)
            out = StringIO()
            with with_fake_probe():
                with redirect_stdout(out):
                    self.assertEqual(BROWSER_SYNC.command_repair(args), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["forgotten"], 2)
            # parked, not deleted: the original bytes are still recoverable
            self.assertFalse((root / "自然" / "Dvp9").exists())
            self.assertFalse((root / "英语" / "Dalso-vp9").exists())
            self.assertEqual(
                (root / "自然" / "Dvp9.unplayable" / "Dvp9_vp9.mp4").read_bytes(), b"\x00" * 32
            )
            # the playable item survives untouched
            self.assertTrue((root / "自然" / "Dh264" / "Dh264_h264.mp4").is_file())
            state = json.loads((root / "自然" / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(state["items"]), ["Dh264", "Dpending"])
            self.assertEqual(state["items"]["Dh264"]["status"], "completed")

    def test_covers_mode_finds_reports_and_forgets_cover_only_reels(self):
        """--covers targets reels whose download predates the reel-video fix."""

        def seed(root: Path) -> None:
            state_file = root / "wtf" / "sync-state.json"
            state_file.parent.mkdir(parents=True)
            payload = {"source": "https://www.instagram.com/demo/saved/_/wtf/", "items": {}}
            for item_id, media_type, files, status in (
                ("Dcover", 2, ["Dcover_demo.jpg"], "completed"),
                ("Dfixed", 2, ["Dfixed_demo.jpg", "Dfixed_demo.mp4"], "completed"),
                ("Dphoto", 1, ["Dphoto_demo.jpg"], "completed"),
                ("Dhalf", 2, ["Dhalf_demo.jpg"], None),
            ):
                directory = state_file.parent / item_id
                directory.mkdir()
                for name in files:
                    (directory / name).write_bytes(b"\x00" * 16)
                (directory / "manifest.json").write_text(json.dumps({
                    "metadata": [{"media_type": media_type}],
                }), encoding="utf-8")
                payload["items"][item_id] = {
                    "status": status,
                    "output_dir": str(directory),
                }
            state_file.write_text(json.dumps(payload), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            seed(root)
            # the tree is the source of truth, so the pending Dhalf is found
            # too: its cover JPEG is on disk and no video beside it
            self.assertEqual(
                [(f["collection"], f["id"]) for f in BROWSER_SYNC.find_cover_only(root)],
                [("wtf", "Dcover"), ("wtf", "Dhalf")],
            )
            # report mode changes nothing
            args = argparse.Namespace(output_root=root, covers=True, forget=False)
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(BROWSER_SYNC.command_repair(args), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["cover_only"], 2)
            self.assertEqual(report["items"], ["Dcover", "Dhalf"])
            self.assertTrue((root / "wtf" / "Dcover" / "Dcover_demo.jpg").is_file())
            # forget parks the cover and drops the state entry
            args = argparse.Namespace(output_root=root, covers=True, forget=True)
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(BROWSER_SYNC.command_repair(args), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["forgotten"], 2)
            self.assertFalse((root / "wtf" / "Dcover").exists())
            self.assertFalse((root / "wtf" / "Dhalf").exists())
            self.assertEqual(
                (root / "wtf" / "Dcover.unplayable" / "Dcover_demo.jpg").read_bytes(),
                b"\x00" * 16,
            )
            state = json.loads((root / "wtf" / "sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(state["items"]), ["Dfixed", "Dphoto"])
            # forgotten items sit deep in the collection: the next sync must
            # walk it in full once, so the state asks for exactly that
            self.assertTrue(state["full_walk_pending"])

    def test_clean_keeps_originals_until_a_playable_replacement_exists(self):
        """The destructive step must never outrun the re-download."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            args = argparse.Namespace(output_root=root, forget=True, clean=False)
            out = StringIO()
            with with_fake_probe(), redirect_stdout(out):
                BROWSER_SYNC.command_repair(args)
            backup = root / "自然" / "Dvp9.unplayable"
            self.assertTrue(backup.is_dir())

            # nothing has been re-downloaded yet: the original must survive
            clean_args = argparse.Namespace(output_root=root, clean=True)
            out = StringIO()
            with mock.patch.object(BROWSER_SYNC, "probe_video_codec", by_name), redirect_stdout(out):
                BROWSER_SYNC.command_repair(clean_args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["removed"], 0)
            self.assertEqual(payload["kept"], 2)
            self.assertTrue(backup.is_dir())

            # simulate the refresh: item completed again, now H.264 on disk
            state_file = root / "自然" / "sync-state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            replacement = root / "自然" / "Dvp9"
            replacement.mkdir()
            (replacement / "Drefreshed_h264.mp4").write_bytes(b"\x00" * 16)
            state["items"]["Dvp9"] = {"status": "completed", "output_dir": str(replacement)}
            state_file.write_text(json.dumps(state), encoding="utf-8")

            out = StringIO()
            with mock.patch.object(BROWSER_SYNC, "probe_video_codec", by_name), redirect_stdout(out):
                BROWSER_SYNC.command_repair(clean_args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["removed"], 1)
            self.assertEqual(payload["kept"], 1)  # 英语/Dalso-vp9 was never refreshed
            self.assertFalse(backup.exists())
            self.assertTrue((root / "自然" / "Dh264").is_dir())

    def test_refuses_to_delete_outside_the_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            victim = Path(outside) / "Dvp9"
            victim.mkdir()
            (victim / "Dvp9_vp9.mp4").write_bytes(b"\x00" * 16)
            state_file = root / "自然" / "sync-state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["items"]["Dvp9"]["output_dir"] = str(victim)
            state_file.write_text(json.dumps(state), encoding="utf-8")

            args = argparse.Namespace(output_root=root, forget=True, clean=False)
            out = StringIO()
            with with_fake_probe(), redirect_stdout(out):
                self.assertEqual(BROWSER_SYNC.command_repair(args), 0)
            self.assertTrue(victim.is_dir())
            self.assertFalse((victim.parent / (victim.name + BROWSER_SYNC.BACKUP_SUFFIX)).exists())
            self.assertEqual(json.loads(out.getvalue())["forgotten"], 1)


if __name__ == "__main__":
    unittest.main()
