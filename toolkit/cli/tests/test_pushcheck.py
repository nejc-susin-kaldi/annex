"""scripts/pushcheck.py: the push stamp decision (pure) and its parser."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # the repo
spec = importlib.util.spec_from_file_location("pushcheck", ROOT / "scripts" / "pushcheck.py")
pushcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pushcheck)

LOCAL = {"commit": "b" * 40, "dirty": "false"}
VM = {"commit": "a" * 40, "dirty": "false"}


class DecideTest(unittest.TestCase):
    def test_first_push_has_no_stamp(self):
        self.assertTrue(pushcheck.decide(None, LOCAL, False, False, False)[0])
        self.assertTrue(pushcheck.decide({}, LOCAL, False, False, False)[0])

    def test_ancestor_proceeds_and_prints_both(self):
        ok, msg = pushcheck.decide(VM, LOCAL, True, True, False)
        self.assertTrue(ok)
        self.assertIn("aaaaaaaaaaaa", msg)
        self.assertIn("bbbbbbbbbbbb", msg)

    def test_same_commit_proceeds(self):
        self.assertTrue(pushcheck.decide(LOCAL, LOCAL, True, True, False)[0])

    def test_not_an_ancestor_refuses_and_force_overrides(self):
        ok, msg = pushcheck.decide(VM, LOCAL, True, False, False)
        self.assertFalse(ok)
        self.assertIn("--force", msg)
        self.assertTrue(pushcheck.decide(VM, LOCAL, True, False, True)[0])

    def test_unknown_commit_refuses_with_fetch_hint(self):
        ok, msg = pushcheck.decide(VM, LOCAL, False, False, False)
        self.assertFalse(ok)
        self.assertIn("git fetch", msg)
        self.assertTrue(pushcheck.decide(VM, LOCAL, False, False, True)[0])

    def test_dirty_is_shown(self):
        self.assertIn("(dirty)", pushcheck.decide({**VM, "dirty": "true"}, LOCAL, True, True, False)[1])

    def test_parse(self):
        self.assertEqual(pushcheck.parse("commit=abc\ndirty=true\n# x\nenv_sha256=ff\n"),
                         {"commit": "abc", "dirty": "true", "env_sha256": "ff"})


if __name__ == "__main__":
    unittest.main()
