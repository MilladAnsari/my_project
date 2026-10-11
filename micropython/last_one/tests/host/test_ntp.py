import unittest

from tbgw.core import ntp


class _FakeNtptime:
    def __init__(self, fail_times=0):
        self.host = None
        self.calls = 0
        self._fail_times = fail_times

    def settime(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise OSError("[Errno 110] ETIMEDOUT")


class TestNtpSync(unittest.TestCase):
    def setUp(self):
        # Each test restores this in tearDown -- ntp.py's module-level
        # `ntptime` reference (and `_synced` flag) would otherwise leak
        # between tests.
        self._original_ntptime = ntp.ntptime
        self._original_synced = ntp._synced
        ntp._synced = False

    def tearDown(self):
        ntp.ntptime = self._original_ntptime
        ntp._synced = self._original_synced

    def test_sync_succeeds_and_sets_is_synced(self):
        ntp.ntptime = _FakeNtptime()
        self.assertFalse(ntp.is_synced())
        ok = ntp.sync()
        self.assertTrue(ok)
        self.assertTrue(ntp.is_synced())

    def test_sync_retries_then_succeeds(self):
        fake = _FakeNtptime(fail_times=2)
        ntp.ntptime = fake
        ok = ntp.sync(retries=3)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, 3)

    def test_sync_gives_up_after_retries_exhausted(self):
        fake = _FakeNtptime(fail_times=99)
        ntp.ntptime = fake
        ok = ntp.sync(retries=2)
        self.assertFalse(ok)
        self.assertFalse(ntp.is_synced())
        self.assertEqual(fake.calls, 2)

    def test_sync_without_ntptime_module_returns_false_not_raises(self):
        ntp.ntptime = None
        ok = ntp.sync()
        self.assertFalse(ok)
        self.assertFalse(ntp.is_synced())

    def test_sync_sets_custom_server_host(self):
        fake = _FakeNtptime()
        ntp.ntptime = fake
        ntp.sync(server="pool.ntp.org")
        self.assertEqual(fake.host, "pool.ntp.org")


class TestUnixTimeMsEpochCorrection(unittest.TestCase):
    """tb_utility.unix_time_ms() lives in a different module, but its
    whole reason for existing is to pair with tbgw.core.ntp -- covered
    here for that reason, alongside test_tb_utility.py's own coverage.
    """

    def test_host_cpython_time_is_already_unix_epoch(self):
        from tbgw.utils import tb_utility
        import time as real_time

        ms = tb_utility.unix_time_ms()
        # Should be within a couple seconds of the real wall clock --
        # confirms no spurious MicroPython-epoch offset gets applied on
        # a host where time.gmtime(0) is already 1970.
        self.assertAlmostEqual(ms / 1000.0, real_time.time(), delta=2)


if __name__ == "__main__":
    unittest.main()
