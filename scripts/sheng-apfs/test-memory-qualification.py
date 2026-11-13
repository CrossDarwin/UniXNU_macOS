"""Prevent an idle system or an incomplete workload from qualifying swap."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('qualification', Path(__file__).with_name('verify-memory-test.py'))
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def records(active=True):
    rows = []
    for index, phase in enumerate(qualification.PHASES, 1):
        rows.append(dict(session='one-boot', seq=index, phase=phase, vm_error=0, swap_error=0,
                         **{key: index * 20 if active else 0 for key in qualification.COUNTERS}))
    return rows


class Qualification(unittest.TestCase):
    def test_requires_writes_and_reads_for_both_backing_mechanisms(self):
        rows = records()
        self.assertEqual(qualification.qualify(rows)['status'], 'PASS')
        for key in qualification.COUNTERS:
            with self.subTest(counter=key):
                idle = [dict(row, **{key: 0}) for row in rows]
                self.assertEqual(qualification.qualify(idle)['status'], 'INSUFFICIENT')

    def test_low_pressure_is_not_a_failure_or_pass(self):
        self.assertEqual(qualification.qualify(records(False))['status'], 'INSUFFICIENT')

    def test_incomplete_or_failed_workloads_do_not_pass(self):
        for rows in (records()[:-1], records()[1:], records()[::-1]):
            with self.assertRaises(ValueError):
                qualification.qualify(rows)
        rows = records()
        rows[-1]['swap_error'] = 5
        with self.assertRaises(ValueError):
            qualification.qualify(rows)

    def test_counters_cannot_be_joined_across_boots_or_reset(self):
        rows = records()
        rows[-1]['session'] = 'second-boot'
        with self.assertRaises(ValueError):
            qualification.qualify(rows)

    def test_io_policy_requires_real_grants_without_test_priority_override(self):
        rows = [dict(row, priority_mode='inherited', requested_disk_policy=0,
                     swapin_io_boost_error=0, swapin_io_boost_enabled=1,
                     swapin_io_boost_grants=row['seq']) for row in records()]
        self.assertEqual(qualification.qualify(rows, require_io_boost=True)['status'], 'PASS')
        for change in (dict(priority_mode='foreground'),
                       dict(swapin_io_boost_error=2), dict(swapin_io_boost_enabled=0),
                       dict(swapin_io_boost_grants=0)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                qualification.qualify([dict(row, **change) for row in rows], require_io_boost=True)
        inherited_background = [dict(row, requested_disk_policy=3) for row in rows]
        self.assertEqual(qualification.qualify(inherited_background, require_io_boost=True)['status'], 'PASS')
        rows[-1]['requested_disk_policy'] = 5
        with self.assertRaises(ValueError):
            qualification.qualify(rows, require_io_boost=True)
        rows = records()
        rows[-1]['swapouts'] = 1
        with self.assertRaises(ValueError):
            qualification.qualify(rows)


if __name__ == '__main__':
    unittest.main()
