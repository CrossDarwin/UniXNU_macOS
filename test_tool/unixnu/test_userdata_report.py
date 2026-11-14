"""Report-only assembly must describe the same verified manifest without a raw copy."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'scripts'))

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from unixnu import userdata


class ReportTests(unittest.TestCase):
    def test_report_matches_materialized_manifest_and_preserves_existing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obj = root / 'OS.vroot'
            obj.write_bytes(b'policy hashes')
            ctx = SimpleNamespace(work=root, args=SimpleNamespace(object=[f'OS.vroot={obj}']),
                                  board={'userdata': {'first_lba': 0, 'last_lba': 4095,
                                                      'windows': [{'name': 'OS', 'component': 'OS'}]}})
            source = Mock(remote=True)
            source.disk_image.return_value = root / 'OS.dmg'
            data = b'raw disk data' * 512
            with patch.object(userdata, 'Ipsw', return_value=source), \
                    patch.object(userdata, 'image_size', return_value=len(data)), \
                    patch.object(userdata, 'disk_blocks', side_effect=lambda *_: (block for block in [data])), \
                    patch.object(userdata.platform, 'system', return_value='Linux'):
                report = userdata.build(ctx, report_only=True)
                image = userdata.paths(ctx)[1]
                self.assertFalse(image.exists())
                expected = json.loads(report.read_text())
                self.assertIsNone(expected['image_sha256'])
                self.assertIsNone(expected['flash_command'])
                userdata.build(ctx)
                actual = json.loads(report.read_text())
                for field in ('image_bytes', 'manifest_sha256', 'windows', 'objects'):
                    self.assertEqual(expected[field], actual[field])
                self.assertEqual(hashlib.sha256(image.read_bytes()[:4096]).hexdigest(), expected['manifest_sha256'])
                image.write_bytes(b'preserved raw image')
                userdata.build(ctx, report_only=True)
                self.assertEqual(image.read_bytes(), b'preserved raw image')


if __name__ == '__main__':
    unittest.main()
