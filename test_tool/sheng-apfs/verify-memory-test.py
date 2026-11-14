#!/usr/bin/env python3
"""Qualify compression and swap using the persisted disposable workload journal."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('journal', Path(__file__).with_name('read-diagnostics.py'))
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)

PHASES = ('baseline', 'cache_written', 'cache_read_first', 'cache_read_warm',
          'pressure_hold', 'memory_verified', 'released', 'cache_read_after_pressure', 'complete')
COUNTERS = ('compressions', 'decompressions', 'swapouts', 'swapins')


def qualify(records, *, require_io_boost=False):
    if not records:
        raise ValueError('No workload samples')
    sessions = {row['session'] for row in records}
    if len(sessions) != 1:
        raise ValueError('Select one workload session; counters cannot cross reboots')
    if [row['seq'] for row in records] != list(range(1, len(records) + 1)):
        raise ValueError('Workload samples are missing, reordered or repeated')
    phases = [row.get('phase') for row in records]
    position = -1
    for phase in PHASES:
        if phases.count(phase) != 1 or phases.index(phase) <= position:
            raise ValueError(f'Missing or out-of-order workload phase: {phase}')
        position = phases.index(phase)
    if phases[0] != 'baseline' or phases[-1] != 'complete':
        raise ValueError('Workload journal does not cover the complete run')
    for row in records:
        if row.get('vm_error', -1) or row.get('swap_error', -1):
            raise ValueError('Memory or swap sampling failed')
        for counter in COUNTERS:
            if type(row.get(counter)) is not int or row[counter] < 0:
                raise ValueError(f'Invalid counter: {counter}')
    for before, after in zip(records, records[1:]):
        if any(after[key] < before[key] for key in COUNTERS):
            raise ValueError('Memory counters reset within the workload')
    deltas = {key: records[-1][key] - records[0][key] for key in COUNTERS}
    grants = records[-1].get('swapin_io_boost_grants', 0) - records[0].get('swapin_io_boost_grants', 0)
    if require_io_boost:
        policy = records[0].get('requested_disk_policy')
        if type(policy) is not int or policy < 0 or any(
               row.get('priority_mode') != 'inherited' or row.get('requested_disk_policy') != policy
               for row in records):
            raise ValueError('Policy qualification requires an unmodified workload priority')
        if any(row.get('swapin_io_boost_error', -1) or row.get('swapin_io_boost_enabled') != 1
               for row in records) or grants <= 0:
            raise ValueError('No verified transient swap-in I/O grants')
    compression = deltas['compressions'] > 0 and deltas['decompressions'] > 0
    swap = deltas['swapouts'] > 0 and deltas['swapins'] > 0
    return dict(status='PASS' if compression and swap else 'INSUFFICIENT',
                compression='PASS' if compression else 'INSUFFICIENT',
                swap='PASS' if swap else 'INSUFFICIENT', counter_deltas=deltas,
                data_integrity='PASS', samples=len(records), session=records[0]['session'],
                peak_compressor_pages=max(row.get('compressor_pages', 0) for row in records),
                peak_swap_used_bytes=max(row.get('swap_used_bytes', 0) for row in records),
                swapin_io_boost_grants_delta=grants,
                requested_disk_policy=records[0].get('requested_disk_policy'),
                baseline_file_backed_pages=records[0].get('file_backed_pages'),
                final_file_backed_pages=records[-1].get('file_backed_pages'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path, help='reclaim.log from one disposable workload')
    parser.add_argument('--require-io-boost', action='store_true', help='require kernel I/O grants with unchanged workload priority')
    args = parser.parse_args()
    try:
        rows, errors = journal.read_records([args.journal])
        if errors:
            raise ValueError('; '.join(errors))
        result = qualify(rows, require_io_boost=args.require_io_boost)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps(dict(status='FAILED', error=str(error)), indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
