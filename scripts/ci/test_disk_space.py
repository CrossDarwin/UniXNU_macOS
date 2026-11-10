"""Disk cleanup must preserve the selected toolchain and refuse local hosts."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('disk_space', Path(__file__).with_name('disk_space.py'))
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


class DiskTests(unittest.TestCase):
    def test_selected_xcode_and_its_alias_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            apps = Path(directory)
            developer = apps / 'Xcode_27.app/Contents/Developer'
            developer.mkdir(parents=True)
            (apps / 'Xcode.app').symlink_to(apps / 'Xcode_27.app', target_is_directory=True)
            unused = apps / 'Xcode_26.app'
            unused.mkdir()
            self.assertEqual(disk.unused_xcodes(apps, developer), [unused])

    def test_unrecognized_toolchain_refuses_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'refusing cleanup'):
            disk.unused_xcodes(Path('/Applications'), Path('/Library/Developer/CommandLineTools'))

    def test_local_execution_never_runs_cleanup_commands(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch('sys.argv', ['disk_space.py']), \
                patch.object(disk.subprocess, 'run') as run, self.assertRaises(SystemExit):
            disk.main()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
