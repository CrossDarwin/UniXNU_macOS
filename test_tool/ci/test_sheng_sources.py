"""Reject dependency drift before downloading the operating system images."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sheng_sources', (Path(__file__).resolve().parents[2] / 'scripts/ci/sheng_sources.py'))
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


class SourceTests(unittest.TestCase):
    def context(self, root):
        ctx = SimpleNamespace(device=root / 'device', tree=root / 'tree',
                              soc=SimpleNamespace(commit=sources.SOC_REVISION))
        paths = [ctx.tree / 'kits/Sm8550Platform' / name for name in sources.SOC_INPUTS]
        paths += [ctx.device / 'drivers/QcomDwc3' / name for name in sources.USB_INPUTS]
        bluetooth = ctx.device / 'drivers/QcaBluetooth'
        paths += [bluetooth / name for name in ('tools/prepare-linux.py', 'tools/source-lock.json',
                  'src/qbt_core.c', 'src/qbt_linux.c', 'src/qbt_geni.c', 'src/qbt_transport.c',
                  'include/qbt.h', 'include/qbt_geni.h', 'include/qbt_transport.h')]
        paths += [ctx.tree / 'scripts' / f'{name}-26C5035r.json' for name in
                  ('legacy-display', 'hid-device', 'usb-xdci', 'usb-xhci', 'net-ether', 'software-aes')]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source')
        path = bluetooth / 'out/linux/btqca.c'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'original Linux input')
        (bluetooth / 'tools/source-lock.json').write_text(json.dumps(
            {'commit': 'a' * 40, 'files': {'drivers/bluetooth/btqca.c': hashlib.sha256(path.read_bytes()).hexdigest()}}))
        return ctx

    def test_missing_uart_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = self.context(Path(directory))
            (ctx.tree / 'kits/Sm8550Platform/Sm8550GeniUart.h').unlink()
            with patch.object(sources.subprocess, 'check_output', return_value=sources.USB_REVISION):
                with self.assertRaisesRegex(ValueError, 'Sm8550GeniUart.h'):
                    sources.check(ctx)

    def test_usb_checkout_must_match_reviewed_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = self.context(Path(directory))
            with patch.object(sources.subprocess, 'check_output', return_value='old gitlink'):
                with self.assertRaisesRegex(ValueError, 'Expected USB source'):
                    sources.check(ctx)

    def test_restored_bluetooth_source_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = self.context(Path(directory))
            with patch.object(sources.subprocess, 'check_output', return_value=sources.USB_REVISION):
                sources.check(ctx)
                (ctx.device / 'drivers/QcaBluetooth/out/linux/btqca.c').write_bytes(b'corrupt cache')
                with self.assertRaisesRegex(ValueError, 'mismatched Bluetooth source'):
                    sources.check(ctx)

    def test_existing_local_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory)
            path = device / 'local.toml'
            path.write_text('user configuration')
            with self.assertRaisesRegex(ValueError, 'Refuse to replace'):
                sources.configure(device)
            self.assertEqual(path.read_text(), 'user configuration')

    def test_configure_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sources.subprocess, 'run'):
                sources.configure(Path(directory))
                sources.configure(Path(directory))
            self.assertIn(sources.SOC_REVISION, (Path(directory) / 'local.toml').read_text())


if __name__ == '__main__':
    unittest.main()
