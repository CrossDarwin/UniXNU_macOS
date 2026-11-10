"""Exercise firmware verification and publication without vendor build tools."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wifi_firmware', Path(__file__).with_name('wifi_firmware.py'))
firmware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firmware)


class FirmwareTests(unittest.TestCase):
    def test_verified_download_and_cache_reuse(self):
        data = b'firmware bytes' * 100
        identity = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            destination = Path(temporary) / 'nested/amss.bin'
            with patch.object(firmware.urllib.request, 'urlopen', return_value=io.BytesIO(base64.b64encode(data))) as request:
                firmware.fetch(destination, identity, 'https://example.invalid/firmware')
                request.assert_called_once()
            self.assertEqual(destination.read_bytes(), data)
            with patch.object(firmware.urllib.request, 'urlopen', side_effect=AssertionError('cache must avoid network')):
                firmware.fetch(destination, identity, 'https://example.invalid/firmware')

    def test_bad_responses_preserve_existing_file(self):
        data = b'expected'
        identity = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for response in [base64.b64encode(b'wrong'), b'not base64!', b'x' * 2048]:
            with self.subTest(response=response[:16]), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / 'amss.bin'
                destination.write_bytes(b'previous')
                with contextlib.redirect_stdout(io.StringIO()), patch.object(
                        firmware.urllib.request, 'urlopen', return_value=io.BytesIO(response)):
                    with self.assertRaises(ValueError):
                        firmware.fetch(destination, identity, 'https://example.invalid/firmware')
                self.assertEqual(destination.read_bytes(), b'previous')
                self.assertFalse(destination.with_suffix('.bin.partial').exists())

    def test_manifest_rejects_unexpected_paths_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'firmware/wcn7850'
            directory.mkdir(parents=True)
            (directory / 'manifest.json').write_text(json.dumps({'files': {'../../elsewhere': {}}}))
            with patch.object(firmware.urllib.request, 'urlopen', side_effect=AssertionError('must reject before network')):
                with self.assertRaisesRegex(ValueError, 'file set'):
                    firmware.prepare(root)

    def test_manifest_bounds_are_checked_before_network(self):
        with patch.object(firmware.urllib.request, 'urlopen', side_effect=AssertionError('must reject before network')):
            for size, digest in [(0, 'a' * 64), (32 * 2**20, 'a' * 64), (16, 'bad')]:
                with self.subTest(size=size, digest=digest), self.assertRaises(ValueError):
                    firmware.fetch(Path('unused.bin'), {'bytes': size, 'sha256': digest}, 'https://example.invalid')


if __name__ == '__main__':
    unittest.main()
