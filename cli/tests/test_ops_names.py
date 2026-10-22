"""App names are checked before anything reaches the VM (D79): a name from an agent must never reach a shell."""
import unittest
from unittest import mock

try:
    from annex_cli import ops
    from annex_cli.remote import AnnexError, Remote
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")

BAD = ["x; id", "../etc", "Console", "console", "a\n", "x$(id)", "", "a b"]

CALLS = {
    "status": lambda a: ops.status(a),
    "logs": lambda a: ops.logs(a),
    "stored_logs": lambda a: ops.stored_logs(a),
    "follow_logs": lambda a: ops.follow_logs(a),
    "run_job": lambda a: ops.run_job(a, "monthly"),
    "secret_set": lambda a: ops.secret_set(a, ["KEY=v"]),
    "remove": lambda a: ops.remove(a),
    "releases": lambda a: ops.releases(a),
    "rollback": lambda a: ops.rollback(a),
    "_rollback": lambda a: ops._rollback(a),
    "usage": lambda a: ops.usage(a),
    "errors": lambda a: ops.errors(a),
    "audit": lambda a: ops.audit(a),
    "sends": lambda a: ops.sends(a),
    "hooks": lambda a: ops.hooks(a),
    "hooks_replay": lambda a: ops.hooks_replay(a, failed=True),
    "_record": lambda a: ops._record(mock.Mock(), a),
    "_releases": lambda a: ops._releases(mock.Mock(), a),
    "_Lock": lambda a: ops._Lock(mock.Mock(), a, {}, None),
    "_running_image": lambda a: ops._running_image(mock.Mock(), a),
    "_images": lambda a: ops._images(mock.Mock(), a),
    "_containers": lambda a: ops._containers(mock.Mock(), a),
    "_maintenance": lambda a: ops._maintenance(mock.Mock(), a, 5),
    "container": lambda a: ops.container(a),
    "project": lambda a: ops.project(a),
    "app_network": lambda a: ops.app_network(a),
    "egress_network": lambda a: ops.egress_network(a),
    "migrate_networks": lambda a: ops.migrate_networks(a),
}


class AppNames(unittest.TestCase):
    def test_bad_names_are_refused_before_any_remote_call(self):
        boom = AssertionError("a bad name reached the VM")
        with mock.patch.object(Remote, "__init__", side_effect=boom), \
                mock.patch.object(Remote, "run", side_effect=boom), \
                mock.patch.object(Remote, "vm", side_effect=boom), \
                mock.patch.object(Remote, "stream", side_effect=boom):
            for fn, call in CALLS.items():
                for name in BAD:
                    with self.subTest(fn=fn, name=name), self.assertRaises(AnnexError):
                        call(name)

    def test_the_message_says_how_to_fix_it(self):
        with self.assertRaises(AnnexError) as cm:
            ops.status("x; id")
        self.assertIn("lowercase letters, digits and '-'", str(cm.exception))

    def test_bad_job_and_key_are_refused(self):
        with mock.patch.object(Remote, "__init__", side_effect=AssertionError("reached the VM")):
            with self.assertRaises(AnnexError):
                ops.run_job("good-app", "x; id")
            with self.assertRaises(AnnexError):
                ops.secret_set("good-app", ["KEY;id=v"])
            with self.assertRaises(AnnexError):
                ops.secret_set("good-app", ["KEY\n=v"])

    def test_good_names_pass(self):
        self.assertEqual(ops.container("sales-dash"), "annex-app-sales-dash")
        self.assertEqual(ops.project("ab"), "annex-app-ab")

    def test_remote_path_quotes(self):
        r = Remote.__new__(Remote)
        r.data = "/srv/annex"
        self.assertEqual(r.path("deploy", "x"), "/srv/annex/deploy/x")
        self.assertEqual(r.path("deploy", "x; id"), "'/srv/annex/deploy/x; id'")


if __name__ == "__main__":
    unittest.main()
