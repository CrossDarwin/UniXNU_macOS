"""CI must show output before exit and retain exact logs and failure status."""
import contextlib
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

from unixnu.build_output import run_logged
from unixnu.util import Error, run


class BuildOutputTests(unittest.TestCase):
    def environment(self, enabled=True):
        return dict(os.environ, UNIXNU_BUILD_PROGRESS='1' if enabled else '0', PYTHONUNBUFFERED='1')

    def test_output_is_visible_before_the_child_can_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ack = root / 'ack'
            class Output(io.StringIO):
                def write(self, text):
                    if 'child-ready' in text:
                        ack.touch()
                    return super().write(text)
            console = Output()
            code = ('import pathlib,sys,time; print("child-ready", flush=True); '
                    'end=time.monotonic()+3\n'
                    'while not pathlib.Path(sys.argv[1]).exists() and time.monotonic()<end: time.sleep(.01)\n'
                    'sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 1)')
            with contextlib.redirect_stdout(console):
                result = run_logged([sys.executable, '-c', code, str(ack)],
                                    log_file=root / 'live.log', env=self.environment())
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / 'live.log').read_bytes(), b'child-ready\n')

    def test_quiet_build_reports_wait_and_preserves_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quiet.log'
            console = io.StringIO()
            code = 'import time,sys; time.sleep(.2); print("compiler failed", file=sys.stderr); sys.exit(7)'
            with contextlib.redirect_stdout(console):
                result = run_logged([sys.executable, '-c', code], log_file=path,
                                    env=self.environment(), interval=.05)
            self.assertEqual(result.returncode, 7)
            self.assertIn('no new output', console.getvalue())
            self.assertIn('failed (7)', console.getvalue())
            self.assertEqual(path.read_bytes(), b'compiler failed\n')

    def test_log_keeps_raw_bytes_while_console_decodes_and_expands_carriage_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bytes.log'
            console = io.StringIO()
            data = b'first\rsecond\n\xff'
            with contextlib.redirect_stdout(console):
                run_logged([sys.executable, '-c', f'import os; os.write(1, {data!r})'],
                           log_file=path, env=self.environment())
            self.assertEqual(path.read_bytes(), data)
            self.assertIn('first\nsecond\n\ufffd', console.getvalue())

    def test_disabled_progress_keeps_file_only_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'local.log'
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                result = run_logged([sys.executable, '-c', 'print("local")'],
                                    log_file=path, env=self.environment(False))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(console.getvalue(), '')
            self.assertEqual(path.read_text(), 'local\n')

    def test_driver_reports_failure_and_capture_remains_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            console = io.StringIO()
            with contextlib.redirect_stdout(console):
                with self.assertRaisesRegex(Error, 'failed \\(4\\)'):
                    run([sys.executable, '-c', 'print("error detail"); raise SystemExit(4)'],
                        env=self.environment(), log_file=Path(directory) / 'driver.log')
                captured = run([sys.executable, '-c', 'print("captured")'],
                               env=self.environment(), capture=True)
            self.assertEqual(captured, 'captured\n')
            self.assertEqual(console.getvalue().count('error detail\n'), 1)

    @unittest.skipUnless(shutil.which('ninja'), 'Ninja is required to verify native counters')
    def test_ninja_native_counter_is_mirrored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build.ninja').write_text('rule build\n  command = echo compiled $out\n'
                                              'build one: build\nbuild two: build\n')
            console = io.StringIO()
            env = dict(self.environment(), NINJA_STATUS='[%f/%t %p - %es] ')
            with contextlib.redirect_stdout(console):
                result = run_logged(['ninja', '-j1'], cwd=root, env=env, log_file=root / 'ninja.log')
            self.assertEqual(result.returncode, 0)
            self.assertRegex(console.getvalue(), re.escape('[1/2 '))
            self.assertRegex(console.getvalue(), re.escape('[2/2 '))


if __name__ == '__main__':
    unittest.main()
