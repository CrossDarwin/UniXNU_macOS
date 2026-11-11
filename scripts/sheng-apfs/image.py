"""Raw APFS geometry and sparse file export."""

import hashlib
import os
import struct
import time
import uuid

CHUNK = 1024 * 1024


def geometry(handle):
    header = handle.read(4096)
    if len(header) != 4096 or header[32:36] != b'NXSB':
        raise ValueError('Expected a bare APFS container')
    block, count = struct.unpack_from('<IQ', header, 36)
    if block not in (4096, 8192, 16384, 32768, 65536) or count == 0:
        raise ValueError('Invalid APFS container geometry')
    handle.seek(0)
    return block * count, str(uuid.UUID(bytes=header[72:88])).upper()


def export(handle, destination, size):
    print(f'Exporting {destination.name} - {size / 2**30:.2f} GiB', flush=True)
    digest = hashlib.sha256()
    written = 0
    next_update = time.monotonic() + 10
    with destination.open('xb') as output:
        while written < size:
            data = handle.read(min(CHUNK, size - written))
            if not data:
                raise ValueError('Unexpected end of the APFS container')
            digest.update(data)
            if data.count(0) == len(data):
                output.seek(len(data), os.SEEK_CUR)
            else:
                output.write(data)
            written += len(data)
            now = time.monotonic()
            if now >= next_update:
                print(f'{destination.name} - {written / size:.1%} - {written}/{size} bytes', flush=True)
                next_update = now + 10
        output.truncate(size)
    return digest.hexdigest()
