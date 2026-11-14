"""Nested diagnostics remain available without publishing binary or key inputs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('build_logs', (Path(__file__).resolve().parents[2] / 'scripts/ci/build_logs.py'))
logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logs)


class BuildLogTests(unittest.TestCase):
    def test_collects_nested_failures_and_redacts_seed_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, device, output = (root / name for name in ('work', 'device', 'output'))
            files = [work / 'logs/runtime.log', work / 'tree/.cache/mesa-build.log',
                     work / 'tree/build/mesa/meson-logs/meson-log.txt',
                     device / 'drivers/Ath12kWifi/out/wpa-macos/build.log']
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('failure - abc123ef ABC123EF\n')
            (work / 'logs/binary.log').write_bytes(b'\0binary')
            (work / 'logs/key.pem').write_text('private input')
            logs.collect(work, device, output, 'abc123ef')
            collected = list(output.rglob('*.log'))
            self.assertEqual(len(collected), 4)
            self.assertTrue(all(path.read_text() == 'failure - *** ***\n' for path in collected))
            self.assertFalse(list(output.rglob('*.pem')))
            self.assertTrue((output / 'wifi/wpa-macos/build.log').is_file())


if __name__ == '__main__':
    unittest.main()
