import json
import os
import unittest


REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class TestPymakrProjectConfig(unittest.TestCase):
    def test_upload_ignores_development_only_directories(self):
        with open(os.path.join(REPO_ROOT, "pymakr.conf")) as handle:
            config = json.load(handle)

        ignored = set(config["py_ignore"])
        self.assertTrue({"build", "docs", "tests", ".git"}.issubset(ignored))
        self.assertNotIn("src", ignored)
        self.assertNotIn("config", ignored)
        self.assertFalse(config["reboot_after_upload"])

    def test_root_main_launches_the_production_source_tree(self):
        with open(os.path.join(REPO_ROOT, "main.py")) as handle:
            launcher = handle.read()
        self.assertIn('sys.path.insert(0, "src")', launcher)
        self.assertIn('open("src/main.py")', launcher)


if __name__ == "__main__":
    unittest.main()
