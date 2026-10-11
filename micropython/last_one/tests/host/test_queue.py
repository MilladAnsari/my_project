import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.utils.queue import Queue, Empty, Full  # noqa: E402


class TestQueue(unittest.TestCase):
    def test_basic_put_get_fifo_order(self):
        q = Queue(maxsize=5)
        q.put("a")
        q.put("b")
        q.put("c")
        self.assertEqual(q.get(), "a")
        self.assertEqual(q.get(), "b")
        self.assertEqual(q.get(), "c")

    def test_qsize_empty(self):
        q = Queue(maxsize=5)
        self.assertTrue(q.empty())
        self.assertEqual(q.qsize(), 0)
        q.put(1)
        self.assertFalse(q.empty())
        self.assertEqual(q.qsize(), 1)

    def test_full_raises_when_not_blocking(self):
        q = Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)
        self.assertTrue(q.full())
        with self.assertRaises(Full):
            q.put_nowait(3)

    def test_empty_raises_when_not_blocking(self):
        q = Queue(maxsize=2)
        with self.assertRaises(Empty):
            q.get_nowait()

    def test_unbounded_queue(self):
        q = Queue(maxsize=0)
        for i in range(100):
            q.put_nowait(i)
        self.assertEqual(q.qsize(), 100)
        self.assertFalse(q.full())

    def test_get_batch(self):
        q = Queue(maxsize=10)
        for i in range(5):
            q.put_nowait(i)
        batch = q.get_batch(3)
        self.assertEqual(batch, [0, 1, 2])
        self.assertEqual(q.qsize(), 2)

    def test_get_batch_more_than_available(self):
        q = Queue(maxsize=10)
        q.put_nowait("x")
        batch = q.get_batch(5)
        self.assertEqual(batch, ["x"])
        self.assertEqual(q.qsize(), 0)

    def test_clear(self):
        q = Queue(maxsize=10)
        q.put_nowait(1)
        q.put_nowait(2)
        q.clear()
        self.assertEqual(q.qsize(), 0)

    def test_blocking_get_with_timeout_raises_empty(self):
        q = Queue(maxsize=10)
        with self.assertRaises(Empty):
            q.get(block=True, timeout=0.05)

    def test_blocking_put_with_timeout_raises_full(self):
        q = Queue(maxsize=1)
        q.put_nowait(1)
        with self.assertRaises(Full):
            q.put(2, block=True, timeout=0.05)


if __name__ == "__main__":
    unittest.main()
