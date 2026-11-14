"""Check the command-line boundary shared with DarwinBoot."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'scripts'))


from pathlib import Path
import sys
import unittest

from unixnu.payload import check_args
from unixnu.util import Error


class CommandLineTests(unittest.TestCase):
    def test_1023_ascii_bytes_leave_room_for_nul(self):
        self.assertEqual(len(check_args(['probe=' + 'x' * 1017])), 1023)

    def test_spaces_count_toward_the_buffer(self):
        with self.assertRaisesRegex(Error, '1024 bytes'):
            check_args(['x' * 512, 'y' * 511])

    def test_1024_bytes_are_rejected(self):
        with self.assertRaisesRegex(Error, 'limit is 1023'):
            check_args(['probe=' + 'x' * 1018])

    def test_non_ascii_is_rejected(self):
        with self.assertRaisesRegex(Error, 'unsupported kernel argument'):
            check_args(['probe=非ASCII'])


if __name__ == '__main__':
    unittest.main()
