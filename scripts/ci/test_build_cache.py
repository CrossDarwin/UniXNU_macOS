"""Reusable archives retain build outputs while excluding private signing keys."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_cache', Path(__file__).with_name('build_cache.py'))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


@unittest.skipUnless(shutil.which('zstd'), 'zstd is required for cache archives')
class CacheTests(unittest.TestCase):
    def test_archive_restores_objects_but_excludes_signing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            build = base / cache.BUILD_PATHS[0]
            build.mkdir(parents=True)
            (build / 'startup.o').write_bytes(b'object-file')
            keys = build / 'verified-root-host/keys'
            keys.mkdir(parents=True)
            (keys / 'owner.pem').write_text('private key')
            archive = base / 'cache.tar.zst'
            self.assertTrue(cache.pack('build', base, archive))
            shutil.rmtree(build)
            cache.unpack(archive, base)
            self.assertEqual((build / 'startup.o').read_bytes(), b'object-file')
            self.assertFalse((keys / 'owner.pem').exists())

    def test_oversized_cache_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / cache.SOURCE_PATHS[0]
            source.mkdir(parents=True)
            (source / 'source').write_text('source')
            archive = base / 'cache.tar.zst'
            with patch.dict(cache.LIMITS, {'sources': 1}):
                self.assertFalse(cache.pack('sources', base, archive))
            self.assertFalse(archive.exists())

    def test_firmware_cache_cannot_replace_the_tracked_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            firmware = base / 'device/firmware/wcn7850'
            firmware.mkdir(parents=True)
            manifest = firmware / 'manifest.json'
            manifest.write_text('old manifest')
            (firmware / 'amss.bin').write_bytes(b'firmware')
            (firmware / 'Notice.txt').write_text('vendor notice')
            archive = base / 'cache.tar.zst'
            self.assertTrue(cache.pack('sources', base, archive))
            manifest.write_text('new tracked manifest')
            (firmware / 'amss.bin').unlink()
            cache.unpack(archive, base)
            self.assertEqual(manifest.read_text(), 'new tracked manifest')
            self.assertEqual((firmware / 'amss.bin').read_bytes(), b'firmware')
            self.assertEqual((firmware / 'Notice.txt').read_text(), 'vendor notice')

    def test_wifi_dependencies_restore_without_replacing_module_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            module = base / 'device/drivers/Ath12kWifi'
            dependency = module / 'deps/linux/include/net/mac80211.h'
            dependency.parent.mkdir(parents=True)
            dependency.write_text('locked source')
            lock = module / 'source-lock.json'
            lock.write_text('old lock')
            archive = base / 'cache.tar.zst'
            self.assertTrue(cache.pack('sources', base, archive))
            shutil.rmtree(module / 'deps')
            lock.write_text('current tracked lock')
            cache.unpack(archive, base)
            self.assertEqual(dependency.read_text(), 'locked source')
            self.assertEqual(lock.read_text(), 'current tracked lock')


if __name__ == '__main__':
    unittest.main()
