#!/usr/bin/env python3
"""Journal corruption checks and optional macOS sampler fault tests."""
import importlib.util
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import tempfile
import time
import unittest
import zlib

spec = importlib.util.spec_from_file_location('journal', Path(__file__).with_name('read-diagnostics.py'))
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)


def encoded(seq=1, session='test'):
    value = dict(schema=2, seq=seq, session=session, gap_s=5, sample_s=0.01, previous_io_s=0.02,
                 previous_fsync_error=0, previous_fullsync_error=0)
    payload = json.dumps(value, separators=(',', ':')).encode()
    return b'{"crc32":"' + f'{zlib.crc32(payload):08x}'.encode() + b'","sample":' + payload + b'}\n'


class Decoder(unittest.TestCase):
    def test_torn_record_does_not_hide_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log'
            path.write_bytes(encoded() + b'{"crc32":"torn"\n' + encoded(1, 'restart'))
            rows, errors = journal.read_records([path])
            self.assertEqual([r['session'] for r in rows], ['test', 'restart'])
            self.assertEqual(len(errors), 1)

    def test_crc_and_sequence_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log'
            path.write_bytes(encoded() + encoded(2).replace(b'"gap_s":5', b'"gap_s":9') + encoded(3))
            rows, errors = journal.read_records([path])
            self.assertEqual(len(errors), 1)
            self.assertEqual(journal.summarize(rows)[0]['missing_inside_range'], 1)


@unittest.skipUnless(os.environ.get('DESKTOP_SAMPLER'), 'Set DESKTOP_SAMPLER to a macOS test binary')
class Sampler(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'log'
        self.command = [os.environ['DESKTOP_SAMPLER'], '--output', str(self.path), '--interval', '1']

    def run_once(self):
        subprocess.run(self.command + ['--samples', '1'], check=True, timeout=20)

    def test_restart_retains_old_session_and_torn_tail(self):
        self.run_once()
        with self.path.open('ab') as stream:
            stream.write(b'{"sample":"interrupted')
        self.run_once()
        rows, errors = journal.read_records([self.path])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r['session'] for r in rows}), 2)
        self.assertEqual(len(errors), 1)

    def test_lock_and_abrupt_termination(self):
        child = subprocess.Popen(self.command)
        try:
            for _ in range(100):
                if self.path.exists() and self.path.stat().st_size:
                    break
                time.sleep(0.1)
            self.assertTrue(self.path.stat().st_size)
            other = subprocess.run(self.command + ['--samples', '1'], timeout=10, capture_output=True)
            self.assertNotEqual(other.returncode, 0)
        finally:
            child.kill()
            child.wait(timeout=10)
        self.run_once()
        rows, errors = journal.read_records([self.path])
        self.assertFalse(errors)
        self.assertGreaterEqual(len({r['session'] for r in rows}), 2)

    def test_rotation_keeps_valid_bounded_records(self):
        for _ in range(8):
            subprocess.run(self.command + ['--samples', '1', '--limit-kib', '16'], check=True, timeout=20)
        files = [p for p in self.path.parent.iterdir() if p.name != 'log.lock']
        self.assertGreater(len(files), 1)
        self.assertLessEqual(len(files), 4)
        self.assertTrue(all(p.stat().st_size <= 16384 for p in files))
        rows, errors = journal.read_records(files)
        self.assertFalse(errors)
        self.assertEqual(len(rows), 8)

    def test_refuses_symlink(self):
        target = self.path.parent / 'target'
        target.write_bytes(b'keep')
        self.path.symlink_to(target)
        result = subprocess.run(self.command + ['--samples', '1'], timeout=10, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_write_failure_preserves_previous_records(self):
        self.run_once()
        before = self.path.read_bytes()

        def bound_file():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (len(before) + 20, len(before) + 20))

        result = subprocess.run(self.command + ['--samples', '2'], preexec_fn=bound_file, timeout=20, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.run_once()
        rows, _ = journal.read_records([self.path])
        self.assertGreaterEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
