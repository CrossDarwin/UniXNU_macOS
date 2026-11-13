#!/usr/bin/env python3
"""Check the free-space budget used before publishing sheng APFS images."""
import importlib.util
import unittest
import tempfile
import json
from types import SimpleNamespace
from pathlib import Path

spec = importlib.util.spec_from_file_location('container', Path(__file__).with_name('make-container.py'))
container = importlib.util.module_from_spec(spec)
spec.loader.exec_module(container)


class CapacityTest(unittest.TestCase):
    def test_full_device_container_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'first boot and swap'):
            container.require_runtime_space({'CapacityCeiling': 34359738368,
                                             'CapacityFree': 111333376})

    def test_eight_gib_free_is_accepted(self):
        container.require_runtime_space({'CapacityCeiling': 64 * 2**30,
                                         'CapacityFree': 8 * 2**30})

    def test_growth_does_not_reuse_an_unrelated_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'source'
            source.mkdir()
            (source / 'container.apfs').write_bytes(bytes(4096))
            (source / 'container.json').write_text(json.dumps(
                {'format': 'sheng-apfs-v1', 'bytes': 4096, 'fixture': True}))
            stage = base / '.output.incomplete'
            stage.mkdir()
            sentinel = stage / 'existing-data'
            sentinel.write_bytes(b'preserve')
            with self.assertRaisesRegex(ValueError, 'another operation'):
                container.grow_container(SimpleNamespace(grow_container=source, size_gib=1),
                                         base / 'output')
            self.assertEqual(sentinel.read_bytes(), b'preserve')
            self.assertFalse((stage / 'grow-inputs.json').exists())

    def test_missing_capacity_is_not_treated_as_empty(self):
        with self.assertRaises(KeyError):
            container.require_runtime_space({'CapacityCeiling': 64 * 2**30})


if __name__ == '__main__':
    unittest.main()
