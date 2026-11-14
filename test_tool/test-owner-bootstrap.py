#!/usr/bin/env python3
"""Owner LaunchDaemons must enter the system bootstrap cache before Aqua starts."""
import importlib.util
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

spec = importlib.util.spec_from_file_location('runtime_builder', (Path(__file__).resolve().parents[1] / 'scripts/build-boot-runtime.py'))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def addon(files):
    output = b''
    for name, body in files.items():
        raw = name.encode() + b'\0'
        packed = zlib.compress(body)
        output += struct.pack('<IIQQ', len(raw), 0o644, len(body), len(packed))
        output += raw + bytes(-len(raw) % 8) + packed + bytes(-len(packed) % 8)
    return b'NGPUADD2' + struct.pack('<IIQ', len(files), 0, 24 + len(output)) + output


class Bootstrap(unittest.TestCase):
    def test_late_jobs_are_collected_with_existing_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / 'addon'
            files = {'bin/not-a-job': b'unused'}
            for name in ('desktop-diagnostics', 'apfs-grow', 'safari-repair', 'wifi.supplicant'):
                files[f'Library/LaunchDaemons/org.unixnu.{name}.plist'] = plistlib.dumps(dict(Label=f'org.unixnu.{name}'))
            files['xpc/launchd.plist'] = plistlib.dumps(dict(LaunchDaemons={}))
            path.write_bytes(addon(files))
            jobs, cached = builder.owner_job_inputs(path, root / 'staged')
            self.assertEqual(len(list(jobs.glob('*.plist'))), 4)
            self.assertEqual(plistlib.loads(cached.read_bytes()), dict(LaunchDaemons={}))

    def test_rejects_bad_archive_and_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / 'addon'
            path.write_bytes(addon({'Library/LaunchDaemons/../bad.plist': b'bad'}))
            with self.assertRaisesRegex(ValueError, 'invalid addon job entry'):
                builder.owner_job_inputs(path, root / 'staged')
            path.write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'invalid addon'):
                builder.owner_job_inputs(path, root / 'other')

    def test_cache_merge_is_idempotent_and_preserves_apple_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); jobs = root / 'jobs'; jobs.mkdir()
            source = root / 'original.plist'; out = root / 'result.plist'
            apple = {'/System/Library/LaunchDaemons/com.apple.test.plist': dict(Label='com.apple.test', Disabled=False)}
            source.write_bytes(plistlib.dumps(dict(LaunchDaemons=apple, LaunchAgents={'keep': 1})))
            job = jobs / 'org.unixnu.desktop-diagnostics.plist'
            job.write_bytes(plistlib.dumps(dict(Label='org.unixnu.desktop-diagnostics', RunAtLoad=True)))
            with patch.object(builder, 'run'):
                first = builder.merge_launchd_cache(source, jobs, out)
                self.assertEqual(len(first['added_jobs']), 1)
                second = root / 'second.plist'
                report = builder.merge_launchd_cache(out, jobs, second)
                self.assertEqual(report['added_jobs'], [])
                self.assertEqual(report['updated_jobs'], [])
                self.assertEqual(plistlib.loads(out.read_bytes()), plistlib.loads(second.read_bytes()))
                value = plistlib.loads(second.read_bytes())
                self.assertEqual(value['LaunchAgents'], {'keep': 1})
                self.assertTrue(all(value['LaunchDaemons'][k] == v for k, v in apple.items()))
                job.write_bytes(plistlib.dumps(dict(Label='org.unixnu.desktop-diagnostics', RunAtLoad=True, ThrottleInterval=60)))
                changed = builder.merge_launchd_cache(second, jobs, out)
                self.assertEqual(len(changed['updated_jobs']), 1)
                self.assertFalse(changed['original_entries_preserved'])

    def test_rejects_non_owner_and_conflicting_labels(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); jobs = root / 'jobs'; jobs.mkdir()
            source = root / 'original.plist'; out = root / 'result.plist'
            key = '/System/Library/LaunchDaemons/org.unixnu.test.plist'
            source.write_bytes(plistlib.dumps(dict(LaunchDaemons={key: dict(Label='com.apple.original')})))
            job = jobs / 'org.unixnu.test.plist'
            job.write_bytes(plistlib.dumps(dict(Label='com.apple.test')))
            with self.assertRaisesRegex(ValueError, 'owner service label'):
                builder.merge_launchd_cache(source, jobs, out)
            job.write_bytes(plistlib.dumps(dict(Label='org.unixnu.test')))
            with self.assertRaisesRegex(ValueError, 'already owns'):
                builder.merge_launchd_cache(source, jobs, out)


if __name__ == '__main__':
    unittest.main()
