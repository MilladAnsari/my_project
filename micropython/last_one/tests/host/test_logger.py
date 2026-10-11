import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tbgw.utils import logger as logger_module  # noqa: E402


class TestLogger(unittest.TestCase):
    def setUp(self):
        # Logger registry/sinks/global level are module-level state --
        # reset between tests so they don't bleed into each other.
        logger_module._registry.clear()
        logger_module._sinks.clear()
        logger_module.set_global_level("INFO")

    def test_get_logger_returns_same_instance(self):
        a = logger_module.get_logger("x")
        b = logger_module.get_logger("x")
        self.assertIs(a, b)

    def test_sink_receives_records_at_or_above_level(self):
        received = []
        logger_module.register_sink(lambda name, level, msg: received.append((name, level, msg)))
        log = logger_module.get_logger("test_module")
        log.info("hello %s", "world")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("test_module", "INFO", "hello world"))

    def test_records_below_level_are_filtered(self):
        received = []
        logger_module.register_sink(lambda name, level, msg: received.append((name, level, msg)))
        log = logger_module.get_logger("test_module")
        log.set_level("WARNING")
        log.info("should not appear")
        log.warning("should appear")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][2], "should appear")

    def test_configure_from_dict(self):
        logger_module.configure_from_dict({"default": "ERROR", "specific": "DEBUG"})
        self.assertEqual(logger_module._global_level, logger_module.LEVELS["ERROR"])
        self.assertEqual(
            logger_module.get_logger("specific").level, logger_module.LEVELS["DEBUG"]
        )

    def test_unregister_sink(self):
        received = []
        sink = logger_module.register_sink(lambda n, l, m: received.append(1))
        logger_module.unregister_sink(sink)
        logger_module.get_logger("test_module").error("boom")
        self.assertEqual(len(received), 0)

    def test_bad_format_args_do_not_raise(self):
        log = logger_module.get_logger("test_module")
        # Deliberately mismatched %-format placeholders -- must not raise.
        log.info("%s %s", "only_one_arg")


if __name__ == "__main__":
    unittest.main()
