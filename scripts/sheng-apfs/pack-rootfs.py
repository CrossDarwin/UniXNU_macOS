#!/usr/bin/env python3
"""Export an APFS container and its fastboot transport from the mkosi staging directory."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/sheng-apfs').resolve()))
from image import export, geometry


def pack(input_dir, output, partition_bytes, raw_only, link_raw=False):
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
        if partition_bytes % 4096 or size > partition_bytes - 2**30:
            raise ValueError('Container and 1 GiB diagnostic reserve do not fit the target partition')
        if link_raw:
            print(f'Verifying linked raw container - {size / 2**30:.2f} GiB', flush=True)
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        else:
            digest = export(handle, raw, size)
        if digest != manifest['sha256']:
            raise ValueError('APFS image SHA-256 disagrees with the manifest')

    files = {raw.name: digest}
    if not raw_only:
        spec = importlib.util.spec_from_file_location('android_sparse', Path('tools/make-android-sparse.py'))
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


if __name__ == '__main__':
    try:
        pack(Path('inputs'), Path(os.environ['OUTPUTDIR']), int(os.environ['APFS_PARTITION_BYTES']),
             os.environ['APFS_RAW_ONLY'] == '1', os.environ.get('APFS_LINK_RAW') == '1')
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f'APFS packaging failed: {error}')
