"""Transfer progress and validated range reuse across process restarts."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from unixnu.ipsw import Ipsw, RangeDiskCache, RangeFile, prune_aea
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
                with owner.lock:
                    owner.gets.append((start, end))
                    owner.active += 1
                    owner.maximum = max(owner.maximum, owner.active)
                    reject = start in owner.fail_once
                    owner.fail_once.discard(start)
                try:
                    if reject:
                        self.send_error(503)
                        return
                    if end > start:
                        time.sleep(owner.delay)
                    self.send_response(206)
                    data = owner.body[start:end + 1]
                    self.send_header('Content-Length', str(len(data)))
                    offset = start + 1 if owner.wrong_offset else start
                    self.send_header('Content-Range', f'bytes {offset}-{end}/{len(owner.body)}')
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    with owner.lock:
                        owner.active -= 1
        self.lock = threading.Lock()
        self.active = self.maximum = 0
        self.delay = 0
        self.fail_once = set()
        self.wrong_offset = False
        self.etag = 'release-1'
        class Server(ThreadingHTTPServer):
            request_queue_size = 128
        self.server = Server(('127.0.0.1', 0), Handler)
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

    def test_eight_connections_are_bounded_to_the_selected_window(self):
        self.body = bytes(range(256)) * 128
        self.delay = 0.1
        with RangeFile(self.url, block=1024, workers=8) as source:
            source.seek(4096)
            with source.prefetch(8192):
                self.assertEqual(source.read(8192), self.body[4096:12288])
            self.assertEqual(source.prefetch_end, 0)
            self.assertLessEqual(len(source.cache), 8)
        self.assertGreater(self.maximum, 1)
        self.assertLessEqual(self.maximum, 8)
        self.assertEqual(sorted(self.gets), [(0, 0)] + [(i*1024, i*1024+1023) for i in range(4,12)])
        self.assertFalse(any(t.name.startswith('ipsw-range') for t in threading.enumerate()))

    def test_maximum_64_connections_preserves_order_and_bounds(self):
        self.body = bytes(range(256)) * 512
        self.delay = 0.1
        with RangeFile(self.url, block=1024, workers=64) as source:
            source.seek(4096)
            with source.prefetch(64*1024):
                self.assertEqual(source.read(64*1024), self.body[4096:4096+64*1024])
            self.assertLessEqual(len(source.cache), 64)
        self.assertGreater(self.maximum, 8)
        self.assertLessEqual(self.maximum, 64)
        self.assertEqual(sorted(self.gets), [(0,0)] + [(i*1024,i*1024+1023) for i in range(4,68)])
        self.assertFalse(any(t.name.startswith('ipsw-range') for t in threading.enumerate()))

    def test_parallel_download_reuses_validated_cache_after_restart(self):
        self.body = bytes(range(256)) * 64
        environment = {'UNIXNU_IPSW_RANGE_CACHE': self.temp.name, 'UNIXNU_IPSW_CACHE_MIB': '1'}
        for restarted in (False, True):
            self.gets.clear()
            with patch.dict(os.environ, environment), RangeFile(self.url, block=1024, workers=8) as source:
                with source.prefetch(len(self.body)):
                    self.assertEqual(source.read(), self.body)
                self.assertEqual(source.downloaded, 0 if restarted else len(self.body))
                self.assertEqual(source.reused, len(self.body) if restarted else 0)
            if restarted:
                self.assertEqual(self.gets, [(0, 0)])

    def test_parallel_failure_retries_only_the_failed_range(self):
        self.body = bytes(range(256)) * 32
        self.fail_once = {2048}
        with RangeFile(self.url, block=1024, workers=8) as source:
            with source.prefetch(len(self.body)):
                self.assertEqual(source.read(), self.body)
        self.assertEqual(self.gets.count((2048,3071)), 2)
        self.assertEqual(len(self.gets), 10)

    def test_wrong_response_offset_is_rejected(self):
        with RangeFile(self.url, block=1024, retries=1, workers=8) as source:
            self.wrong_offset = True
            with self.assertRaisesRegex(OSError, 'offset'):
                source.read(1024)
        self.assertFalse(any(t.name.startswith('ipsw-range') for t in threading.enumerate()))

    def test_actual_zip_extraction_keeps_crc_and_sha256_checks(self):
        data = os.urandom(32*1024)
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=compression) as archive:
                archive.writestr('OS.dmg.aea', data)
                archive.writestr('unrelated.bin', os.urandom(64*1024))
            self.body = buffer.getvalue()
            self.gets.clear()
            with RangeFile(self.url, block=1024, workers=8) as stream, zipfile.ZipFile(stream) as archive:
                source = Ipsw.__new__(Ipsw)
                source.stream, source.zip, source.remote = stream, archive, True
                source.identity = {'Manifest': {'OS': {'Info': {'Path': 'OS.dmg.aea'}}}}
                source.config = {'sha256': {'OS.dmg.aea': hashlib.sha256(data).hexdigest()}}
                source.cache = Path(self.temp.name)/str(compression)
                self.assertEqual(source.extract('OS').read_bytes(), data)
                self.assertEqual(stream.prefetch_end, 0)
                self.assertLess(stream.downloaded, len(self.body))

    def test_worker_limit_is_validated_before_network_access(self):
        for workers in (0,65):
            with self.assertRaisesRegex(Exception, 'workers'):
                RangeFile(self.url, workers=workers)

    def test_budget_keeps_prefix_without_scan_eviction(self):
        cache = RangeDiskCache(self.temp.name, 'fixture', 1024 + 32)
        cache.put(0, b'a' * 1024)
        cache.put(1024, b'b' * 1024)
        restarted = RangeDiskCache(self.temp.name, 'fixture', 1024 + 32)
        self.assertEqual(restarted.get(0, 1024), b'a' * 1024)
        self.assertIsNone(restarted.get(1024, 1024))
        self.assertLessEqual(restarted.used, 1024 + 32)


class PruneTests(unittest.TestCase):
    def test_encrypted_copy_is_removed_only_when_enabled_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'OS.dmg.aea'
            archive.write_bytes(b'encrypted')
            plain = archive.with_suffix('')
            plain.write_bytes(b'plain')
            with patch.dict(os.environ, {'UNIXNU_PRUNE_AEA': '1'}):
                prune_aea(archive)
                self.assertTrue(archive.exists())
                for path in (archive, plain):
                    path.with_suffix(path.suffix + '.json').write_text('{}')
                with patch.dict(os.environ, {'UNIXNU_PRUNE_AEA': '0'}):
                    prune_aea(archive)
                self.assertTrue(archive.exists())
                prune_aea(archive)
            self.assertFalse(archive.exists())
            self.assertEqual(plain.read_bytes(), b'plain')
            self.assertTrue(archive.with_suffix('.aea.json').exists())


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
