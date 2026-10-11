import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.storage.memory_storage import MemoryEventStorage  # noqa: E402
from tbgw.utils.queue import Full  # noqa: E402


class TestMemoryEventStorage(unittest.TestCase):
    def test_put_and_len(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        s.put({"a": 2})
        self.assertEqual(s.len(), 2)

    def test_get_event_pack_does_not_remove(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        s.put({"a": 2})
        pack = s.get_event_pack(10)
        self.assertEqual(len(pack), 2)
        self.assertEqual(s.len(), 2)  # not removed until processing_done()

    def test_event_pack_processing_done_removes_pack(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        s.put({"a": 2})
        s.get_event_pack(10)
        s.event_pack_processing_done()
        self.assertEqual(s.len(), 0)

    def test_repeated_get_event_pack_returns_same_pack(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        pack1 = s.get_event_pack(10)
        s.put({"a": 2})  # arrives after checkout -- must not appear in the pack yet
        pack2 = s.get_event_pack(10)
        self.assertEqual(pack1, pack2)
        self.assertEqual(len(pack2), 1)

    def test_discard_pending_pack_allows_retry(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        s.get_event_pack(10)
        s.discard_pending_pack()
        self.assertEqual(s.len(), 1)
        pack = s.get_event_pack(10)
        self.assertEqual(len(pack), 1)

    def test_drop_oldest_overflow_policy(self):
        s = MemoryEventStorage(maxsize=2, overflow_policy="drop_oldest")
        s.put({"id": 1})
        s.put({"id": 2})
        s.put({"id": 3})  # should evict id=1
        pack = s.get_event_pack(10)
        self.assertEqual([e["id"] for e in pack], [2, 3])

    def test_reject_new_overflow_policy(self):
        s = MemoryEventStorage(maxsize=2, overflow_policy="reject_new")
        s.put({"id": 1})
        s.put({"id": 2})
        with self.assertRaises(Full):
            s.put({"id": 3})

    def test_invalid_overflow_policy_rejected(self):
        with self.assertRaises(ValueError):
            MemoryEventStorage(maxsize=2, overflow_policy="not_a_real_policy")

    def test_clear(self):
        s = MemoryEventStorage(maxsize=10)
        s.put({"a": 1})
        s.get_event_pack(10)
        s.clear()
        self.assertEqual(s.len(), 0)


if __name__ == "__main__":
    unittest.main()
