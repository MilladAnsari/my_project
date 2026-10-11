import unittest

from tbgw.core.wifi import WifiManager, _MockWlan, connect_with_retry


class TestWifiManagerFactory(unittest.TestCase):
    def test_create_falls_back_to_mock_on_host(self):
        # No 'network' module is importable on a desktop interpreter, so
        # create() must fall back to the mock rather than raising.
        wlan = WifiManager.create()
        self.assertIsInstance(wlan, _MockWlan)


class TestConnectWithRetry(unittest.TestCase):
    def test_succeeds_on_first_attempt(self):
        wlan = _MockWlan()
        ok = connect_with_retry(wlan, "ssid", "pw", timeout_s=1, max_attempts=3, backoff_s=0)
        self.assertTrue(ok)
        self.assertEqual(wlan.connect_calls, 1)

    def test_gives_up_after_max_attempts_when_network_never_comes_up(self):
        wlan = _MockWlan()
        wlan.should_connect = False
        ok = connect_with_retry(wlan, "ssid", "pw", timeout_s=0.05, max_attempts=3, backoff_s=0)
        self.assertFalse(ok)
        self.assertEqual(wlan.connect_calls, 3)

    def test_retries_forever_when_max_attempts_is_zero_until_it_connects(self):
        wlan = _MockWlan()
        wlan.should_connect = False

        # Flip it to succeed after a couple of failed attempts by
        # wrapping connect() -- simulates the network eventually coming
        # up mid-retry-loop.
        original_connect = wlan.connect

        def flaky_connect(ssid, password):
            original_connect(ssid, password)
            if wlan.connect_calls >= 3:
                wlan.should_connect = True

        wlan.connect = flaky_connect
        ok = connect_with_retry(wlan, "ssid", "pw", timeout_s=0.02, max_attempts=0, backoff_s=0)
        self.assertTrue(ok)
        self.assertGreaterEqual(wlan.connect_calls, 3)

    def test_connect_exception_does_not_abort_the_retry_loop(self):
        wlan = _MockWlan()

        def raising_connect(ssid, password):
            raise OSError("radio not ready")

        wlan.connect = raising_connect
        # should_connect stays True but connect() always raises before
        # ever setting _connected -- so this exercises the "connect()
        # itself raised" path specifically, not just a slow/failed join.
        ok = connect_with_retry(wlan, "ssid", "pw", timeout_s=0.02, max_attempts=2, backoff_s=0)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
