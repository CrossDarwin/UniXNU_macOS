#!/usr/bin/env python3
"""Build a sheng APFS container on macOS from prepared restore image components."""

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from image import CHUNK, export, geometry


def run(*args):
    subprocess.run(args, check=True, stdout=sys.stderr)


def plist(*args):
    return plistlib.loads(subprocess.check_output(args))


@contextmanager
def attached(image, readonly=False):
    options = ['-readonly'] if readonly else []
    with image.open('rb') as handle:
        header = handle.read(520)
    if header[32:36] == b'NXSB' or header[512:520] == b'EFI PART':
        options += ['-imagekey', 'diskimage-class=CRawDiskImage']
    result = plist('hdiutil', 'attach', '-plist', '-nomount', '-nobrowse', *options, image)
    devices = [entry['dev-entry'].removeprefix('/dev/') for entry in result['system-entities']
               if 'dev-entry' in entry]
    try:
        yield devices
    finally:
        run('hdiutil', 'detach', devices[0])


def container_for(devices):
    entries = plist('diskutil', 'apfs', 'list', '-plist')['Containers']
    matches = [entry for entry in entries if
               any(store['DeviceIdentifier'] in devices for store in entry['PhysicalStores'])]
    if len(matches) != 1:
        raise ValueError('Expected exactly one APFS container on the attached image')
    return matches[0]


def volume(container, *, name=None, uuid=None):
    entries = plist('diskutil', 'apfs', 'list', '-plist', container)['Containers'][0]['Volumes']
    matches = [entry for entry in entries
               if (name is None or entry['Name'] == name)
               and (uuid is None or entry['APFSVolumeUUID'].upper() == uuid.upper())]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one volume in {container} (name={name}, uuid={uuid})')
    return matches[0]['DeviceIdentifier']


def add(container, name, role, *options):
    run('diskutil', 'apfs', 'addVolume', container, 'APFS', name, '-role', role, '-nomount', *options)
    return volume(container, name=name)


@contextmanager
def mounted(device, readonly=False):
    options = ['readOnly'] if readonly else []
    run('diskutil', 'mount', *options, device)
    try:
        mount = plist('diskutil', 'info', '-plist', device)['MountPoint']
        yield Path(mount)
    finally:
        run('diskutil', 'unmount', device)


@contextmanager
def source_volume(image, uuid):
    with attached(image, readonly=True) as devices:
        container = container_for(devices)['ContainerReference']
        with mounted(volume(container, uuid=uuid), readonly=True) as mount:
            yield mount


def copy_contents(source, target):
    run('ditto', '--rsrc', '--extattr', '--acl', source, target)


def cryptex_paths(base):
    cryptexes = base / 'Cryptexes'
    cryptexes.mkdir(parents=True, exist_ok=True)
    link = cryptexes / 'OS'
    if not link.is_symlink():
        link.symlink_to('/')
    elif os.readlink(link) != '/':
        raise ValueError(f'Unexpected OS cryptex link: {link}')
    (base / 'com.apple.security.cryptexd' / 'Cryptexes').mkdir(parents=True, exist_ok=True)
    return cryptexes


def populate(args, system, data, preboot):
    print('Copying the System image and Data template', flush=True)
    with source_volume(args.system, args.system_uuid) as source:
        template = source / 'System/Library/Templates/Data'
        if not template.is_dir() or not (source / 'sbin/launchd').is_file():
            raise ValueError('The OS volume must contain launchd and the Data template')
        with mounted(system) as target:
            copy_contents(source, target)
        with mounted(data) as target:
            copy_contents(template, target)
            for relative in ('private/tmp', 'private/var/tmp'):
                directory = target / relative
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o1777)

    print('Staging SystemOS and AppOS cryptex contents', flush=True)
    with source_volume(args.system_os, args.system_os_uuid) as source, mounted(system) as target:
        cache = source / 'System/Library/dyld'
        if not any(cache.glob('dyld_shared_cache_arm64e*')):
            raise ValueError('SystemOS does not contain the arm64e shared cache')
        copy_contents(source, target)
        cryptex_paths(target / 'System/Volumes/Preboot')
    with source_volume(args.app_os, args.app_os_uuid) as source, mounted(preboot) as target:
        copy_contents(source, cryptex_paths(target) / 'App')


def volume_group(container):
    groups = plist('diskutil', 'apfs', 'listVolumeGroups', '-plist', container)
    entries = groups['Containers'][0]['VolumeGroups']
    if len(entries) != 1:
        raise ValueError('Expected a single System/Data volume group')
    return entries[0]


@contextmanager
def checked_container(disk):
    with attached(disk, readonly=True) as devices:
        info = container_for(devices)
        group = volume_group(info['ContainerReference'])
        raw = '/dev/r' + info['PhysicalStores'][0]['DeviceIdentifier']
        run('/sbin/fsck_apfs', '-n', raw)
        volumes = {row['Name']: {'uuid': row['APFSVolumeUUID'], 'roles': row['Roles']}
                   for row in info['Volumes']}
        members = {row['Role']: row['DiskUUID'] for row in group['Volumes']}
        if members != {name: volumes[name]['uuid'] for name in ('System', 'Data')}:
            raise ValueError('System and Data do not match the APFS volume group')
        with open(raw, 'rb', buffering=0) as handle:
            size, container_uuid = geometry(handle)
            yield {'bytes': size, 'container_uuid': container_uuid,
                   'volume_group_uuid': group['APFSVolumeGroupUUID'],
                   'system_uuid': volumes['System']['uuid'], 'volumes': volumes}, handle


