"""Reject corrupt or mismatched installation inputs before image publication."""
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import uuid

spec = importlib.util.spec_from_file_location('sheng_macos_ci', Path(__file__).with_name('sheng_macos.py'))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


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
