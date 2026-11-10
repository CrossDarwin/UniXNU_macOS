"""Periodic byte progress for long transfers and image preparation."""
import threading
import time

from .util import log


def duration(seconds):
    if seconds is None:
        return '--:--'
    seconds = max(0, int(seconds))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


class Progress:
    def __init__(self, label, total, interval=10, clock=time.monotonic, emit=log):
        self.label, self.total, self.interval = label, total, interval
        self.clock, self.emit = clock, emit
        self.done = 0
        self.started = self.changed = clock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def advance(self, count):
        with self.lock:
            self.done += count
            self.changed = self.clock()

    def report(self, state='running'):
        with self.lock:
            now = self.clock()
            done, elapsed, idle = self.done, max(0, now - self.started), max(0, now - self.changed)
        fraction = min(1, done / self.total) if self.total else 1
        speed = done / elapsed if elapsed else 0
        eta = max(0, self.total - done) / speed if speed else None
        bar = '#' * int(fraction * 20) + '-' * (20 - int(fraction * 20))
        wait = f' - no new bytes for {int(idle)}s' if state == 'running' and idle >= self.interval else ''
        self.emit(f'{self.label} [{bar}] {fraction:.1%} - {done / 2**20:.1f}/{self.total / 2**20:.1f} MiB'
                  f' - {speed / 2**20:.1f} MiB/s - elapsed {duration(elapsed)} - ETA {duration(eta)} - {state}{wait}')

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            self.report()

    def __enter__(self):
        self.report()
        self.thread = threading.Thread(target=self._heartbeat, name='unixnu-progress', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join()
        self.report('failed' if kind else 'complete')
