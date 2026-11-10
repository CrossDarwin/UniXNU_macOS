#!/usr/bin/env python3
"""Release unused tools on the ephemeral macOS runner and check build capacity."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def unused_xcodes(applications, developer):
    active = next((path for path in developer.resolve().parents if path.suffix == '.app'), None)
    if active is None:
        raise RuntimeError('Could not identify the active Xcode application; refusing cleanup')
    return [path for path in applications.glob('Xcode*.app') if path.resolve() != active]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--min-free-gib', type=int, default=50)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true' or platform.system() != 'Darwin':
        parser.error('Cleanup is restricted to an ephemeral macOS Actions runner')
    developer = Path(subprocess.check_output(['xcode-select', '-p'], text=True).strip())
    print(f'Active Xcode preserved: {developer}', flush=True)
    before = shutil.disk_usage(ROOT).free
    candidates = unused_xcodes(Path('/Applications'), developer)
    # Device and host SDKs are needed; simulator runtimes are not used by this build.
    subprocess.run(['xcrun', 'simctl', 'shutdown', 'all'], capture_output=True)
    result = subprocess.run(['xcrun', 'simctl', 'runtime', 'delete', 'all'], capture_output=True, text=True)
    if result.returncode:
        print('Simulator runtime cleanup unavailable; continuing with unused tool directories', flush=True)
    candidates += [Path.home() / 'Library/Developer/CoreSimulator',
                   Path('/Library/Developer/CoreSimulator/Caches'),
                   Path('/Library/Developer/CoreSimulator/Profiles/Runtimes'),
                   Path.home() / 'Library/Caches/Homebrew', Path('/usr/local/share/dotnet'),
                   Path.home() / 'Library/Android/sdk']
    for path in candidates:
        if path.exists():
            print(f'Releasing unused runner files: {path}', flush=True)
            subprocess.run(['sudo', 'rm', '-rf', '--', str(path)], check=True)
    available = shutil.disk_usage(ROOT).free
    print(f'Disk free: {available / 2**30:.1f} GiB; released {(available - before) / 2**30:.1f} GiB', flush=True)
    if available < args.min_free_gib * 2**30:
        raise SystemExit(f'Need at least {args.min_free_gib} GiB free before downloading OS images; '
                         'this runner needs more disk capacity')
    # Release this reserve in an always() step so disk errors cannot suppress the final diagnostics.
    reserve = ROOT / 'out/diagnostic-reserve.bin'
    reserve.parent.mkdir(parents=True, exist_ok=True)
    with reserve.open('wb') as output:
        output.write(bytes(64 * 2**20))


if __name__ == '__main__':
    main()
