"""Check the namespace used by protected Safari shortcuts."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from layout import ensure_cryptex_view


class CryptexLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'Data'
        self.data.mkdir()

    def test_preserves_original_shortcut_and_is_idempotent(self):
        apps = self.data / 'Applications'
        apps.mkdir()
        safari = apps / 'Safari.app'
        target = '../System/Cryptexes/App/System/Applications/Safari.app'
        safari.symlink_to(target)
        before = safari.lstat()
        ensure_cryptex_view(self.data)
        ensure_cryptex_view(self.data)
        self.assertEqual(safari.lstat().st_ino, before.st_ino)
        self.assertEqual(os.readlink(safari), target)
        for name, target in (('App', '/System/Volumes/Preboot/Cryptexes/App'), ('OS', '/')):
            self.assertEqual(os.readlink(self.data / 'System/Cryptexes' / name), target)

    def test_rejects_parent_symlink(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.data / 'System').symlink_to(outside)
        with self.assertRaises(ValueError):
            ensure_cryptex_view(self.data)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_custom_cryptex(self):
        directory = self.data / 'System/Cryptexes'
        directory.mkdir(parents=True)
        link = directory / 'App'
        link.symlink_to('/custom/App')
        with self.assertRaises(ValueError):
            ensure_cryptex_view(self.data)
        self.assertEqual(os.readlink(link), '/custom/App')

    def test_rejects_existing_directory(self):
        app = self.data / 'System/Cryptexes/App'
        app.mkdir(parents=True)
        with self.assertRaises(ValueError):
            ensure_cryptex_view(self.data)
        self.assertTrue(app.is_dir())

    def test_runtime_repair_resolves_original_relative_link(self):
        preboot = self.root / 'Preboot/Cryptexes/App'
        app = preboot / 'System/Applications/Safari.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_text('fixture')
        binary = app / 'Contents/MacOS/Safari'
        binary.touch(mode=0o755)
        apps = self.data / 'Applications'
        apps.mkdir()
        shortcut = apps / 'Safari.app'
        shortcut.symlink_to('../System/Cryptexes/App/System/Applications/Safari.app')
        inode = shortcut.lstat().st_ino
        source = Path(__file__).with_name('safari.sh').read_text()
        source = source.replace('/System/Volumes/Data', str(self.data))
        source = source.replace('/System/Volumes/Preboot/Cryptexes/App', str(preboot))
        script = self.root / 'Safari.sh'
        script.write_text(source)
        for _ in range(2):
            subprocess.run(['bash', script, 'repair'], check=True, capture_output=True)
        self.assertEqual(shortcut.lstat().st_ino, inode)
        self.assertEqual(shortcut.resolve(), app.resolve())
        self.assertTrue((shortcut / 'Contents/MacOS/Safari').is_file())


if __name__ == '__main__':
    unittest.main()
