"""Maintain the Data-side paths used by the original Cryptex shortcuts."""

import os
from pathlib import Path


def ensure_cryptex_view(data: Path) -> None:
    """Preserve Apple shortcuts and reject paths that escape the mounted Data volume."""
    if data.is_symlink() or not data.is_dir():
        raise ValueError(f'Expected a mounted Data directory: {data}')
    directory = data
    for name in ('System', 'Cryptexes'):
        directory /= name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f'Unexpected Data Cryptex parent: {directory}')
        if not directory.exists():
            directory.mkdir(mode=0o755)
            if os.geteuid() == 0:
                os.chown(directory, 0, 0)
    for name, target in (('App', '/System/Volumes/Preboot/Cryptexes/App'), ('OS', '/')):
        link = directory / name
        if link.is_symlink():
            if os.readlink(link) != target:
                raise ValueError(f'Unexpected Data Cryptex target: {link}')
        elif link.exists():
            raise ValueError(f'Expected a Data Cryptex shortcut: {link}')
        else:
            link.symlink_to(target)
            if os.geteuid() == 0:
                os.chown(link, 0, 0, follow_symlinks=False)