def verify_xart(baseline, directory):
    before = json.loads(baseline.read_text())
    if before['format'] != 'sheng-apfs-v1' or 'xART' in before['volumes']:
        raise ValueError('Expected the original container manifest without xART')
    disk = directory / 'container.apfs'
    receipt = directory / 'container.json'
    if receipt.exists() or receipt.is_symlink():
        raise ValueError('Verify the modified image in a directory without container.json')

    with checked_container(disk) as (layout, _):
        volumes = dict(layout['volumes'])
        xart = volumes.pop('xART', None)
        if not xart or xart['roles'] != ['xART']:
            raise ValueError('The added volume does not have the xART role')
        if volumes != before['volumes']:
            raise ValueError('Existing APFS volume identities or roles changed')
        keys = ('bytes', 'container_uuid', 'volume_group_uuid', 'system_uuid')
        if any(layout[key] != before[key] for key in keys) or disk.stat().st_size != layout['bytes']:
            raise ValueError('APFS geometry or System/Data group identity changed')
        print(f'Hashing {disk.name} - {layout["bytes"] / 2**30:.2f} GiB', flush=True)
        digest = hashlib.sha256()
        with disk.open('rb') as handle:
            for data in iter(lambda: handle.read(CHUNK), b''):
                digest.update(data)

    with receipt.open('x') as handle:
        handle.write(json.dumps({**before, **layout, 'sha256': digest.hexdigest()}, indent=2) + '\n')
    print(f'Verified xART {xart["uuid"]} - {receipt}', flush=True)


def make_container(args, output):
    with tempfile.TemporaryDirectory(prefix='.apfs-', dir=output.parent) as work:
        stage = Path(work)
        disk = stage / 'volumes.sparseimage'
        run('hdiutil', 'create', '-size', f'{args.size_gib}g', '-layout', 'NONE',
            '-fs', 'APFS', '-volname', 'UnixnuScratch', '-type', 'SPARSE', disk)
        with attached(disk) as devices:
            container = container_for(devices)['ContainerReference']
            scratch = volume(container, name='UnixnuScratch')
            data = add(container, 'Data', 'D')
            system = add(container, 'System', 'S', '-groupWith', data)
            run('diskutil', 'apfs', 'deleteVolume', scratch)
            preboot = add(container, 'Preboot', 'B')
            add(container, 'VM', 'V')
            add(container, 'Update', 'E')
            add(container, 'Hardware', 'H')
            if not args.without_xart:
                add(container, 'xART', 'X')
            if not args.fixture:
                populate(args, system, data, preboot)

        with checked_container(disk) as (layout, handle):
            digest = export(handle, stage / 'container.apfs', layout['bytes'])
        metadata = {'format': 'sheng-apfs-v1', 'profile': args.profile, 'fixture': args.fixture,
                    **layout, 'sha256': digest}
        (stage / 'container.json').write_text(json.dumps(metadata, indent=2) + '\n')
        disk.unlink()
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='Container directory')
    parser.add_argument('--size-gib', type=int)
    parser.add_argument('--verify-xart', metavar='BASELINE_JSON', type=Path,
                        help='Verify an offline xART addition and write a new container.json')
    parser.add_argument('--profile', default='macos-27.3-26C5035r')
    parser.add_argument('--fixture', action='store_true', help='Create an empty test volume group')
    parser.add_argument('--without-xart', action='store_true', help='Omit xART for a test on hosts that cannot create it')
    for name in ('system', 'system-os', 'app-os'):
        parser.add_argument(f'--{name}', type=Path, help='Prepared, decrypted APFS image')
        parser.add_argument(f'--{name}-uuid', help='Required when the input has multiple APFS volumes')
    args = parser.parse_args()
    images = (args.system, args.system_os, args.app_os)
    if sys.platform != 'darwin':
        parser.error('APFS volume creation needs macOS')
    output = args.output.absolute()
    if args.verify_xart:
        if args.size_gib is not None or args.fixture or args.without_xart or any(images):
            parser.error('--verify-xart requires only the baseline and modified container directory')
        try:
            verify_xart(args.verify_xart, output)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            sys.exit(f'xART verification failed: {error}')
        return
    if args.size_gib is None or args.size_gib < (4 if args.fixture else 24):
        parser.error('Use at least 4 GiB for a fixture or 24 GiB for system images')
    if args.fixture and any(images):
        parser.error('--fixture cannot be combined with system images')
    if not args.fixture and (not all(p and p.is_file() for p in images) or os.geteuid() != 0):
        parser.error('System builds need the three image files and root privileges to preserve ownership')
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        parser.error('Use a new output directory under an existing parent')
    try:
        make_container(args, output)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'APFS build failed: {error}')


if __name__ == '__main__':
    main()
