#!/usr/bin/env python3
"""Stage the experimental writable UFS backend without changing the device checkout."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from unixnu.apfs_root import container_receipt, marker


def write_config(destination, receipt):
    metadata, fingerprint = container_receipt(receipt)
    volumes = metadata['volumes']
    container = uuid.UUID(metadata['container_uuid']).bytes
    identity = hashlib.sha384(json.dumps(metadata, sort_keys=True).encode()).digest()
    header = ('// Generated experimental image identities; no content-integrity claim.\n'
              '#pragma once\n'
              f'#define UNIXNU_APFS_MARKER "{marker(fingerprint)}"\n'
              f'#define UNIXNU_APFS_BYTES {metadata["bytes"]}ULL\n'
              f'#define UNIXNU_APFS_SYSTEM_UUID "{metadata["system_uuid"].upper()}"\n'
              f'#define UNIXNU_APFS_DATA_UUID "{volumes["Data"]["uuid"].upper()}"\n'
              f'#define UNIXNU_APFS_VM_UUID "{volumes["VM"]["uuid"].upper()}"\n')
    for name, value in (('kNativeApfsContainerUUID', container), ('kNativeApfsImageIdentity', identity)):
        header += f'static const unsigned char {name}[{len(value)}] = {{' + ','.join(map(str, value)) + '};\n'
    (destination / 'NativeApfsConfig.h').write_text(header)


def stage(source, output, receipt):
    output.mkdir(parents=True, exist_ok=True)
    # A fresh stage prevents a stale patched file from escaping a patch failure.
    destination = Path(tempfile.mkdtemp(prefix='driver-', dir=output))
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('.git', '.codegraph', 'build'))
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(ROOT / 'native-container.patch')],
                   cwd=destination, check=True)
    shutil.copy2(ROOT / 'ContainerLayout.h', destination)
    write_config(destination, receipt)
    return destination / 'NativeUfs.cpp'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('receipt', type=Path)
    args = parser.parse_args()
    print(stage(args.source.resolve(), args.output.resolve(), args.receipt.resolve()))
