"""Transfer progress and validated range reuse across process restarts."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from unixnu.ipsw import RangeDiskCache, RangeFile
from unixnu.progress import Progress


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.body = bytes(range(256)) * 20
        self.gets = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(owner.body)))
                self.send_header('ETag', owner.etag)
                self.end_headers()
            def do_GET(self):
                start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
                owner.gets.append((start, end))
                self.send_response(206)
                data = owner.body[start:end + 1]
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.etag = 'release-1'
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}/restore.ipsw'

    def stop_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def download(self):
        with patch.dict(os.environ, {'UNIXNU_IPSW_RANGE_CACHE': self.temp.name, 'UNIXNU_IPSW_CACHE_MIB': '1'}):
            with RangeFile(self.url, block=1024) as source:
                actual = source.read()
                return actual, source.downloaded, source.reused

    def test_restart_reuses_validated_ranges_without_downloading_them(self):
        self.assertEqual(self.download(), (self.body, len(self.body), 0))
        self.gets.clear()
        self.assertEqual(self.download(), (self.body, 0, len(self.body)))
        self.assertEqual(self.gets, [(0, 0)])

    def test_corrupt_range_is_downloaded_again(self):
        self.download()
        path = sorted(Path(self.temp.name).glob('*/*.block'))[0]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        actual, downloaded, reused = self.download()
        self.assertEqual(actual, self.body)
        self.assertEqual(downloaded, 1024)
        self.assertEqual(reused, len(self.body) - 1024)

    def test_same_url_with_changed_etag_cannot_reuse_old_data(self):
        self.download()
        self.body = self.body[::-1]
        self.etag = 'release-2'
        self.assertEqual(self.download(), (self.body, len(self.body), 0))

    def test_budget_keeps_prefix_without_scan_eviction(self):
        cache = RangeDiskCache(self.temp.name, 'fixture', 1024 + 32)
        cache.put(0, b'a' * 1024)
        cache.put(1024, b'b' * 1024)
        restarted = RangeDiskCache(self.temp.name, 'fixture', 1024 + 32)
        self.assertEqual(restarted.get(0, 1024), b'a' * 1024)
        self.assertIsNone(restarted.get(1024, 1024))
        self.assertLessEqual(restarted.used, 1024 + 32)


class ProgressTests(unittest.TestCase):
    def test_stalled_transfer_is_distinct_from_working_transfer(self):
        now = [0]
        lines = []
        progress = Progress('download', 10 * 2**20, clock=lambda: now[0], emit=lines.append)
        now[0] = 10
        progress.advance(2 * 2**20)
        progress.report()
        self.assertIn('20.0%', lines[-1])
        self.assertIn('elapsed 00:10', lines[-1])
        self.assertNotIn('no new bytes', lines[-1])
        now[0] = 30
        progress.report()
        self.assertIn('no new bytes for 20s', lines[-1])

    def test_failed_transfer_stops_its_reporter(self):
        lines = []
        progress = Progress('download', 1024, emit=lines.append)
        with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
            with progress:
                raise RuntimeError('transfer failed')
        self.assertFalse(progress.thread.is_alive())
        self.assertTrue(lines[-1].endswith('failed'))


if __name__ == '__main__':
    unittest.main()
