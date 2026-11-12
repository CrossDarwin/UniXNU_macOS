#!/usr/bin/env python3
"""Export a checked APFS container and its Android sparse transport."""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MKOSI_STAGE = 'OUTPUTDIR' in os.environ and len(sys.argv) == 1
if MKOSI_STAGE:
    sys.path.insert(0, str(Path('tools/sheng-apfs').resolve()))
from image import export, geometry, hash_image


def pack(input_dir, output, partition_bytes, raw_only, link_raw, tools, *, native=False):
    manifest = json.loads((input_dir / 'container.json').read_text())
    if manifest['format'] != 'sheng-apfs-v1':
        raise ValueError('Unexpected container manifest format')
    raw = output / 'rootfs.img'
    with (input_dir / 'container.apfs').open('rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Expected a regular APFS image file')
        size, container_uuid = geometry(handle)
        if size != info.st_size or size != manifest['bytes']:
            raise ValueError('APFS geometry disagrees with the file or manifest')
        if container_uuid != manifest['container_uuid']:
            raise ValueError('APFS container UUID disagrees with the manifest')
        if partition_bytes <= 0 or partition_bytes % 4096 or size > partition_bytes - 2**30:
            raise ValueError('Container and 1 GiB diagnostic reserve do not fit the target partition')
        required = 0
        if not raw_only:
            required += size + (size // 4096) * 12 + 28
        if not link_raw and not (native and sys.platform == 'darwin'):
            required += size
        if required:
            required += 256 * 2**20
            free = shutil.disk_usage(output).free
            print(f'Packaging space - available {free / 2**30:.2f} GiB'
                  f' - required up to {required / 2**30:.2f} GiB', flush=True)
            if free < required:
                raise ValueError('Insufficient packaging space; preserve the container and boot images,'
                                 ' free space and resume with build-images.py --pack-only WORK')
        if link_raw:
            print(f'Verifying linked raw container - {size / 2**30:.2f} GiB', flush=True)
            digest = hash_image(handle, size, f'Verifying {raw.name}')
        elif native and sys.platform == 'darwin':
            print(f'Cloning {raw.name} - {size / 2**30:.2f} GiB', flush=True)
            subprocess.run(['/bin/cp', '-c', input_dir / 'container.apfs', raw], check=True)
            with raw.open('rb') as cloned:
                digest = hash_image(cloned, size, f'Verifying {raw.name}')
        else:
            digest = export(handle, raw, size)
        if digest != manifest['sha256']:
            raise ValueError('APFS image SHA-256 disagrees with the manifest')

    files = {raw.name: digest}
    if not raw_only:
        spec = importlib.util.spec_from_file_location('android_sparse', tools / 'make-android-sparse.py')
        sparse_tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sparse_tool)
        sparse = output / 'rootfs.fastboot.img'
        print(f'Encoding {sparse.name}', flush=True)
        blocks, chunks = sparse_tool.convert(raw, sparse)
        print(f'Verifying sparse expansion - {chunks} chunks', flush=True)
        if sparse_tool.expand_hash(sparse, blocks) != digest:
            raise ValueError('Sparse expansion differs from the raw APFS container')
        files[sparse.name] = sparse_tool.sha256_file(sparse)

    report = output / 'rootfs.json'
    report.write_text(json.dumps({**manifest, 'partition_bytes': partition_bytes, 'files': files,
                                  'boot_verified': False, 'flash_ready': False}, indent=2) + '\n')
    files[report.name] = hashlib.sha256(report.read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in files.items()))
    print(f'Packaged System volume {manifest["system_uuid"]} - boot validation pending', flush=True)


def main():
    if MKOSI_STAGE:
        pack(Path('inputs'), Path(os.environ['OUTPUTDIR']), int(os.environ['APFS_PARTITION_BYTES']),
             os.environ['APFS_RAW_ONLY'] == '1', os.environ.get('APFS_LINK_RAW') == '1', Path('tools'))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--partition-bytes', type=int, required=True)
    parser.add_argument('--raw-only', action='store_true')
    parser.add_argument('--link-raw', action='store_true')
    parser.add_argument('container', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.link_raw and not args.raw_only:
        parser.error('--link-raw requires --raw-only')
    if os.path.lexists(args.output):
        parser.error('Choose a new output directory')
    with tempfile.TemporaryDirectory(prefix='.rootfs-', dir=args.output.parent) as temp:
        staged = Path(temp) / 'images'
        staged.mkdir(mode=0o700)
        if args.link_raw:
            (staged / 'rootfs.img').hardlink_to(args.container / 'container.apfs')
        pack(args.container, staged, args.partition_bytes, args.raw_only, args.link_raw,
             Path(__file__).resolve().parents[1], native=True)
        if os.path.lexists(args.output):
            raise ValueError('The package output already exists')
        staged.rename(args.output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'APFS packaging failed: {error}')
