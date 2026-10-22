"""secret set, remove, migrate-networks and run are validation plus one call to the VM (D102). A fake Remote stands in
for the SSH call: what is sent, where the secret's value goes (stdin only), and how `run`'s trailer becomes the result."""
import json
import subprocess
import unittest
from unittest import mock

try:
    from annex_cli import ops
    from annex_cli.remote import AnnexError, Remote
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


class Calls(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def call(remote, *args, input=None, progress=None, silent=180):
            self.calls.append((args, input))
            return {"ok": True, "set": ["API_KEY"], "applied": "restarted with new values"}
        for p in (mock.patch.object(Remote, "__init__", lambda self, *a, **k: None),
                  mock.patch.object(Remote, "call", call),
                  mock.patch.object(ops, "_who", return_value="kim@mac")):
            p.start()
            self.addCleanup(p.stop)

    def test_secret_set_is_one_call_and_the_value_travels_on_stdin_only(self):
        res = ops.secret_set("demo", ["API_KEY=s3cr3t value"])
        [(args, stdin)] = self.calls
        self.assertEqual(args, ("secret-set", "demo", "--actor", "kim@mac", "--recreate"))
        self.assertEqual(stdin, "API_KEY=s3cr3t value\n")
        self.assertNotIn("s3cr3t", " ".join(args))
        self.assertEqual(res["applied"], "restarted with new values")

    def test_secret_set_checks_the_key_before_calling(self):
        with self.assertRaises(AnnexError):
            ops.secret_set("demo", ["lower=1"])
        self.assertEqual(self.calls, [])

    def test_remove_and_migrate_networks_are_one_call_each(self):
        ops.remove("demo")
        ops.remove("demo", keep_data=True)
        ops.migrate_networks("demo")
        ops.migrate_networks("demo", egress=True)
        self.assertEqual([a for a, _ in self.calls], [
            ("remove", "demo", "--actor", "kim@mac"),
            ("remove", "demo", "--actor", "kim@mac", "--keep-data"),
            ("migrate-networks", "demo", "--actor", "kim@mac"),
            ("migrate-networks", "demo", "--actor", "kim@mac", "--egress")])


class RunJob(unittest.TestCase):
    def run_job(self, stdout=b"", stderr=b"", rc=0):
        sent = []

        def run(remote, script, **kw):
            sent.append(script)
            return subprocess.CompletedProcess([], rc, stdout, stderr)
        with mock.patch.object(Remote, "__init__", lambda self, *a, **k: None), \
                mock.patch.object(Remote, "vm_command", lambda self, *a: "annex_vm.py " + " ".join(a)), \
                mock.patch.object(Remote, "run", run):
            try:
                return ops.run_job("demo", "work"), sent
            except AnnexError as e:
                return e, sent

    def test_asks_the_vm_for_the_job_by_name_and_returns_the_result_shape(self):
        trailer = json.dumps({"ok": True, "exit_code": 3, "seconds": 1.5}).encode() + b"\n"
        res, sent = self.run_job(b"did it\n", trailer, rc=3)
        self.assertEqual(sent, ["annex_vm.py run-job demo work"])  # no command from this side
        self.assertEqual(res, {"app": "demo", "job": "work", "exit_code": 3, "seconds": 1.5, "output": "did it\n"})

    def test_the_output_is_cut_to_its_last_8000_characters(self):
        res, _ = self.run_job(b"x" * 9000 + b"END", b'{"ok": true, "exit_code": 0, "seconds": 0.1}\n')
        self.assertEqual(len(res["output"]), 8000)
        self.assertTrue(res["output"].endswith("END"))

    def test_a_refusal_from_the_vm_becomes_the_error(self):
        err, _ = self.run_job(b"", b'{"ok": false, "error": "demo has no job \'work\'. Jobs: tick (schedule in annex.yaml)"}\n', rc=1)
        self.assertIsInstance(err, AnnexError)
        self.assertIn("Jobs: tick", str(err))

    def test_jobs_own_json_on_stdout_is_not_mistaken_for_the_trailer(self):
        res, _ = self.run_job(b'{"ok": false, "error": "from the job"}\n', b'{"ok": true, "exit_code": 0, "seconds": 2}\n')
        self.assertEqual(res["exit_code"], 0)

    def test_no_trailer_names_the_failure_and_an_old_platform(self):
        err, _ = self.run_job(b"", b"usage: annex_vm.py\nannex_vm.py: error: argument cmd: invalid choice: 'run-job'\n", rc=2)
        self.assertIsInstance(err, AnnexError)
        self.assertIn("scripts/push.sh", str(err))


class Detached(unittest.TestCase):
    def test_a_dropped_secret_set_does_not_point_at_releases(self):
        msg = Remote._detached("secret-set", "demo", "provision", "the connection to the VM dropped")
        self.assertIn("secret set continues on the VM", msg)
        self.assertNotIn("annex releases", msg)

    def test_a_deploys_message_is_unchanged(self):
        self.assertIn("annex releases demo", Remote._detached("deploy", "demo", "gate", "x"))


if __name__ == "__main__":
    unittest.main()
