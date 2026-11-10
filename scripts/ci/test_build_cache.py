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


if __name__ == '__main__':
    unittest.main()
