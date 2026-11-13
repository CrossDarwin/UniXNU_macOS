#!/usr/bin/env python3
"""Exercise the real grow helper on disposable disk images inside a macOS VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
GIB = 1 << 30


def run(*args):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True)


def plist(*args):
    return plistlib.loads(subprocess.check_output(list(map(str, args))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extra-blocks', type=int, default=0, help='add 4 KiB blocks to test a non-GiB device boundary')
    parser.add_argument('--skip-io', action='store_true', help='skip the previously qualified 5 GiB cross-boundary I/O test')
    parser.add_argument('--keep-images', action='store_true', help='retain the disposable device image after success')
    parser.add_argument('work', type=Path, help='new private test directory on APFS')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run inside the macOS QEMU VM')
    if not 0 <= args.extra_blocks < 256:
        parser.error('--extra-blocks must be between 0 and 255')
    target = 8 * GIB + args.extra_blocks * 4096
    work = args.work.absolute()
    required = (512 << 20) if args.skip_io else 6 * GIB
    if shutil.disk_usage(work.parent).free < required:
        parser.error('Insufficient fixture space; cross-boundary I/O needs 6 GiB free, or use --skip-io')
    work.mkdir(mode=0o700)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    helper = work / 'ApfsGrow-fixture'
    run('xcrun', '--sdk', 'macosx', 'clang', '-isysroot', sdk, '-O2', '-Wall', '-Wextra', '-Werror',
        '-DAPFS_GROW_FIXTURE', HERE / 'ApfsGrow.c', '-framework', 'CoreFoundation', '-framework', 'IOKit', '-o', helper)
    probe = work / 'grow-io'
    run('xcrun', '--sdk', 'macosx', 'clang', '-isysroot', sdk, '-O2', '-Wall', '-Wextra', '-Werror',
        HERE / 'test-grow-io.c', '-o', probe)
    run(sys.executable, HERE / 'make-container.py', '--fixture', '--without-xart', '--size-gib', '4', work / 'seed')
    receipt = json.loads((work / 'seed/container.json').read_text())
    image = work / 'device.img'
    run('/bin/cp', '-c', work / 'seed/container.apfs', image)
    # The device has a reserved tail, while its published store excludes that tail.
    with image.open('r+b') as handle:
        handle.truncate(target + GIB)
        handle.seek(target)
        sentinel = hashlib.sha256(b'UniXNU diagnostic reserve').digest() * 128
        handle.write(sentinel)
        handle.seek(target + GIB - 4096)
        handle.write(sentinel)
    attached = plist('hdiutil', 'attach', '-nomount', '-section', f'0,{target // 512}', '-plist', image)
    disk = attached['system-entities'][0]['dev-entry']
    bsd = disk.removeprefix('/dev/')
    config = {
        'UniXNUAPFSInitialBytes': 4 * GIB, 'UniXNUAPFSTargetBytes': target,
        'UniXNUAPFSContainerUUID': receipt['container_uuid'],
        **{f'UniXNUAPFS{name}UUID': receipt['volumes'][name]['uuid'] for name in ('System', 'Data', 'VM')},
        'FixtureBSD': bsd,
    }
    config_path = work / 'fixture.plist'
    config_path.write_bytes(plistlib.dumps(config))
    state = work / 'state'
    def info():
        return next(row for row in plist('diskutil', 'apfs', 'list', '-plist')['Containers']
                    if row['APFSContainerUUID'].upper() == receipt['container_uuid'].upper())
    def invoke(expected=0):
        result = subprocess.run([helper, config_path, state], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (work / 'grow.log').open('a') as log:
            log.write(result.stdout)
        print(result.stdout, end='', flush=True)
        assert result.returncode == expected, result.returncode
        return result.stdout
    try:
        # A mismatched role must fail before DiskManagement can modify the container.
        bad = dict(config, UniXNUAPFSDataUUID=receipt['volumes']['VM']['uuid'])
        config_path.write_bytes(plistlib.dumps(bad))
        invoke(2)
        assert info()['CapacityCeiling'] == 4 * GIB
        config_path.write_bytes(plistlib.dumps(config))
        before = info()
        payload = b'preserve user data across APFS growth\n'
        data_device = next(v['DeviceIdentifier'] for v in before['Volumes'] if v['Name'] == 'Data')
        run('diskutil', 'mount', data_device)
        mount = Path(plist('diskutil', 'info', '-plist', data_device)['MountPoint'])
        (mount / 'grow-sentinel.txt').write_bytes(payload)
        # Interrupted preflight state is not trusted as evidence of completion.
        state.mkdir(mode=0o700)
        (state / 'apfs-grow.json').write_text('{"state":"running"}\n')
        # A failed resize must preserve the container and remain retryable.
        config_path.write_bytes(plistlib.dumps(dict(config, FixtureResizeProgram='/usr/bin/false')))
        invoke(1)
        assert info()['CapacityCeiling'] == 4 * GIB
        assert json.loads((state / 'apfs-grow.json').read_text())['state'] == 'failed'
        assert (mount / 'grow-sentinel.txt').read_bytes() == payload
        config_path.write_bytes(plistlib.dumps(config))
        output = invoke()
        assert 'complete' in output
        after = info()
        assert after['CapacityCeiling'] == target
        assert {(v['APFSVolumeUUID'], tuple(v['Roles'])) for v in before['Volumes']} == {
            (v['APFSVolumeUUID'], tuple(v['Roles'])) for v in after['Volumes']}
        assert (mount / 'grow-sentinel.txt').read_bytes() == payload
        assert 'already complete' in invoke()
        if not args.skip_io:
            run(probe, 'create', mount / 'cross-boundary.bin')
        # Hold the exact advisory lock to prove a second process performs no resize.
        import fcntl
        with (state / 'apfs-grow.lock').open('r+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert 'another check owns the lock' in invoke()
        run('diskutil', 'unmountDisk', disk)
        run('/sbin/fsck_apfs', '-n', '/dev/r' + bsd)
    finally:
        run('hdiutil', 'detach', disk)
    # Reattach with a new BSD number; identity and geometry, not cached state, decide.
    attached = plist('hdiutil', 'attach', '-nomount', '-section', f'0,{target // 512}', '-plist', image)
    disk = attached['system-entities'][0]['dev-entry']
    try:
        config['FixtureBSD'] = disk.removeprefix('/dev/')
        config_path.write_bytes(plistlib.dumps(config))
        assert 'already complete' in invoke()
        current = info()
        data_device = next(v['DeviceIdentifier'] for v in current['Volumes'] if v['Name'] == 'Data')
        run('diskutil', 'mount', data_device)
        mount = Path(plist('diskutil', 'info', '-plist', data_device)['MountPoint'])
        if not args.skip_io:
            run(probe, 'verify', mount / 'cross-boundary.bin')
            (mount / 'cross-boundary.bin').unlink()
        assert (mount / 'grow-sentinel.txt').read_bytes() == payload
        vm_device = next(v['DeviceIdentifier'] for v in current['Volumes'] if v['Name'] == 'VM')
        run('diskutil', 'mount', vm_device)
        vm_mount = Path(plist('diskutil', 'info', '-plist', vm_device)['MountPoint'])
        if not args.skip_io:
            run(probe, 'create', vm_mount / 'cross-boundary.bin')
            run(probe, 'verify', vm_mount / 'cross-boundary.bin')
            (vm_mount / 'cross-boundary.bin').unlink()
    finally:
        run('hdiutil', 'detach', disk)
    with image.open('rb') as handle:
        for offset in (target, target + GIB - 4096):
            handle.seek(offset)
            assert handle.read(4096) == sentinel
    if not args.keep_images:
        image.unlink()
    print('APFS grow - resize, retry, restart, volume identity, lock, Data and log reserve - PASS', flush=True)


if __name__ == '__main__':
    main()
