"""Recovery tests for stale URLs, server failures and unsafe range responses."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import browser_session
import browser_sync
import cdp


class Response(io.BytesIO):
    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers


class DownloadSuccessTests(unittest.TestCase):
    def test_wrong_range_offset_restarts_instead_of_corrupting_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'clip.mp4'
            destination.with_suffix('.mp4.part').write_bytes(b'ab')
            responses = [Response(b'abcdef', 206, {'Content-Range': 'bytes 0-5/6'}),
                         Response(b'abcdef', 200, {'Content-Length': '6'})]
            with mock.patch.object(browser_session.urllib.request, 'urlopen', side_effect=responses) as request, mock.patch.object(browser_session.time, 'sleep'):
                size = browser_session.download_url('https://cdn/video.mp4', destination, attempts=2)
            self.assertEqual(size, 6)
            self.assertEqual(destination.read_bytes(), b'abcdef')
            self.assertEqual(request.call_args_list[0].args[0].get_header('Range'), 'bytes=2-')
            self.assertIsNone(request.call_args_list[1].args[0].get_header('Range'))

    def test_expired_cdn_url_is_refreshed_once(self):
        item = {'pk': '123', 'code': 'B7', 'media_type': 2, 'username': 'user',
                'video_url': 'https://cdn/expired.mp4', 'children': []}
        refreshed = dict(item, video_url='https://cdn/fresh.mp4')
        session = mock.Mock()
        session.media_info.return_value = refreshed
        url = browser_session.item_page_url(item)
        downloader = browser_sync.make_session_downloader({url: item}, session=session)
        def transfer(source, destination):
            if source.endswith('expired.mp4'):
                raise browser_session.MediaDownloadError('HTTP 403', 403)
            destination.write_bytes(b'complete video')
            return 14
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(browser_session, 'download_url', side_effect=transfer) as download:
            ok, detail = downloader(Path('unused'), url, Path(tmp), None, None)
            self.assertTrue(ok, detail)
            self.assertEqual(download.call_count, 2)
            session.media_info.assert_called_once_with('123')

    def test_disabled_fallback_does_not_count_as_yt_dlp_download(self):
        counters = {}
        downloader = browser_sync.make_session_downloader({}, allow_ytdlp_fallback=False, stats=counters)
        ok, _ = downloader(Path('unused'), 'https://instagram.com/p/B7/', Path('unused'), None, None)
        self.assertFalse(ok)
        self.assertEqual(counters, {})

    def test_page_timeouts_and_server_errors_are_retried_but_not_not_found(self):
        for error, count in [('Request timed out for /api', 3), ('HTTP 503 for /api', 3), ('HTTP 404 for /api', 1)]:
            with self.subTest(error=error):
                session = browser_session.InstagramSession()
                session.origin_ready = mock.Mock()
                session.evaluate = mock.Mock(side_effect=cdp.CdpError(error))
                with mock.patch.object(browser_session.time, 'sleep'), self.assertRaises(cdp.CdpError):
                    session.media_info('123')
                self.assertEqual(session.evaluate.call_count, count)

    def test_unsatisfiable_range_restarts_the_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'clip.mp4'
            destination.with_suffix('.mp4.part').write_bytes(b'stale partial')
            error = browser_session.urllib.error.HTTPError('https://cdn/video.mp4', 416, 'range', {}, io.BytesIO(b''))
            with mock.patch.object(browser_session.urllib.request, 'urlopen', side_effect=[error, Response(b'video', 200, {'Content-Length': '5'})]), mock.patch.object(browser_session.time, 'sleep'):
                size = browser_session.download_url('https://cdn/video.mp4', destination, attempts=2)
            self.assertEqual(size, 5)
            self.assertEqual(destination.read_bytes(), b'video')

    def test_html_error_page_is_not_saved_as_a_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'clip.mp4'
            response = Response(b'<html>login required</html>', 200, {'Content-Type': 'text/html; charset=utf-8'})
            with mock.patch.object(browser_session.urllib.request, 'urlopen', return_value=response), self.assertRaises(browser_session.MediaDownloadError):
                browser_session.download_url('https://cdn/video.mp4', destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix('.mp4.part').exists())
