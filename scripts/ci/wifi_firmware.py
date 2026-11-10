#!/usr/bin/env python3
"""Fetch hash-pinned sheng Wi-Fi inputs before the expensive macOS build."""
import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unixnu.progress import Progress

REVISION = '69e51bb26f3fa3e118dd770f1962a93bed690dc1'
SOURCE = ('https://kernel.googlesource.com/pub/scm/linux/kernel/git/firmware/'
          f'linux-firmware.git/+/{REVISION}/ath12k/WCN7850/hw2.0')
NAMES = {'amss.bin', 'm3.bin', 'board-2.bin'}
NOTICE = {'bytes': 142991, 'sha256': '515bf4c9d620a87458e4447fe01a0e9bc384d1c3e0037cc4c3d2037b1ff25525'}


def matches(path, expected):
    return (path.is_file() and path.stat().st_size == expected['bytes'] and
            hashlib.sha256(path.read_bytes()).hexdigest() == expected['sha256'])


def fetch(path, expected, url):
    size = expected['bytes']
    digest = expected['sha256']
    if (not isinstance(size, int) or not 0 < size <= 16 * 2**20 or
            not isinstance(digest, str) or len(digest) != 64 or
            any(c not in '0123456789abcdef' for c in digest)):
        raise ValueError(f'Invalid firmware identity: {path.name}')
    if matches(path, expected):
        print(f'Wi-Fi firmware - {path.name} - reused, SHA-256 verified', flush=True)
        return
    encoded_size = 4 * ((size + 2) // 3)
    for attempt in range(3):
        try:
            encoded = bytearray()
            request = urllib.request.Request(url, headers={'User-Agent': 'UniXNU-sheng-CI'})
            with Progress(f'Wi-Fi {path.name} - base64 transfer', encoded_size) as progress:
                with urllib.request.urlopen(request, timeout=30) as response:
                    while chunk := response.read(256 * 1024):
                        encoded.extend(chunk)
                        if len(encoded) > encoded_size + 1024:
                            raise ValueError(f'Oversized firmware response: {path.name}')
                        progress.advance(len(chunk))
            try:
                data = base64.b64decode(bytes(encoded).strip(), validate=True)
            except binascii.Error as error:
                raise ValueError(f'Invalid firmware encoding: {path.name}') from error
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f'Firmware differs from pinned manifest: {path.name}')
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            print(f'Wi-Fi firmware - {path.name} - retry {attempt + 1}/2', flush=True)
            time.sleep(2 ** attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Wi-Fi firmware - {path.name} - downloaded, SHA-256 verified', flush=True)


def prepare(device):
    directory = device / 'firmware/wcn7850'
    entries = json.loads((directory / 'manifest.json').read_text())['files']
    if set(entries) != NAMES:
        raise ValueError('Expected the pinned sheng WCN7850 firmware file set')
    for name, expected in [*entries.items(), ('Notice.txt', NOTICE)]:
        fetch(directory / name, expected, f'{SOURCE}/{name}?format=TEXT')
    print(f'Wi-Fi firmware ready - linux-firmware {REVISION}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.device.resolve())


if __name__ == '__main__':
    main()
