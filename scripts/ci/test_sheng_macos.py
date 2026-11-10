"""Reject corrupt or mismatched installation inputs before image publication."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sheng_macos_ci', Path(__file__).with_name('sheng_macos.py'))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class CollectionCleanupTests(unittest.TestCase):
    def test_local_collection_keeps_the_reusable_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root/'ipsw/OS.dmg'
            image.parent.mkdir()
            image.write_bytes(b'preserved')
            with patch.dict(ci.os.environ, {'GITHUB_ACTIONS': 'false'}):
                ci.reclaim_collected_images(SimpleNamespace(out_root=root), [{'image': str(image)}])
            self.assertTrue(image.exists())

    def test_failed_package_copy_preserves_source_images_on_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root/'work'
            layout = work/'userdata'
            layout.mkdir(parents=True)
            image = root/'ipsw/OS.dmg'
            image.parent.mkdir()
            image.write_bytes(b'reusable OS image')
            inputs = {'windows': [{'image': str(image)}], 'partition_bytes': 4096, 'objects': []}
            (layout/'ci-inputs.json').write_text(json.dumps(inputs))
            (layout/'userdata-root.json').write_text(json.dumps({'image_bytes': 4096}))
            destination = root/'package'
            def make_sparse(command, **kwargs):
                (destination/'userdata-full.img').write_bytes(
                    struct.pack('<IHHHHIIII', 0xED26FF3A, 1, 0, 28, 12, 4096, 1, 1, 0))
            ctx = SimpleNamespace(work=work, out_root=root, device=root/'device')
            with patch.dict(ci.os.environ, {'GITHUB_ACTIONS': 'true'}), \
                    patch.object(ci.shutil, 'disk_usage', return_value=SimpleNamespace(free=100*2**30)), \
                    patch.object(ci.subprocess, 'run', side_effect=make_sparse):
                with self.assertRaisesRegex(ValueError, 'Missing build product'):
                    ci.collect(ctx, destination)
            self.assertEqual(image.read_bytes(), b'reusable OS image')
            self.assertTrue((destination/'userdata-full.img').exists())

    def test_ci_validates_all_paths_before_releasing_any_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root/'ipsw/OS.dmg'
            image.parent.mkdir()
            image.write_bytes(b'preserved')
            outside = root/'outside.dmg'
            outside.write_bytes(b'outside')
            with patch.dict(ci.os.environ, {'GITHUB_ACTIONS': 'true'}):
                with self.assertRaisesRegex(ValueError, 'outside the build cache'):
                    ci.reclaim_collected_images(SimpleNamespace(out_root=root),
                        [{'image': str(image)}, {'image': str(outside)}])
                self.assertTrue(image.exists())
                ci.reclaim_collected_images(SimpleNamespace(out_root=root), [{'image': str(image)}])
            self.assertFalse(image.exists())
            self.assertTrue(outside.exists())


class BuildInputTests(unittest.TestCase):
    def context(self, root):
        tree, device, uefi = (root / name for name in ('tree', 'device', 'uefi'))
        board = {'kernel': {'archives': ['build-one.py']},
                 'payload': {'kernel_args': ['-v'], 'runtime': {'tool': 'scripts/runtime.py',
                             'data_seed_tool': 'scripts/seed.py', 'wifi_tool': 'scripts/wifi.py'}},
                 'firmware': {'preflight': ['scripts/preflight.py --check'], 'config': 'sheng', 'dsc': 'sheng.dsc'}}
        for base, names in [(tree, ['scripts/build-one.py', 'scripts/runtime.py', 'scripts/seed.py', 'scripts/preflight.py']),
                            (device, ['scripts/wifi.py']),
                            (uefi, ['BootShim/BootShim.S', 'Resources/Scripts/mkbootimg.py',
                                    'Resources/Configs/sheng.toml', 'sheng.dsc'])]:
            for name in names:
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('input')
        return SimpleNamespace(tree=tree, device=device, args=SimpleNamespace(uefi=uefi),
                               schema_version=1, board=board, work=root / 'work')

    def test_missing_runtime_input_fails_before_image_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = self.context(Path(temporary))
            ci.check_build_inputs(ctx)
            (ctx.device / 'scripts/wifi.py').unlink()
            with patch('unixnu.ipsw.Ipsw', side_effect=AssertionError('must fail before image access')):
                with self.assertRaisesRegex(ValueError, 'scripts/wifi.py'):
                    ci.prepare_inputs(ctx, False)

    def test_seed_cache_miss_fails_before_image_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = self.context(Path(temporary))
            with patch('unixnu.ipsw.Ipsw', side_effect=AssertionError('must fail before image access')):
                with self.assertRaisesRegex(ValueError, 'No preserved Data seed'):
                    ci.prepare_inputs(ctx, False)

    def test_corrupt_seed_fails_before_image_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = self.context(Path(temporary))
            runtime = ctx.work / 'runtime'
            runtime.mkdir(parents=True)
            (runtime / 'data.dbr').write_bytes(b'corrupted')
            (runtime / 'ci-seed.json').write_text('{"sha256": "wrong"}')
            with patch('unixnu.ipsw.Ipsw', side_effect=AssertionError('must fail before image access')):
                with self.assertRaisesRegex(ValueError, 'Data seed has changed'):
                    ci.prepare_inputs(ctx, False)


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.key = cls.root / 'key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
                        '-out', str(cls.key)], check=True, capture_output=True)
        modulus = subprocess.check_output(['openssl', 'rsa', '-in', str(cls.key), '-modulus', '-noout'], text=True)
        cls.anchor = b'VROOTKEY' + bytes.fromhex(modulus.strip().split('=', 1)[1]) + b'\x01\x00\x01'

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def policy(self, data):
        table = b''.join(hashlib.sha256(data[i:i + 4096]).digest() for i in range(0, len(data), 4096))
        header = struct.pack('<8s4I2Q16s32s32s32sQ', b'VROOT01\0', 1, 160, 4096, 0, len(data),
                             (len(data) + 4095) // 4096, uuid.UUID('84581E99-85BE-4957-AC53-2947E887B3E4').bytes,
                             hashlib.sha256(data).digest(), hashlib.sha256(table).digest(),
                             hashlib.sha256(self.anchor).digest(), 1)
        path = self.root / 'header'
        path.write_bytes(header)
        signature = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(self.key), str(path)])
        return header + signature, table

    def test_signed_stream_with_unaligned_chunks_and_partial_last_block(self):
        data = b'abc123' * 1800
        header, expected = self.policy(data)
        output = self.root / 'table'
        report = ci.write_policy_table(header, self.anchor, [data[:111], data[111:7000], data[7000:]], output)
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(report['image_bytes'], len(data))
        self.assertEqual(report['blocks_verified'], 3)

    def test_corrupt_data_never_replaces_a_verified_table(self):
        data = b'a' * 8192
        header, _ = self.policy(data)
        output = self.root / 'preserved'
        output.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, 'Image digest'):
            ci.write_policy_table(header, self.anchor, [b'b' * len(data)], output)
        self.assertEqual(output.read_bytes(), b'original')
        self.assertFalse(output.with_suffix('.partial').exists())

    def test_short_and_long_images_are_rejected(self):
        data = b'x' * 4096
        header, _ = self.policy(data)
        for actual in [data[:-1], data + b'x']:
            with self.subTest(size=len(actual)), self.assertRaises(ValueError):
                ci.write_policy_table(header, self.anchor, [actual], self.root / 'invalid')

    def test_signature_and_anchor_mutations_are_rejected(self):
        header, _ = self.policy(b'x' * 4096)
        changed = bytearray(header)
        changed[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'signature'):
            ci.verify_policy(bytes(changed), self.anchor)
        changed_key = bytearray(self.anchor)
        changed_key[40] ^= 1
        with self.assertRaisesRegex(ValueError, 'anchor digest'):
            ci.verify_policy(header, bytes(changed_key))

    def test_c_provider_refusal_stops_publication(self):
        data = b'x' * 4096
        header, _ = self.policy(data)
        def refused(index, block):
            raise ValueError('C provider refusal')
        output = self.root / 'refused'
        with self.assertRaisesRegex(ValueError, 'C provider refusal'):
            ci.write_policy_table(header, self.anchor, [data], output, refused)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
