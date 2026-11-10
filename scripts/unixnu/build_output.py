"""Mirror build logs while preserving subprocess status and full output on disk."""
import codecs
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def enabled(env=None):
    return (os.environ if env is None else env).get('UNIXNU_BUILD_PROGRESS') == '1'


def elapsed(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def run_logged(command, *, log_file, cwd=None, env=None, interval=10):
    """Keep raw logs intact; quiet notices describe output inactivity, not a hang verdict."""
    if interval <= 0:
        raise ValueError('Build progress interval must be positive')
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not enabled(env):
        with path.open('wb') as output:
            return subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    started = changed = reported = time.monotonic()
    label = path.stem
    print(f'== build - {label} - started - log {path}', flush=True)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    with path.open('wb') as output, path.open('rb') as reader:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)

        def drain(final=False):
            nonlocal changed
            while data := reader.read(64 * 1024):
                changed = time.monotonic()
                sys.stdout.write(decoder.decode(data).replace('\r', '\n'))
            if final:
                sys.stdout.write(decoder.decode(b'', final=True))
            sys.stdout.flush()

        try:
            while True:
                try:
                    process.wait(timeout=min(0.2, interval))
                    break
                except subprocess.TimeoutExpired:
                    pass
                drain()
                now = time.monotonic()
                if now - changed >= interval and now - reported >= interval:
                    try:
                        space = f'{shutil.disk_usage(path.parent).free / 2**30:.1f} GiB free'
                    except OSError:
                        space = 'disk space unavailable'
                    print(f'\n== build - {label} - elapsed {elapsed(now - started)}'
                          f' - no new output for {int(now - changed)}s'
                          f' - log {reader.tell() / 2**20:.1f} MiB - {space}', flush=True)
                    reported = now
            drain(final=True)
        except BaseException:
            # Match subprocess.run cleanup: reap the child before returning control.
            process.kill()
            process.wait()
            raise
    state = 'complete' if process.returncode == 0 else f'failed ({process.returncode})'
    print(f'\n== build - {label} - {state} - elapsed {elapsed(time.monotonic() - started)}', flush=True)
    return subprocess.CompletedProcess(command, process.returncode)
