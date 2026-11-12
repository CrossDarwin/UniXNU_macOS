"""Assign the xART role to an empty volume in a detached 4 KiB APFS image."""

import os
import struct
import uuid

from image import geometry

BLOCK = 4096
VOLUME_ROLE = 964
XART_ROLE = 0x100


def checksum(data):
    first = second = 0
    modulus = 0xffffffff
    for word, in struct.iter_unpack('<I', data):
        first = (first + word) % modulus
        second = (second + first) % modulus
    low = modulus - (first + second) % modulus
    return struct.pack('<II', low, modulus - (first + low) % modulus)


def u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


def read_block(image, number, blocks):
    if not 0 <= number < blocks:
        raise ValueError('APFS block is outside the container')
    image.seek(number * BLOCK)
    return bytearray(image.read(BLOCK))


def valid(block, kind):
    return (len(block) == BLOCK and block[:8] == checksum(block[8:])
            and struct.unpack_from('<H', block, 24)[0] == kind)


def latest_checkpoint(image, blocks):
    header = read_block(image, 0, blocks)
    if not valid(header, 1) or header[32:36] != b'NXSB':
        raise ValueError('Invalid APFS container superblock')
    count = struct.unpack_from('<I', header, 104)[0]
    base = u64(header, 112)
    if not 0 < count < 2**31 or base < 1 or base + count > blocks:
        raise ValueError('Expected a contiguous APFS checkpoint descriptor area')
    latest = None
    for number in range(base, base + count):
        block = read_block(image, number, blocks)
        if block[32:36] == b'NXSB' and valid(block, 1):
            if latest is None or u64(block, 16) > u64(latest, 16):
                latest = block
    if latest is None:
        raise ValueError('No valid APFS checkpoint found')
    return latest


def volume_mappings(image, checkpoint, blocks):
    omap = read_block(image, u64(checkpoint, 160), blocks)
    if not valid(omap, 11) or u64(omap, 56):
        raise ValueError('Expected an APFS container object map without snapshots')
    leaf = read_block(image, u64(omap, 48), blocks)
    if not valid(leaf, 2) or struct.unpack_from('<HH', leaf, 32) != (7, 0):
        raise ValueError('Expected a single fixed-size APFS object map root leaf')
    count, table, table_size = struct.unpack_from('<IHH', leaf, 36)
    table += 56
    key_base = table + table_size
    value_base = BLOCK - 40
    if table_size < count * 4 or key_base > value_base:
        raise ValueError('Invalid APFS object map table')
    xid = u64(checkpoint, 16)
    mappings = {}
    for index in range(count):
        key, value = struct.unpack_from('<HH', leaf, table + index * 4)
        key += key_base
        value = value_base - value
        if not key_base <= key <= key + 16 <= value <= value_base - 16:
            raise ValueError('Invalid APFS object map entry')
        oid, transaction = struct.unpack_from('<QQ', leaf, key)
        flags, size, block = struct.unpack_from('<IIQ', leaf, value)
        if transaction <= xid and (oid not in mappings or mappings[oid][0] < transaction):
            mappings[oid] = (transaction, flags, size, block)
    return mappings


def assign_role(path, container_uuid, volume_uuid):
    target = uuid.UUID(volume_uuid).bytes
    with path.open('r+b') as image:
        size, identity = geometry(image)
        if size != os.fstat(image.fileno()).st_size or identity != container_uuid:
            raise ValueError('The detached APFS image does not match the staged container')
        blocks = size // BLOCK
        checkpoint = latest_checkpoint(image, blocks)
        if struct.unpack_from('<IQ', checkpoint, 36) != (BLOCK, blocks) or checkpoint[72:88] != uuid.UUID(identity).bytes:
            raise ValueError('Expected a 4 KiB checkpoint for the staged container')
        mappings = volume_mappings(image, checkpoint, blocks)
        count = struct.unpack_from('<I', checkpoint, 180)[0]
        if not 0 < count <= 100:
            raise ValueError('Invalid APFS volume slot count')
        candidate = None
        for oid in struct.unpack_from(f'<{count}Q', checkpoint, 184):
            if not oid:
                continue
            transaction, flags, mapped_size, number = mappings[oid]
            volume = read_block(image, number, blocks)
            if flags or mapped_size != BLOCK or not valid(volume, 13) or volume[32:36] != b'APSB' or (
                    u64(volume, 8), u64(volume, 16)) != (oid, transaction):
                raise ValueError('Invalid APFS volume mapping')
            role = struct.unpack_from('<H', volume, VOLUME_ROLE)[0]
            if volume[240:256] == target:
                if candidate is not None or role not in (0, XART_ROLE) or volume[704:709] != b'xART\0':
                    raise ValueError('Expected the staged xART volume')
                candidate = (number, volume, role)
            elif role == XART_ROLE:
                raise ValueError('The container already has another xART volume')
        if candidate is None:
            raise ValueError('The staged xART volume was not found')
        number, volume, role = candidate
        if role == XART_ROLE:
            return
        struct.pack_into('<H', volume, VOLUME_ROLE, XART_ROLE)
        volume[:8] = checksum(volume[8:])
        image.seek(number * BLOCK)
        image.write(volume)
        image.flush()
        os.fsync(image.fileno())
