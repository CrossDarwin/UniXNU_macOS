#!/usr/bin/env python3
"""Create a private APFS clone with a local administrator and Setup Assistant completed."""

import argparse
import getpass
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from layout import ensure_cryptex_view


def load_script(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def provision(seed, data, name, display_name, password, auto_login):
    directory = data / seed.DSLOCAL
    home = data / 'Users' / name
    user_path = directory / 'users' / f'{name}.plist'
    if os.path.lexists(home) or os.path.lexists(user_path):
        raise ValueError(f'The Data volume already contains {name}')
    for path in (directory / 'users').glob('*.plist'):
        account = plistlib.loads(path.read_bytes())
        if any(int(value) >= 501 and int(value) != 65534 for value in account.get('uid', [])):
            raise ValueError('Expected an unprovisioned Data volume')
    seed.add_user(directory, name, password, display_name=display_name)
    seed.add_home(data, data, name)
    if auto_login:
        seed.add_auto_login(data, name, password)
    seed.write_root(data / seed.SETUP_SKIP,
                    plistlib.dumps({'SkipSetupItems': seed.SETUP_SKIP_ITEMS}), 0o644)
    seed.write_root(data / 'private/var/db/.AppleSetupDone', b'', 0o644)
    account = plistlib.loads(user_path.read_bytes())
    if (account['name'] != [name] or account['realname'] != [display_name]
            or account['home'] != [f'/Users/{name}'] or home.stat().st_uid != 501
            or not (home / '.skipbuddy').is_file()):
        raise ValueError('Local account verification failed')
    for group in ('admin', 'staff'):
        record = plistlib.loads((directory / 'groups' / f'{group}.plist').read_bytes())
        if name not in record['users'] or account['generateduid'][0] not in record['groupmembers']:
            raise ValueError(f'Local account missing from the {group} group')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path, help='directory with container.apfs and container.json')
    parser.add_argument('output', type=Path, help='new private output directory on the same APFS volume')
    parser.add_argument('--user', required=True, help='short local account name')
    parser.add_argument('--display-name', required=True)
    parser.add_argument('--auto-login', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.geteuid() != 0:
        parser.error('Run as root on the macOS build host')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', args.user) or not args.display_name.strip():
        parser.error('Use a lowercase account name and a nonempty display name')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Choose a new private output directory')
    container = load_script(Path(__file__).with_name('make-container.py'))
    seed = load_script(Path(__file__).parents[1] / 'make-macos-data-seed.py')
    receipt = json.loads((args.baseline / 'container.json').read_text())
    if receipt['format'] != 'sheng-apfs-v1' or receipt['fixture']:
        parser.error('Expected a populated sheng APFS container')
    source = args.baseline / 'container.apfs'
    if source.stat().st_size != receipt['bytes']:
        parser.error('Baseline image size does not match its receipt')
    password = getpass.getpass('Local macOS account password: ')
    if not password:
        parser.error('The local account needs a password')
    stage = Path(tempfile.mkdtemp(prefix='.provision-', dir=args.output.parent))
    try:
        disk = stage / 'container.apfs'
        subprocess.run(['/bin/cp', '-c', source, disk], check=True)
        disk.chmod(0o600)
        with container.attached(disk) as devices:
            reference = container.container_for(devices)['ContainerReference']
            device = container.volume(reference, uuid=receipt['volumes']['Data']['uuid'])
            with container.mounted(device) as data:
                ensure_cryptex_view(data)
                provision(seed, data, args.user, args.display_name, password, args.auto_login)
        with container.checked_container(disk) as layout:
            if any(layout[key] != receipt[key] for key in layout):
                raise ValueError('Account provisioning changed the APFS volume identities')
            digest = container.image_hash(disk)
        manifest = stage / 'container.json'
        manifest.write_text(json.dumps({**receipt, 'sha256': digest}, indent=2) + '\n')
        manifest.chmod(0o600)
        if os.environ.get('SUDO_UID') and os.environ.get('SUDO_GID'):
            for path in (disk, manifest, stage):
                os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
        stage.rename(args.output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f'Provisioned {args.user} ({args.display_name}) - {args.output}', flush=True)


if __name__ == '__main__':
    main()
