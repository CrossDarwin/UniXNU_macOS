#!/usr/bin/env python3
"""Bounded source and build caches, isolated by revisions and host toolchain."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
WORK = Path('UniXNU/out/sheng/macos-27.3-26C5035r/sheng_macos/tree')
SOURCE_PATHS = [Path('UniXNU/out/feeds'), Path('device/.cache/firmware')]
SOURCE_PATHS += [Path('device/firmware/wcn7850') / name for name in ('amss.bin', 'm3.bin', 'board-2.bin', 'Notice.txt')]
SOURCE_PATHS += [Path('device/drivers/Ath12kWifi/deps')]
SOURCE_PATHS += [Path('device/drivers/QcomIris/deps')]
SOURCE_PATHS += [Path('device/drivers/QcaBluetooth/out/linux')]
SOURCE_PATHS += [WORK / '.cache' / name for name in ('mesa-work', 'gpu-reference', 'mesa-venv', 'mesa-tools',
                                                  'github-sources', 'sources', 'adreno-source', 'softfloat-source',
                                                  'appsync-signer/sources')]
BUILD_PATHS = [WORK / 'build', WORK / '.cache/fakeroot', WORK / '.cache/xnu-work']
LIMITS = {'sources': 1024 * 2**20, 'build': 2048 * 2**20}


def run(*args, cwd=ROOT):
    return subprocess.check_output([str(arg) for arg in args], cwd=cwd, text=True).strip()


def identity(device, uefi):
    source = {'version': 1, 'workspace': str(ROOT.parent), 'python': sys.version,
              'feeds': (ROOT / 'feeds.toml').read_text(), 'modules': (ROOT / '.gitmodules').read_text(),
              'os': (ROOT / 'profiles/os/macos-27.3-26C5035r/profile.toml').read_text(),
              'device_feeds': (device / 'feeds.toml').read_text(), 'device_board': (device / 'board.toml').read_text(),
              'device_overrides': (device / 'local.toml').read_text() if (device / 'local.toml').is_file() else '',
              'usb_module': run('git', '-C', device / 'drivers/QcomDwc3', 'rev-parse', 'HEAD'),
              'bluetooth_module': run('git', '-C', device, 'ls-tree', 'HEAD', 'drivers/QcaBluetooth'),
              'wifi_module': run('git', '-C', device, 'ls-tree', 'HEAD', 'drivers/Ath12kWifi')}
    compiled = {**source, 'core': run('git', 'ls-files', '-s'),
                'device': run('git', '-C', device, 'ls-files', '-s'), 'uefi': run('git', '-C', uefi, 'rev-parse', 'HEAD'),
                'xcode': run('xcodebuild', '-version'), 'clang': run('xcrun', 'clang', '--version'),
                'sdk': run('xcrun', '--sdk', 'iphoneos', '--show-sdk-path'),
                'llvm': run('llvm-config', '--version'), 'rust': run('rustc', '+1.98.0', '--version')}
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return {'sources': digest(source), 'build': digest(compiled)}


def paths_for(kind, base):
    choices = SOURCE_PATHS if kind == 'sources' else BUILD_PATHS
    return [str(path) for path in choices if (base / path).exists()]


def pack(kind, base, output):
    output.unlink(missing_ok=True)
    paths = paths_for(kind, base)
    if not paths:
        print(f'{kind} cache: no reusable files', flush=True)
        return False
    if shutil.disk_usage(base).free < LIMITS[kind] + 512 * 2**20:
        print(f'{kind} cache: skipped because the runner is low on disk space', flush=True)
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    # Signing keys are regenerated locally and never enter a shared compiler cache.
    command = ['tar', '-cf', '-', '--exclude=*.partial',
               '--exclude=*/verified-root-host/keys', '-C', str(base), *paths]
    producer = subprocess.Popen(command, stdout=subprocess.PIPE)
    compressor = None
    oversized = False
    try:
        compressor = subprocess.Popen(['zstd', '-T0', '-3'], stdin=producer.stdout, stdout=subprocess.PIPE)
        producer.stdout.close()
        with output.open('wb') as target:
            size = 0
            while data := compressor.stdout.read(1 << 20):
                size += len(data)
                if size > LIMITS[kind]:
                    oversized = True
                    break
                target.write(data)
        complete = not oversized
        if complete:
            compressor.wait(timeout=10)
            producer.wait(timeout=10)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        producer.stdout.close()
        if compressor is not None:
            compressor.stdout.close()
        # Stop both processes on a size limit or an interrupted/failed write.
        for process in (compressor, producer):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (compressor, producer):
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    compressed, status = compressor.returncode, producer.returncode
    if oversized:
        output.unlink()
        print(f'{kind} cache: skipped archive above the {LIMITS[kind] / 2**20:.0f} MiB limit', flush=True)
        return False
    if status or compressed:
        output.unlink(missing_ok=True)
        raise RuntimeError(f'{kind}: could not archive reusable inputs')
    size = output.stat().st_size
    print(f'{kind} cache: {size / 2**20:.1f} MiB saved', flush=True)
    return True


def unpack(archive, base):
    if not archive.is_file():
        print(f'cache miss: {archive.name}', flush=True)
        return
    producer = subprocess.Popen(['zstd', '-d', '-c', str(archive)], stdout=subprocess.PIPE)
    try:
        consumer = subprocess.run(['tar', '-xf', '-', '-C', str(base)], stdin=producer.stdout)
    finally:
        producer.stdout.close()
        status = producer.wait()
    if status or consumer.returncode:
        raise RuntimeError(f'could not restore {archive}')
    archive.unlink()
    print(f'cache restored: {archive.name}; removed the unpacked archive', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['identity', 'pack', 'unpack'])
    parser.add_argument('--device', type=Path, default=ROOT.parent / 'device')
    parser.add_argument('--uefi', type=Path, default=ROOT.parent / 'uefi-sheng')
    parser.add_argument('--kind', choices=['sources', 'build'])
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    if args.stage == 'identity':
        values = identity(args.device.resolve(), args.uefi.resolve())
        text = ''.join(f'{key}={value}\n' for key, value in values.items())
        if args.github_output:
            with args.github_output.open('a') as output:
                output.write(text)
        else:
            print(text, end='')
    else:
        if not args.archive or (args.stage == 'pack' and not args.kind):
            parser.error('--archive is required; pack also needs --kind')
        if args.stage == 'pack':
            pack(args.kind, ROOT.parent, args.archive.resolve())
        else:
            unpack(args.archive.resolve(), ROOT.parent)


if __name__ == '__main__':
    main()
