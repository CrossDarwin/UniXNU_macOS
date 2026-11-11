"""Read the experimental sheng APFS input used by the kernel and payload."""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .util import Error


def container_receipt(path):
    try:
        raw = Path(path).read_bytes()
        data = json.loads(raw)
        if data['format'] != 'sheng-apfs-v1' or type(data['fixture']) is not bool or not isinstance(data['profile'], str):
            raise ValueError('Unexpected container format')
        size = data['bytes']
        if not isinstance(size, int) or size < 4 * 2**30 or size % 4096:
            raise ValueError('Expected a 4 KiB aligned container of at least 4 GiB')
        if not re.fullmatch(r'[0-9a-f]{64}', data['sha256']):
            raise ValueError('Expected the SHA-256 of the checked container')
        for key in ('container_uuid', 'volume_group_uuid', 'system_uuid'):
            data[key] = str(uuid.UUID(data[key])).upper()
        volumes = data['volumes']
        for name in ('System', 'Data', 'Preboot', 'VM', 'Hardware', 'xART'):
            if volumes[name]['roles'] != [name]:
                raise ValueError(f'{name}: unexpected APFS role')
            volumes[name]['uuid'] = str(uuid.UUID(volumes[name]['uuid'])).upper()
        if volumes['System']['uuid'] != data['system_uuid']:
            raise ValueError('System identity disagrees with the manifest')
        identities = {uuid.UUID(row['uuid']) for row in volumes.values()}
        if len(identities) != len(volumes):
            raise ValueError('Volume identities must be distinct')
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Error(f'Invalid APFS container receipt {path}: {error}') from error


def selected(ctx):
    path = os.environ.get('UNIXNU_APFS_CONTAINER')
    if not path:
        return None
    data, fingerprint = container_receipt(path)
    if ctx.schema_version != 2 or ctx.work.parent.name != data['profile'] or data['fixture']:
        raise Error('Native APFS boot requires a populated container for the selected OS profile')
    return data, fingerprint


def marker(fingerprint):
    return 'UNIXNU_NATIVE_APFS:' + fingerprint
