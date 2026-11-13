#!/usr/bin/env python3
"""Build and package sheng Native APFS images from the macOS IPSW."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))

from unixnu import cli
from unixnu.apfs_root import container_receipt
from unixnu.config import Context
from unixnu.ipsw import Ipsw
from unixnu.util import Error, sha256_file

BOOT_LIMIT = 192 * 2**20
RESTORE_IMAGES = (('system', 'OS', 'os_volume_uuid'),
                  ('system-os', 'Cryptex1,SystemOS', 'systemos_volume_uuid'),
                  ('app-os', 'Cryptex1,AppOS', 'app_volume_uuid'))


def step(label):
    print(f'\n== {label}', flush=True)


def python(script, *args, privileged=False, env=None):
    prefix = ['sudo', '--'] if privileged and os.geteuid() != 0 else []
    subprocess.run([*prefix, sys.executable, script, *args], cwd=ROOT, env=env, check=True)


def unixnu(*args):
    python(ROOT / 'unixnu', *args)


def build_container(args, ctx, source, directory):
    if directory.exists():
        print(f'Reusing APFS container - {directory}', flush=True)
        return
    if args.container:
        raise Error(f'Existing APFS container directory not found: {directory}')
    images = []
    for name, component, uuid_key in RESTORE_IMAGES:
        image = source.disk_image(component)
        images += [f'--{name}', image, f'--{name}-uuid', ctx.board['root'][uuid_key]]
    rsync = ['--rsync', Path(args.rsync).expanduser().resolve()] if args.rsync else []
    print('APFS creation needs administrator privileges. Respond to the sudo prompt if shown.', flush=True)
    python(HERE / 'make-container.py', '--size-gib', str(args.size_gib), '--profile',
           ctx.work.parent.name, *rsync, *images, directory, privileged=True)


def prepare_runtime(ctx, source, directory, modules):
    spec = ctx.board['payload']['runtime']
    board = ctx.device
    if spec.get('wifi_tool'):
        python(HERE.parent / 'ci/wifi_firmware.py', '--device', board)
        env = os.environ.copy()
        index = int(env.get('GIT_CONFIG_COUNT', '0'))
        env.update({'GIT_CONFIG_COUNT': str(index + 1), f'GIT_CONFIG_KEY_{index}': 'core.sparseCheckoutCone',
                    f'GIT_CONFIG_VALUE_{index}': 'false'})
        python(board / 'drivers/Ath12kWifi/tools/fetch-sources.py', env=env)
    bluetooth = modules.get(('driver', 'QcaBluetooth'))
    if bluetooth is not None:
        python(bluetooth.path / 'tools/prepare-linux.py')
    if 'scripts/stage-video-firmware.py' in spec.get('firmware_tools', []):
        python(board / 'scripts/extract-files.py', '--section', 'Video')
        python(board / 'drivers/QcomIris/tools/fetch-sources.py')
    directory.mkdir(parents=True, exist_ok=True)
    request = dict(spec, tree=str(ctx.tree), device=str(board), output=str(directory),
                   native_apfs=True, init_data=False, boot_shims=False, gpu_addon=None,
                   gpu_addon_enabled=spec.get('gpu_addon', True), trust_cache=None,
                   os_image=str(source.disk_image('OS')),
                   trust_modules=[str(source.extract(name)) for name in spec['trust_components']])
    if bluetooth is not None:
        request['bluetooth_component'] = str(bluetooth.path)
    path = directory / 'request.json'
    path.write_text(json.dumps(request, indent=2) + '\n')
    python(ctx.tree / spec['tool'], '--request', path)


def template_boot(template, payload, output):
    transport = (template / 'boot.img').read_bytes()
    receipt = json.loads((template / 'boot.json').read_text())
    if (len(transport) < 4096 or transport[:8] != b'ANDROID!'
            or hashlib.sha256(transport).hexdigest() != receipt['boot_sha256']
            or struct.unpack_from('<I', transport, 40)[0] != 0):
        raise ValueError('Expected a verified version 0 sheng boot template')
    kernel_size = struct.unpack_from('<I', transport, 8)[0]
    second_size, _, _, page = struct.unpack_from('<4I', transport, 24)
    if page != 4096 or second_size or not 0 < kernel_size <= len(transport) - page:
        raise ValueError('Unsupported sheng boot template layout')
    kernel = transport[page:page + kernel_size]
    kernel_sha = hashlib.sha256(kernel).hexdigest()
    if kernel_sha != receipt['transport_kernel_sha256']:
        raise ValueError('Boot template transport does not match its receipt')
    ramdisk = payload.read_bytes()
    header = bytearray(transport[:page])
    struct.pack_into('<I', header, 16, len(ramdisk))
    boot_id = hashlib.sha1()
    for data in (kernel, ramdisk, b''):
        boot_id.update(data)
        boot_id.update(struct.pack('<I', len(data)))
    header[576:608] = boot_id.digest() + bytes(12)
    with output.open('xb') as handle:
        handle.write(header)
        for data in (kernel, ramdisk):
            handle.write(data)
            handle.write(bytes(-len(data) % page))
    return kernel_sha


def build_boot(args, ctx, common, container, runtime, work):
    receipt_path = container / 'container.json'
    receipt, fingerprint = container_receipt(receipt_path)
    if receipt['fixture'] or receipt['profile'] != ctx.work.parent.name:
        raise ValueError('Expected a populated APFS container for the selected macOS profile')
    os.environ['UNIXNU_APFS_CONTAINER'] = str(receipt_path)
    stages = ('kernel', 'payload') if args.boot_template else ('kernel', 'package', 'firmware', 'payload')
    unixnu('build', *stages, *common, '--runtime-dir', str(runtime))
    payload = ctx.work / 'payload/payload.img'
    record = json.loads(payload.with_suffix('.json').read_text())
    if (record['apfs_container_receipt_sha256'] != fingerprint or record['apfs_container_uuid'] != receipt['container_uuid']
            or record['payload_bytes'] != payload.stat().st_size or record['payload_sha256'] != sha256_file(payload)):
        raise ValueError('The payload does not match the APFS container or its build report')

    destination = work / 'boot'
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.boot-', dir=work) as temporary:
        stage = Path(temporary)
        boot = stage / 'boot.img'
        if args.boot_template:
            kernel_sha = template_boot(args.boot_template, payload, boot)
        else:
            unixnu('bootimg', *common, '--ramdisk', str(payload), '--output', str(boot))
            kernel_sha = json.loads((stage / 'boot.json').read_text())['kernel_sha256']
        if not 0 < boot.stat().st_size <= BOOT_LIMIT:
            raise ValueError(f'Boot image exceeds the verified sheng transport limit of {BOOT_LIMIT >> 20} MiB')
        shutil.copyfile(payload, stage / payload.name)
        shutil.copyfile(payload.with_suffix('.json'), stage / 'payload.json')
        record = {**record, 'boot_bytes': boot.stat().st_size, 'boot_sha256': sha256_file(boot),
                  'transport_kernel_sha256': kernel_sha,
                  'hardware_tested': False}
        (stage / 'boot.json').write_text(json.dumps(record, indent=2) + '\n')
        for name in ('boot.img', 'payload.img', 'payload.json', 'boot.json'):
            (stage / name).replace(destination / name)
    geometry = ctx.board['userdata']
    return (geometry['last_lba'] - geometry['first_lba'] + 1) * geometry['lba_bytes']


def prepare(args, work, container):
    common = ['--os', 'macos-27']
    for argument in args.kernel_arg or []:
        common += [f'--kernel-arg={argument}']
    for name in ('device', 'uefi', 'ipsw'):
        value = getattr(args, name)
        if value:
            common += [f'--{name}', str(value)]
    step('1/4 - Resolve sheng sources and validate the build inputs')
    ctx = Context(cli.parser().parse_args(['tree', *common]))
    if ctx.name != 'sheng' or ctx.board['payload']['runtime']['target'] != 'macos27':
        raise Error('Expected the sheng macOS profile')
    cli.need_uefi(ctx)
    chip_id = ctx.option_text('unique_chip_id', 'UNIXNU_UNIQUE_CHIP_ID')
    if not chip_id:
        raise Error('Set UNIXNU_UNIQUE_CHIP_ID or configure the local platform seed before building')
    if not ctx.config_path.is_file():
        unixnu('defconfig', ctx.config_name, *common)
    modules = cli.prepare(ctx)
    source = Ipsw(ctx) if not container.exists() or not args.runtime_dir else None
    step('2/4 - Create and verify the APFS container')
    build_container(args, ctx, source, container)
    if args.user:
        personal = work / 'personal'
        account = work / 'account.json'
        request = {'baseline_sha256': sha256_file(container / 'container.json'), 'user': args.user,
                   'display_name': args.display_name or args.user, 'auto_login': args.auto_login}
        if personal.exists() and (not account.is_file() or json.loads(account.read_text()) != request):
            raise ValueError('Account or container differs from the preserved Data image')
        if not personal.exists():
            account.write_text(json.dumps(request, indent=2) + '\n')
            auto = ['--auto-login'] if args.auto_login else []
            python(HERE / 'provision-data.py', container, personal, '--user', args.user,
                   '--display-name', args.display_name or args.user, *auto, privileged=True)
        container = personal
    step('3/4 - Build the runtime, kernel, payload and boot image')
    runtime = args.runtime_dir or ctx.work / 'runtime-native-apfs'
    if not args.runtime_dir:
        prepare_runtime(ctx, source, runtime, modules)
    partition = build_boot(args, ctx, common, container, runtime, work)
    state = {'container': os.path.relpath(container, work), 'partition_bytes': partition}
    (work / 'build.json').write_text(json.dumps(state, indent=2) + '\n')


def package(args, work):
    step('4/4 - Package and verify the root filesystem images')
    state = json.loads((work / 'build.json').read_text())
    container = args.container if (args.pack_only or args.use_mkosi) and args.container else work / state['container']
    receipt, fingerprint = container_receipt(container / 'container.json')
    boot = work / 'boot'
    record = json.loads((boot / 'boot.json').read_text())
    if (record['apfs_container_receipt_sha256'] != fingerprint or record['apfs_container_uuid'] != receipt['container_uuid']
            or record['boot_sha256'] != sha256_file(boot / 'boot.img')
            or record['payload_sha256'] != sha256_file(boot / 'payload.img')):
        raise ValueError('The boot and root filesystem inputs do not match')
    destination = work / 'rootfs'
    options = ['--use-mkosi'] if args.use_mkosi else []
    if args.raw_only:
        options += ['--raw-only']
    subprocess.run([HERE / 'pack-rootfs.sh', *options, '--partition-bytes', str(state['partition_bytes']),
                    container, destination], check=True)
    checksums = ''.join(f'{digest}  rootfs/{name}\n'
                       for digest, name in (line.split() for line in (destination / 'SHA256SUMS').read_text().splitlines()))
    checksums += f"{record['boot_sha256']}  boot/boot.img\n{record['payload_sha256']}  boot/payload.img\n"
    (work / 'SHA256SUMS').write_text(checksums)
    print(f'Images and SHA256SUMS - {work}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--prepare-only', action='store_true', help='build the container and boot images, then stop')
    modes.add_argument('--pack-only', action='store_true', help='package a previously prepared build')
    modes.add_argument('--use-mkosi', action='store_true', help='package a previously prepared build with mkosi on Linux')
    parser.add_argument('--device', type=Path, help='sheng source checkout')
    parser.add_argument('--uefi', type=Path, help='uefi-sheng source checkout')
    parser.add_argument('--ipsw', help='local IPSW path or URL (default: the pinned macOS profile IPSW)')
    parser.add_argument('--container', type=Path, help='reuse an existing container and its exact receipt')
    parser.add_argument('--size-gib', type=int, default=48, help='initial APFS container size in GiB (default: 48; grows on device)')
    parser.add_argument('--rsync', help='rsync 3 executable for creating the container')
    parser.add_argument('--runtime-dir', type=Path, help='reuse a complete runtime report and its matched addon/trust bundle')
    parser.add_argument('--boot-template', type=Path, help='reuse the verified sheng transport from a boot.img and boot.json directory')
    parser.add_argument('--kernel-arg', action='append', help='extra kernel boot argument (repeatable)')
    parser.add_argument('--user', help='provision a local administrator and skip Setup Assistant')
    parser.add_argument('--display-name', help='display name of the local administrator')
    parser.add_argument('--auto-login', action='store_true', help='automatically log in to the provisioned account')
    parser.add_argument('--raw-only', action='store_true', help='omit the Android sparse image')
    parser.add_argument('work', type=Path, help='private workspace for the images and build state')
    args = parser.parse_args()
    if args.use_mkosi and not sys.platform.startswith('linux'):
        parser.error('--use-mkosi runs the packaging step on Linux')
    if not (args.pack_only or args.use_mkosi) and sys.platform != 'darwin':
        parser.error('Image preparation requires macOS')
    if (args.display_name or args.auto_login) and not args.user:
        parser.error('--display-name and --auto-login require --user')
    for name in ('device', 'uefi', 'runtime_dir', 'boot_template', 'container'):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, path.expanduser().resolve())
    if args.ipsw and '://' not in args.ipsw:
        args.ipsw = str(Path(args.ipsw).expanduser().resolve())
    work = args.work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (work / '.build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.prepare_only and os.path.lexists(work / 'rootfs'):
            raise ValueError('The workspace already has a rootfs package; choose a new workspace for a new package')
        if not (args.pack_only or args.use_mkosi):
            prepare(args, work, args.container or work / 'container')
        if not args.prepare_only:
            package(args, work)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (Error, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'Image build failed: {error}')
