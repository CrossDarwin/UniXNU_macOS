#!/usr/bin/env python3
"""Check the default flashing transport and its explicit raw-only opt-out."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('android_sparse', (Path(__file__).resolve().parents[2] / 'scripts/make-android-sparse.py'))
sparse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sparse)


class PackageDefault(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'input'
        self.source.mkdir()
        identifier = uuid.uuid4()
        # Transport fixture only; it is not a mountable APFS filesystem.
        raw = bytearray(4 * 2**20)
        raw[32:36] = b'NXSB'
        struct.pack_into('<IQ', raw, 36, 4096, len(raw) // 4096)
        raw[72:88] = identifier.bytes
        raw[2**20:2**20 + 4096] = bytes(range(256)) * 16
        self.digest = hashlib.sha256(raw).hexdigest()
        (self.source / 'container.apfs').write_bytes(raw)
        (self.source / 'container.json').write_text(json.dumps(dict(
            format='sheng-apfs-v1', fixture=True, bytes=len(raw),
            container_uuid=str(identifier).upper(), system_uuid=str(uuid.uuid4()).upper(),
            sha256=self.digest)))

    def package(self, output, *options, check=True):
        return subprocess.run(['bash', str((Path(__file__).resolve().parents[2] / 'scripts/sheng-apfs/pack-rootfs.sh')), *options,
                               '--partition-bytes', str(2 * 2**30), str(self.source), str(output)],
                              capture_output=True, text=True, check=check)

    def test_default_emits_verified_android_sparse_transport(self):
        output = self.base / 'default'
        self.package(output)
        image = output / 'rootfs.fastboot.img'
        with image.open('rb') as stream:
            self.assertEqual(struct.unpack('<I', stream.read(4))[0], sparse.MAGIC)
        self.assertEqual(sparse.expand_hash(image, 1024), self.digest)
        self.assertLess(image.stat().st_size, 4 * 2**20)
        report = json.loads((output / 'rootfs.json').read_text())
        self.assertEqual(report['files']['rootfs.img'], self.digest)
        self.assertEqual(report['files'][image.name], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertIn('rootfs.fastboot.img', (output / 'SHA256SUMS').read_text())

    def test_raw_only_remains_an_explicit_opt_out(self):
        output = self.base / 'raw-only'
        self.package(output, '--raw-only')
        self.assertFalse((output / 'rootfs.fastboot.img').exists())
        self.assertEqual(hashlib.sha256((output / 'rootfs.img').read_bytes()).hexdigest(), self.digest)

    def test_existing_package_is_preserved(self):
        output = self.base / 'existing'
        output.mkdir()
        sentinel = output / 'keep'
        sentinel.write_bytes(b'existing package')
        result = self.package(output, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'existing package')


if __name__ == '__main__':
    unittest.main()
