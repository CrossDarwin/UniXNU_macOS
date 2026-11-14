"""Exact userdata capacity shared by offline preparation and packaging."""

LOG_RESERVE = 1 << 30
BLOCK = 4096


def usable_bytes(partition):
    if (type(partition) is not int or partition % BLOCK or
            not 5 * (1 << 30) <= partition <= (1 << 63) - 1):
        raise ValueError('userdata must be 4 KiB aligned and hold at least 4 GiB APFS plus the 1 GiB log reserve')
    return partition - LOG_RESERVE
