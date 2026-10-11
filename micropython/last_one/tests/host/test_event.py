import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.utils.event import Event  # noqa: E402


class TestEvent(unittest.TestCase):
    def test_initially_unset(self):
        e = Event()
        self.assertFalse(e.is_set())

    def test_set_and_is_set(self):
        e = Event()
        e.set()
        self.assertTrue(e.is_set())

    def test_clear(self):
        e = Event()
        e.set()
        e.clear()
        self.assertFalse(e.is_set())

    def test_wait_returns_true_when_already_set(self):
        e = Event()
        e.set()
        self.assertTrue(e.wait(timeout=0.1))

    def test_wait_times_out_when_never_set(self):
        e = Event()
        self.assertFalse(e.wait(timeout=0.05))


if __name__ == "__main__":
    unittest.main()
