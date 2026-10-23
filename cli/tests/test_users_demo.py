"""`annex users add --demo` / `reset --demo` (D107): the flag reaches the VM script, and the CLI says where the password went."""
import contextlib
import io
import unittest
from unittest import mock

try:
    from annex_cli import main as cli
    from annex_cli import ops
    from annex_cli.remote import Remote
except ImportError as e:  # the CLI's dependencies aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class Demo(unittest.TestCase):
    def run_cli(self, argv, reply):
        sent = []

        def vm(remote, *args):
            sent.append(args)
            return reply
        out = io.StringIO()
        with mock.patch.object(Remote, "__init__", lambda self, *a, **k: None), \
                mock.patch.object(Remote, "vm", vm), mock.patch.object(ops, "_who", return_value="kim@mac"), \
                contextlib.redirect_stdout(out):
            cli.main(argv)
        return sent, out.getvalue()

    def test_add_demo_passes_the_flag_and_reports_users_txt(self):
        reply = {"ok": True, "user": {"login": "maja", "name": "Maja Horvat", "email": "maja@goat-arcade.example",
                                      "groups": ["users"], "disabled": False},
                 "password": "pw-123", "demo_password_stored": True}
        sent, out = self.run_cli(["users", "add", "maja", "--name", "Maja Horvat", "--email",
                                  "maja@goat-arcade.example", "--demo"], reply)
        [args] = sent
        self.assertEqual(args[:4], ("users-add", "maja", "--actor", "kim@mac"))
        self.assertIn("--demo", args)
        self.assertNotIn("pw-123", " ".join(args))
        self.assertIn("pw-123", out)  # printed once, for the human
        self.assertIn("secrets/users.txt", out)

    def test_plain_add_has_no_demo_flag(self):
        reply = {"ok": True, "user": {"login": "x1", "name": "X", "email": "x@y.com", "groups": ["users"],
                                      "disabled": False}, "invited": "x@y.com"}
        sent, _ = self.run_cli(["users", "add", "x1", "--name", "X", "--email", "x@y.com"], reply)
        self.assertNotIn("--demo", sent[0])

    def test_reset_demo(self):
        sent, out = self.run_cli(["users", "reset", "sam", "--demo"],
                                 {"ok": True, "login": "sam", "password": "pw-9", "demo_password_updated": True})
        self.assertIn("--demo", sent[0])
        self.assertIn("pw-9", out)


if __name__ == "__main__":
    unittest.main()
