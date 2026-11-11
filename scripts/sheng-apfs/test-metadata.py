#!/usr/bin/env python3
"""Exercise APFS metadata restoration on a macOS build host with filesystem access."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from metadata import check_file_flags, restore_metadata, rootless_attribute


@unittest.skipUnless(sys.platform == 'darwin' and os.geteuid() == 0, 'Requires macOS root')
class MetadataTest(unittest.TestCase):
    def test_restricted_flags_ownership_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            check_file_flags(base)
            source, target = base / 'source', base / 'target'
            for root in (source, target):
                (root / 'dir').mkdir(parents=True)
                (root / 'dir/file').write_bytes(b'payload')
                (root / 'link').symlink_to('dir/file')
            os.chmod(source / 'dir/file', 0o640)
            os.chmod(target / 'dir/file', 0o600)
            os.chown(target / 'dir/file', 501, 20)
            os.chflags(source / 'dir/file', 0x00080000)
            self.assertEqual(restore_metadata(source, target)['changed'], 1)
            info = (target / 'dir/file').stat()
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o640))
            self.assertTrue(info.st_flags & 0x00080000)
            self.assertIsNone(rootless_attribute(target / 'dir/file'))
            self.assertEqual((target / 'dir/file').read_bytes(), b'payload')
            self.assertEqual(os.readlink(target / 'link'), 'dir/file')
            self.assertEqual(restore_metadata(source, target)['changed'], 0)
            (target / 'dir/file').unlink()
            with self.assertRaises(FileNotFoundError):
                restore_metadata(source, target)

    def test_overlay_replaces_symlink_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source, overlay, target = [Path(directory) / name for name in ('source', 'overlay', 'target')]
            for root in (source, overlay, target):
                root.mkdir()
            (source / 'Framework').symlink_to('/nonexistent-lower-image')
            (overlay / 'Framework').mkdir()
            (target / 'Framework').mkdir()
            self.assertEqual(restore_metadata(source, target, overlay)['checked'], 0)
            restore_metadata(overlay, target)
            self.assertTrue((target / 'Framework').is_dir())


if __name__ == '__main__':
    unittest.main()
