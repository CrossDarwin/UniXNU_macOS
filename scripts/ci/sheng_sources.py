#!/usr/bin/env python3
"""Select compatible source pins for the sheng macOS package."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

SOC_REVISION = '97665e28578483082b674e8375a8fa65f4cf49cf'
USB_REVISION = 'e4efe95c903a737e0f5955e5b50a8fb4d2a7d718'
SOC_INPUTS = ('Sm8550GeniUart.h', 'Sm8550GeniUart.c', 'Sm8550Gcc.c',
              'Sm8550QupDma.h', 'Sm8550QupDma.c')
USB_INPUTS = ('QcomDwc3.cpp', 'QcomEusb2Phy.h', 'QcomQmpUsb3.h', 'QcomUcsi.h')


def configure(device: Path) -> None:
    # The device's recorded SoC predates its UART consumer. Keep the override
    # separate from tracked upstream configuration and include it in cache keys.
    text = ('[[source]]\nkind = "soc"\nname = "sm8550"\n'
            'git = "https://github.com/UniXNU/UniXNU-sm8550-common.git"\n'
            f'commit = "{SOC_REVISION}"\n')
    path = device / 'local.toml'
    if path.exists() and path.read_text() != text:
        raise ValueError(f'Refuse to replace existing source overrides: {path}')
    path.write_text(text)
    # The display archive compiles USB even though it is not a selected component.
    # The workflow checks out the published pin instead of the unavailable gitlink.
    subprocess.run(['git', '-C', str(device), 'config', 'submodule.drivers/QcomDwc3.update', 'none'], check=True)
    print(f'SM8550 source: {SOC_REVISION}', flush=True)
    print(f'USB source: {USB_REVISION} (explicit workflow checkout)', flush=True)


def check(ctx) -> None:
    if not ctx.soc or ctx.soc.commit != SOC_REVISION:
        raise ValueError(f'Expected SM8550 source {SOC_REVISION}')
    usb = ctx.device / 'drivers/QcomDwc3'
    revision = subprocess.check_output(['git', '-C', str(usb), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != USB_REVISION:
        raise ValueError(f'Expected USB source {USB_REVISION}, found {revision}')
    bluetooth = ctx.device / 'drivers/QcaBluetooth'
    required = [ctx.tree / 'kits/Sm8550Platform' / name for name in SOC_INPUTS]
    required += [usb / name for name in USB_INPUTS]
    required += [bluetooth / name for name in ('tools/prepare-linux.py', 'tools/source-lock.json',
                'src/qbt_core.c', 'src/qbt_linux.c', 'src/qbt_geni.c', 'src/qbt_transport.c',
                'include/qbt.h', 'include/qbt_geni.h', 'include/qbt_transport.h')]
    required += [ctx.tree / 'scripts' / f'{name}-26C5035r.json' for name in
                 ('legacy-display', 'hid-device', 'usb-xdci', 'usb-xhci', 'net-ether', 'software-aes')]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError('Missing native driver inputs before image preparation:\n' + '\n'.join(missing))
    lock = json.loads((bluetooth / 'tools/source-lock.json').read_text())
    for name, digest in lock['files'].items():
        path = bluetooth / 'out/linux' / Path(name).name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Missing or mismatched Bluetooth source: {path}')
    print(f'Native source pins verified: SoC {ctx.soc.commit[:12]}, USB {revision[:12]}, '
          f'Bluetooth Linux {lock["commit"][:12]}', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', type=Path, required=True)
    options = parser.parse_args()
    device = options.device.resolve()
    board = tomllib.loads((device / 'board.toml').read_text())
    if board.get('board', {}).get('name') != 'sheng':
        parser.error('Expected the sheng device repository')
    configure(device)


if __name__ == '__main__':
    main()
