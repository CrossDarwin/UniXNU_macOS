#!/usr/bin/env python3
"""Collect text diagnostics from the driver and nested build tools."""
import argparse
import os
from pathlib import Path
import re


def collect(work, device, destination, seed=''):
    sources = [('driver', work / 'logs', '*.log'),
               ('runtime-tools', work / 'tree/.cache', '*.log'),
               ('build-tools', work / 'tree/build', '**/*.log'),
               ('meson', work / 'tree/build', '**/meson-log.txt'),
               ('wifi', device / 'drivers/Ath12kWifi/out', '**/*.log')]
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for label, root, pattern in sources:
        for source in sorted(root.glob(pattern)):
            if not source.is_file():
                continue
            data = source.read_bytes()
            if b'\0' in data:
                continue
            text = data.decode('utf-8', errors='replace')
            if seed:
                text = re.sub(re.escape(seed), '***', text, flags=re.IGNORECASE)
            target = destination / label / source.relative_to(root)
            if target.suffix != '.log':
                target = target.with_suffix(target.suffix + '.log')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            count += 1
    print(f'Collected {count} sanitized build logs', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--device', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.work, args.device, args.output, os.environ.get('SEED', ''))


if __name__ == '__main__':
    main()
