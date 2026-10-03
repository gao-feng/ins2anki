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


SCRIPTS = Path(__file__).parents[1] / "instagram-to-anki/scripts"


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

    def test_clean_removes_the_parked_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instagram-saved"
            self.seed(root)
            args = argparse.Namespace(output_root=root, forget=True, clean=False)
            out = StringIO()
            with with_fake_probe(), redirect_stdout(out):
                BROWSER_SYNC.command_repair(args)
            self.assertTrue((root / "自然" / "Dvp9.unplayable").is_dir())
            clean_args = argparse.Namespace(output_root=root, clean=True)
            out = StringIO()
            with redirect_stdout(out):
                BROWSER_SYNC.command_repair(clean_args)
            self.assertEqual(json.loads(out.getvalue())["removed"], 2)
            self.assertFalse((root / "自然" / "Dvp9.unplayable").exists())
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
