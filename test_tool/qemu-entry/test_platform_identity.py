"""Check the ARM inputs consumed by IOPlatformExpertDevice::generatePlatformUUID."""
from pathlib import Path
import hashlib
import struct
import subprocess
import sys
import tempfile
import unittest


def read_node(blob, offset=0):
    properties, children = {}, {}
    property_count, child_count = struct.unpack_from('<II', blob, offset)
    offset += 8
    for _ in range(property_count):
        key, size = struct.unpack_from('<32sI', blob, offset)
        offset += 36
        properties[key.split(b'\0')[0].decode()] = blob[offset:offset + size]
        offset += (size + 3) & ~3
    for _ in range(child_count):
        child, offset = read_node(blob, offset)
        children[child[0]['name'].rstrip(b'\0').decode()] = child
    return (properties, children), offset


class PlatformIdentity(unittest.TestCase):
    def generate(self, *arguments):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.adt'
            subprocess.run([sys.executable, Path(__file__).with_name('make-dt.py'),
                            '--output', path, *arguments], check=True,
                           stdout=subprocess.DEVNULL)
            blob = path.read_bytes()
        tree, end = read_node(blob)
        self.assertEqual(end, len(blob))
        return tree[1]['chosen'][0]

    def test_stable_arm_uuid_inputs_across_reboots(self):
        identities = []
        for _ in range(2):
            chosen = self.generate('--armvirt', '--nvram-proxy')
            self.assertEqual(len(chosen['unique-chip-id']), 8)
            self.assertEqual(len(chosen['chip-id']), 4)
            self.assertTrue(chosen['nvram-proxy-data'])
            space = bytes.fromhex('a6dd4ccbb5e84af5acddb6dc6a0542b8')
            identities.append(hashlib.sha1(space + chosen['unique-chip-id'] +
                                           chosen['chip-id']).digest()[:16])
        self.assertEqual(identities[0], identities[1])

    def test_minimal_entry_fixture_has_no_platform_identity(self):
        self.assertNotIn('unique-chip-id', self.generate())


if __name__ == '__main__':
    unittest.main()
