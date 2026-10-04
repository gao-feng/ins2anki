import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sync_common


class ValidationTests(unittest.TestCase):
    def test_malformed_manifest_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for payload in ([], None, 3, {'media': [None, {}, 42, '', '\x00']}):
                with self.subTest(payload=payload):
                    (root / 'manifest.json').write_text(json.dumps(payload))
                    self.assertFalse(sync_common.valid_download(root))
            (root / 'manifest.json').write_bytes(b'\xff')
            self.assertFalse(sync_common.valid_download(root))

    def test_only_referenced_media_can_validate_a_moved_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'manifest.json').write_text(json.dumps({'media': ['/old/place/clip.mp4']}))
            for name in ('.DS_Store', 'caption.txt', 'unrelated.jpg'):
                (root / name).write_bytes(b'not the missing media')
            self.assertFalse(sync_common.valid_download(root))
            (root / 'clip.mp4').write_bytes(b'media')
            self.assertTrue(sync_common.valid_download(root))
            (root / 'clip.mp4').write_bytes(b'')
            self.assertFalse(sync_common.valid_download(root))

    def test_video_cover_is_not_a_completed_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'cover.jpg').write_bytes(b'cover')
            manifest = {'media': ['cover.jpg'], 'metadata': [{'media_type': 2}]}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            self.assertFalse(sync_common.valid_download(root))
            manifest['metadata'][0]['media_type'] = 1
            (root / 'manifest.json').write_text(json.dumps(manifest))
            self.assertTrue(sync_common.valid_download(root))

    def test_download_exception_does_not_abort_remaining_items(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def download(_script, url, directory, *_args):
                    if url == 'bad':
                        raise OSError('connection lost')
                    directory.mkdir(parents=True)
                    (directory / 'clip.mp4').write_bytes(b'media')
                    (directory / 'manifest.json').write_text(json.dumps({'media': ['clip.mp4']}))
                    return True, ''
                with redirect_stderr(StringIO()):
                    result = sync_common.sync_items(
                        [('instagram', 'bad', 'bad'), ('instagram', 'good', 'good')],
                        root, root / 'state.json', Path('unused'), download, 'test',
                        jobs=jobs, stream=StringIO())
                self.assertEqual(result, 2)
                state = json.loads((root / 'state.json').read_text())['items']
                self.assertEqual(state['bad']['status'], 'failed')
                self.assertEqual(state['good']['status'], 'completed')
