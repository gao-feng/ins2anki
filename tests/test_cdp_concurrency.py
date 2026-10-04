"""A shared CDP socket must never have two readers or unbounded lock waits."""
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cdp


class BlockingSocket:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent = []
        self.receivers = 0
        self.max_receivers = 0
        self.guard = threading.Lock()

    def send_text(self, message):
        with self.guard:
            self.sent.append(json.loads(message))

    def recv_text(self, timeout):
        with self.guard:
            self.receivers += 1
            self.max_receivers = max(self.max_receivers, self.receivers)
            message = self.sent[-1]
        self.entered.set()
        try:
            if not self.release.wait(timeout):
                raise cdp.CdpError('mock read timed out')
            return json.dumps({'id': message['id'], 'result': {'method': message['method']}})
        finally:
            with self.guard:
                self.receivers -= 1


class CdpConcurrencyTests(unittest.TestCase):
    def make_session(self):
        session = cdp.CdpSession('ws://unused', timeout=1)
        channel = BlockingSocket()
        session._socket = channel
        return session, channel

    def test_commands_share_one_reader_and_keep_their_own_replies(self):
        session, channel = self.make_session()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(session.call, 'first')
            try:
                self.assertTrue(channel.entered.wait(1))
                waiting = threading.Event()
                def second_call():
                    waiting.set()
                    return session.call('second', timeout=.05)
                second = pool.submit(second_call)
                self.assertTrue(waiting.wait(1))
                with self.assertRaisesRegex(cdp.CdpError, 'waiting for the CDP command channel'):
                    second.result(timeout=1)
                self.assertEqual(len(channel.sent), 1)
            finally:
                channel.release.set()
            self.assertEqual(first.result(timeout=1), {'method': 'first'})
        self.assertEqual(session.call('second'), {'method': 'second'})
        self.assertEqual(channel.max_receivers, 1)

    def test_failed_command_releases_channel(self):
        session, channel = self.make_session()
        with self.assertRaises(cdp.CdpError):
            session.call('first', timeout=.01)
        channel.release.set()
        self.assertEqual(session.call('second'), {'method': 'second'})
