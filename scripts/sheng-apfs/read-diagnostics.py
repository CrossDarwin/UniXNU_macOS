#!/usr/bin/env python3
"""Validate bounded desktop journals without discarding intact records after a torn write."""
import argparse
import json
from pathlib import Path
import sys
import zlib


def read_records(paths):
    records, errors = [], []
    for path in paths:
        with path.open('rb') as stream:
            for line_number, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                    payload = raw.split(b',"sample":', 1)[1].rstrip(b'\n')[:-1]
                    if f'{zlib.crc32(payload):08x}' != entry['crc32']:
                        raise ValueError('CRC mismatch')
                    record = entry['sample']
                    if record['schema'] != 2 or record['seq'] < 1:
                        raise ValueError('unsupported record')
                    records.append(record)
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    errors.append(f'{path}:{line_number}: {error}')
    return records, errors


def summarize(records):
    sessions = {}
    for row in records:
        sessions.setdefault(row['session'], {})[row['seq']] = row
    result = []
    for session, by_sequence in sessions.items():
        rows = [by_sequence[n] for n in sorted(by_sequence)]
        counters = ('compressions', 'decompressions', 'swapins', 'swapouts', 'pageins', 'pageouts', 'purges')
        result.append(dict(session=session, samples=len(rows), first_seq=rows[0]['seq'], last_seq=rows[-1]['seq'],
            missing_inside_range=rows[-1]['seq'] - rows[0]['seq'] + 1 - len(rows),
            max_gap_seconds=max(r['gap_s'] for r in rows),
            max_sample_seconds=max(r['sample_s'] for r in rows),
            max_io_seconds=max(r['previous_io_s'] for r in rows),
            sync_errors=sum(bool(r['previous_fsync_error'] or r['previous_fullsync_error']) for r in rows),
            counter_deltas={key: rows[-1].get(key, 0) - rows[0].get(key, 0) for key in counters},
            first=rows[0], last=rows[-1]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', nargs='+', type=Path)
    parser.add_argument('--records', action='store_true', help='Print valid records as JSON lines')
    args = parser.parse_args()
    try:
        rows, errors = read_records(args.logs)
    except OSError as error:
        parser.exit(2, f'Cannot read diagnostic journal: {error}\n')
    if args.records:
        for row in rows:
            print(json.dumps(row, separators=(',', ':')))
    else:
        print(json.dumps(dict(sessions=summarize(rows), damaged_records=errors), indent=2))
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors) or not rows)


if __name__ == '__main__':
    raise SystemExit(main())
