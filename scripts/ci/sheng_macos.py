#!/usr/bin/env python3
"""Prepare verified macOS inputs and collect a sheng first-install package."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
PROFILE = 'macos-27.3-26C5035r'
BLOCK = 4096


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify_policy(header: bytes, anchor: bytes) -> tuple[int, int, str]:
    require(len(header) == 544 and len(anchor) == 395, 'Invalid policy or anchor length')
    magic, version, length, block, flags, size, count = struct.unpack_from('<8s4I2Q', header)
    require((magic, version, length, block, flags) == (b'VROOT01\0', 1, 160, BLOCK, 0), 'Invalid policy format')
    require(size > 0 and count == (size + BLOCK - 1) // BLOCK, 'Invalid policy geometry')
    require(anchor[:8] == b'VROOTKEY' and anchor[-3:] == b'\x01\x00\x01', 'Invalid RSA anchor')
    require(hashlib.sha256(anchor).digest() == header[120:152], 'Policy anchor digest mismatch')
    modulus = int.from_bytes(anchor[8:392], 'big')
    signature = int.from_bytes(header[160:], 'big')
    require(modulus.bit_length() == 3072 and modulus & 1 and signature < modulus, 'Invalid RSA values')
    digest_info = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(header[:160]).digest()
    expected = b'\0\1' + b'\xff' * (384 - len(digest_info) - 3) + b'\0' + digest_info
    actual = pow(signature, 65537, modulus).to_bytes(384, 'big')
    require(hmac.compare_digest(actual, expected), 'Policy RSA signature verification failed')
    return size, count, str(uuid.UUID(bytes=header[40:56])).upper()


def write_policy_table(header: bytes, anchor: bytes, blocks, destination: Path, verify_block=None) -> dict:
    size, count, volume = verify_policy(header, anchor)
    image_hash, table_hash = hashlib.sha256(), hashlib.sha256()
    done = emitted = 0
    pending = b''
    temporary = destination.with_suffix('.partial')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open('wb') as output:
            for data in blocks:
                done += len(data)
                require(done <= size, 'Image exceeds signed size')
                image_hash.update(data)
                data = pending + data
                end = len(data) // BLOCK * BLOCK
                for offset in range(0, end, BLOCK):
                    if verify_block is not None:
                        verify_block(emitted, data[offset:offset + BLOCK])
                    digest = hashlib.sha256(memoryview(data)[offset:offset + BLOCK]).digest()
                    output.write(digest)
                    table_hash.update(digest)
                    emitted += 1
                pending = data[end:]
            if pending:
                if verify_block is not None:
                    verify_block(emitted, pending)
                digest = hashlib.sha256(pending).digest()
                output.write(digest)
                table_hash.update(digest)
                emitted += 1
        require(done == size and emitted == count, 'Image shorter than signed geometry')
        require(image_hash.digest() == header[56:88], 'Image digest differs from signed policy')
        require(table_hash.digest() == header[88:120], 'Hash table differs from signed policy')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'image_bytes': size, 'volume_uuid': volume, 'image_sha256': image_hash.hexdigest(),
            'table_sha256': table_hash.hexdigest(), 'blocks_verified': count, 'signature_verified': True}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def c_block_verifier(library: Path, envelope: bytes, anchor: bytes):
    class View(ctypes.Structure):
        _fields_ = [('hashes', ctypes.c_void_p), ('image_bytes', ctypes.c_uint64), ('block_count', ctypes.c_uint64),
                    ('generation', ctypes.c_uint64), ('volume_uuid', ctypes.c_uint8 * 16), ('image_sha256', ctypes.c_uint8 * 32)]

    native = ctypes.CDLL(str(library))
    native.vr_host_open.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(View)]
    native.vr_host_open.restype = ctypes.c_int
    native.vr_host_block.argtypes = [ctypes.POINTER(View), ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
    native.vr_host_block.restype = ctypes.c_int
    encoded, trusted, view = ctypes.create_string_buffer(envelope), ctypes.create_string_buffer(anchor), View()
    require(native.vr_host_open(encoded, len(envelope), trusted, 1, ctypes.byref(view)) == 0,
            'C provider rejected the signed policy')

    def verify(index, data):
        # Keep the envelope and anchor alive while View points into their storage.
        _ = encoded, trusted
        require(native.vr_host_block(ctypes.byref(view), index, data, len(data)) == 0,
                f'C provider rejected image block {index}')
    return verify


def context(options):
    from unixnu.cli import parser
    from unixnu.config import Context
    args = parser().parse_args(['build', 'userdata', '--os', PROFILE, '--device', str(options.device),
                               '--uefi', str(options.uefi)])
    ctx = Context(args)
    require(ctx.name == 'sheng' and ctx.board['os']['target'] == 'macosx', 'Expected the sheng macOS profile')
    return ctx


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare_inputs(ctx, init_data: bool) -> None:
    from unixnu import userdata
    from unixnu.ipsw import Ipsw
    from unixnu.util import sha256_file
    source = Ipsw(ctx)
    spec = ctx.board['userdata']
    partition = (spec['last_lba'] - spec['first_lba'] + 1) * spec['lba_bytes']
    require(partition == 249497890816, 'Board geometry differs from the restored 256 GB sheng')
    rootdir = ctx.work / 'userdata'
    host = ctx.tree / 'build/verified-root-host'
    openssl = subprocess.check_output(['brew', '--prefix', 'openssl@3'], text=True).strip()
    subprocess.run([sys.executable, str(ROOT / 'kits/VerifiedRoot/tests/check.py'), '--output', str(host),
                    '--openssl-prefix', openssl], check=True)
    signer = load_module('ci_root_manifest', ROOT / 'kits/VerifiedRoot/tools/manifest.py')
    inputs, objects, checks = [], [], {}
    for window in spec['windows']:
        name = window['name']
        image = source.disk_image(window['component'])
        policy = ctx.device / 'scripts/policies' / PROFILE / name
        volume = ctx.board['root'][name.lower() + '_volume_uuid'].upper()
        size = userdata.image_size(image)
        signed = rootdir / 'signed' / name
        device = userdata.attach(image)
        try:
            with open(device.replace('/dev/disk', '/dev/rdisk'), 'rb', buffering=0) as stream:
                signer.make(stream, size, host / 'keys/owner.pem', host / 'keys/owner-public.pem', volume, 1, signed)
        finally:
            subprocess.run(['hdiutil', 'detach', device], check=True)
        envelope = (signed / 'manifest.bin').read_bytes()
        header, anchor = envelope[:544], (signed / 'trusted-key.bin').read_bytes()
        size, _, volume = verify_policy(header, anchor)
        require(volume == ctx.board['root'][name.lower() + '_volume_uuid'].upper(), f'{name}: wrong volume UUID')
        require(header[56:88].hex() == ctx.board['root'][name.lower() + '_image_sha256'], f'{name}: wrong OS profile')
        require(userdata.image_size(image) == size, f'{name}: wrong raw image size')
        table = rootdir / 'policies' / f'{name}.vroot'
        verify = c_block_verifier(host / 'verified-root.dylib', envelope, anchor)
        checks[name] = write_policy_table(header, anchor, userdata.disk_blocks(image, size), table, verify)
        policy.mkdir(parents=True, exist_ok=True)
        (policy / 'manifest.header.bin').write_bytes(header)
        (policy / 'trusted-key.bin').write_bytes(anchor)
        save_json(policy / 'verification.json', dict(checks[name], generation=1,
                  trusted_key_sha256=hashlib.sha256(anchor).hexdigest(), real_c_signature_and_block_verification=True,
                  original_image_modified=False, host_only=True, boot_root_authenticated=False,
                  qemu_tested=False, hardware_tested=False))
        objects.append(f'{name}.vroot={table}')
        inputs.append({'name': name, 'image': str(image), 'table': str(table)})
        print(f'{name}: signed policy and {checks[name]["blocks_verified"]} blocks verified', flush=True)
    ctx.args.object = objects
    raw = userdata.build(ctx)
    # The verified report is reused by the streaming sparse writer; the raw copy is disposable.
    raw.unlink()
    runtime = ctx.work / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    seed = runtime / 'data.dbr'
    if not seed.exists():
        require(init_data, 'No preserved Data seed; enable init_data only for a new installation')
        tool = ctx.tree / ctx.board['payload']['runtime']['data_seed_tool']
        command = ['sudo', 'env', 'PATH=' + os.environ['PATH'], sys.executable, str(tool), inputs[0]['image'],
                   str(seed), '--gib', str(ctx.board['payload']['runtime']['data_gib'])]
        subprocess.run(command, check=True)
    require(seed.is_file() and seed.stat().st_size > 0, 'Data seed was not generated')
    prior = runtime / 'ci-seed.json'
    seed_hash = sha256_file(seed)
    if prior.is_file():
        require(json.loads(prior.read_text())['sha256'] == seed_hash, 'Preserved Data seed has changed')
    save_json(prior, {'sha256': seed_hash, 'os_profile': PROFILE})
    report = json.loads((rootdir / 'userdata-root.json').read_text())
    data_bytes = ctx.board['payload']['runtime']['data_gib'] << 30
    reserve = 1 << 30
    require(report['image_bytes'] + data_bytes + reserve + 4 * 1024 * 1024 <= partition,
            'Root windows overlap the persistent Data or log reserve')
    save_json(rootdir / 'ci-inputs.json', {'windows': inputs, 'objects': objects, 'partition_bytes': partition,
              'data_bytes': data_bytes, 'log_reserve_bytes': reserve, 'verified_policies': checks})


def collect(ctx, destination: Path) -> None:
    from unixnu.util import sha256_file
    rootdir = ctx.work / 'userdata'
    inputs = json.loads((rootdir / 'ci-inputs.json').read_text())
    require(not destination.exists(), 'Refuse to replace an existing artifact directory')
    destination.mkdir(parents=True)
    sparse = destination / 'userdata-full.img'
    command = [sys.executable, str(ctx.device / 'scripts/make-userdata-full.py'),
               *[item['image'] for item in inputs['windows'][:2]], str(rootdir / 'userdata-root.json'),
               str(inputs['partition_bytes']), str(sparse),
               *[item['image'] for item in inputs['windows'][2:]]]
    for item in inputs['objects']:
        command += ['--object', item]
    subprocess.run(command, check=True)
    with sparse.open('rb') as handle:
        header = handle.read(28)
    magic, major, minor, file_header, chunk_header, block, count, chunks, checksum = struct.unpack('<IHHHHIIII', header)
    require((magic, major, minor, file_header, chunk_header, block) == (0xED26FF3A, 1, 0, 28, 12, BLOCK),
            'Unexpected Android sparse header')
    require(count * block == inputs['partition_bytes'] and chunks > 0 and checksum == 0, 'Sparse geometry mismatch')
    files = {'boot.img': ctx.work / 'boot/boot.img', 'payload.img': ctx.work / 'payload/payload.img',
             'kernel.macho': ctx.work / 'kernel/kernel.macho', 'package.manifest.json': ctx.work / 'package.manifest.json',
             'userdata-root.json': rootdir / 'userdata-root.json', 'data.dbr': ctx.work / 'runtime/data.dbr',
             'data-inputs.json': ctx.work / 'runtime/inputs.json'}
    for name, path in files.items():
        require(path.is_file(), f'Missing build product: {path}')
        shutil.copy2(path, destination / name)
    revisions = {}
    for name, path in [('unixnu', ROOT), ('device', ctx.device), ('uefi', ctx.args.uefi)]:
        revisions[name] = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    report = {'device': 'sheng', 'os_profile': PROFILE, 'revisions': revisions,
              'userdata_bytes': inputs['partition_bytes'], 'data_bytes': inputs['data_bytes'],
              'log_reserve_bytes': inputs['log_reserve_bytes'], 'policies': inputs['verified_policies'],
              'hardware_tested': False}
    save_json(destination / 'test-package.json', report)
    checksums = [f'{sha256_file(path)}  {path.name}' for path in sorted(destination.iterdir()) if path.is_file()]
    (destination / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n')
    print(f'Test package: {destination}', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['inputs', 'collect'])
    parser.add_argument('--device', type=Path, required=True)
    parser.add_argument('--uefi', type=Path, required=True)
    parser.add_argument('--init-data', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'out/sheng-macos-test')
    options = parser.parse_args()
    ctx = context(options)
    if options.stage == 'inputs':
        prepare_inputs(ctx, options.init_data)
    else:
        collect(ctx, options.output.resolve())


if __name__ == '__main__':
    main()
