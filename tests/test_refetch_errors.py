import sys
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import browser_session
import browser_sync
import cdp


class RefetchErrorsTests(unittest.TestCase):
    def session(self):
        session = browser_session.InstagramSession()
        session.origin_ready = mock.Mock(return_value=True)
        return session

    def test_transient_fetch_failure_is_retried(self):
        session = self.session()
        session.evaluate = mock.Mock(side_effect=[cdp.CdpError('TypeError: Failed to fetch'), {'pk': '123', 'video_url': 'https://cdn/video.mp4'}])
        with mock.patch.object(browser_session.time, 'sleep'):
            result = session.media_info('123')
        self.assertEqual(result['pk'], '123')
        self.assertEqual(session.evaluate.call_count, 2)

    def test_persistent_fetch_failure_has_bounded_retries(self):
        session = self.session()
        session.evaluate = mock.Mock(side_effect=cdp.CdpError('TypeError: Failed to fetch'))
        with mock.patch.object(browser_session.time, 'sleep'), self.assertRaises(cdp.CdpError):
            session.media_info('123')
        self.assertEqual(session.evaluate.call_count, 3)

    def test_http_rate_limit_is_not_retried(self):
        session = self.session()
        session.evaluate = mock.Mock(side_effect=cdp.CdpError('HTTP 429 for /api/v1/media/123/info/'))
        with self.assertRaises(browser_session.RateLimitedError):
            session.media_info('123')
        self.assertEqual(session.evaluate.call_count, 1)

    def test_refetch_does_not_label_network_errors_as_quota_exhaustion(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.media_info.side_effect = cdp.CdpError('TypeError: Failed to fetch')
        err = StringIO()
        with mock.patch.object(browser_sync, '_parked_backlog', return_value=({'test': [{'pk': '123'}]}, [])), mock.patch.object(browser_sync, 'open_session', return_value=session), redirect_stderr(err):
            code = browser_sync._refetch(SimpleNamespace(output_root='unused'))
        self.assertEqual(code, 1)
        self.assertIn('does not confirm rate limiting', err.getvalue())
        self.assertNotIn('wait for the quota', err.getvalue())


class ParkedBacklogTests(unittest.TestCase):
    def test_shortcodes_are_converted_and_multiple_backups_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for suffix, identity in (('.unplayable', '123'), ('.unplayable2', 'B7')):
                directory = root / 'collection' / ('B7' + suffix)
                directory.mkdir(parents=True)
                (directory / 'manifest.json').write_text(json.dumps({
                    'metadata': [{'id': identity, 'shortcode': 'B7', 'media_type': 2}]
                }))
            groups, skipped = browser_sync._parked_backlog(root)
            self.assertEqual(skipped, [])
            self.assertEqual(len(groups['collection']), 1)
            self.assertEqual(groups['collection'][0]['pk'], '123')

    def test_yt_dlp_short_id_and_compound_media_id(self):
        for identity in ('B7', '123_987', ''):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / 'collection' / 'B7.unplayable'
                directory.mkdir(parents=True)
                (directory / 'manifest.json').write_text(json.dumps({
                    'metadata': [{'id': identity, 'shortcode': 'B7'}]
                }))
                groups, skipped = browser_sync._parked_backlog(Path(tmp))
                self.assertEqual(skipped, [])
                self.assertEqual(groups['collection'][0]['pk'], '123')

    def test_corrupt_backups_are_skipped_without_losing_healthy_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, payload in [('bad', []), ('broken', {'metadata': [None]}), ('B7', {'metadata': [{'id': '123', 'shortcode': 'B7'}]})]:
                directory = root / 'collection' / (name + '.unplayable')
                directory.mkdir(parents=True)
                (directory / 'manifest.json').write_text(json.dumps(payload))
            groups, skipped = browser_sync._parked_backlog(root)
            self.assertEqual(len(skipped), 2)
            self.assertEqual(len(groups['collection']), 1)
