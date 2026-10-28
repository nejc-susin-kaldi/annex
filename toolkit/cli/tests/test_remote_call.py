"""Remote.call: the JSON-line protocol of annex_vm.py deploy|rollback (D101), against a fake VM process (a Python
script in place of ssh). Progress order, the final object, a refusal, a connection that drops or goes silent, Ctrl-C,
and stdin delivered whole (or refused unread)."""
import hashlib
import sys
import time
import unittest

try:
    from annex_cli.remote import AnnexError, Remote
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")


def remote(script: str) -> Remote:
    r = Remote.__new__(Remote)
    r.host, r.data = "annex", "/srv/annex"
    r.ssh = [sys.executable, "-c", "import json, sys, time, hashlib\n"
             "def say(**obj):\n    print(json.dumps(obj), flush=True)\n" + script]
    return r


class Call(unittest.TestCase):
    def call(self, script, *, input=b"", silent=180, progress=None):
        seen = []
        res = remote(script).call("deploy", "demo", "--actor", "kim", input=input, silent=silent,
                                  progress=progress or seen.append)
        return res, seen

    def test_progress_and_notes_in_order_and_the_final_object(self):
        res, seen = self.call(
            'say(note="r4 of demo was interrupted at gate")\n'
            'say(progress="Uploading source", step="upload")\n'
            'say(heartbeat="upload")\n'
            'say(progress="Building r5 on the VM", step="build")\n'
            'say(ok=True, release=5, warnings=[])\n')
        self.assertEqual(seen, ["r4 of demo was interrupted at gate", "Uploading source", "Building r5 on the VM"])
        self.assertEqual(res, {"ok": True, "release": 5, "warnings": []})

    def test_a_failure_is_an_annex_error_with_its_text(self):
        with self.assertRaises(AnnexError) as cm:
            self.call('say(progress="Building", step="build")\nsay(ok=False, error="Docker build failed for demo.", step="build")\n')
        self.assertEqual(str(cm.exception), "Docker build failed for demo.")

    def test_lines_that_are_not_json_are_ignored(self):
        res, _ = self.call('print("Warning: Permanently added annex", flush=True)\nsay(ok=True, release=1)\n')
        self.assertEqual(res["release"], 1)

    def test_a_dropped_connection_names_the_last_step_and_says_the_deploy_continues(self):
        with self.assertRaises(AnnexError) as cm:
            self.call('say(progress="Building", step="build")\nsay(progress="Health gate", step="gate")\nsys.exit(255)\n')
        msg = str(cm.exception)
        self.assertIn("dropped at the gate step", msg)
        self.assertIn("the deploy continues on the VM", msg)
        self.assertIn("annex status demo", msg)
        self.assertIn("annex releases demo", msg)

    def test_a_silent_connection_is_cut_and_detached(self):
        t0 = time.time()
        with self.assertRaises(AnnexError) as cm:
            self.call('say(progress="Building", step="build")\ntime.sleep(30)\n', silent=1)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("sent nothing for 1 s", str(cm.exception))
        self.assertIn("continues on the VM", str(cm.exception))

    def test_ctrl_c_detaches(self):
        def progress(msg):
            if msg == "Health gate":
                raise KeyboardInterrupt
        with self.assertRaises(AnnexError) as cm:
            self.call('say(progress="Building", step="build")\nsay(progress="Health gate", step="gate")\n'
                      'time.sleep(30)\n', progress=progress)
        self.assertIn("dropped at the gate step (interrupted); the deploy continues on the VM", str(cm.exception))

    def test_stdin_is_delivered_whole(self):
        body = b"{}\n" + bytes(range(256)) * 12000  # 3 MB: more than a pipe holds
        res, _ = self.call('data = sys.stdin.buffer.read()\n'
                           'say(ok=True, n=len(data), sha=hashlib.sha256(data).hexdigest())\n', input=body)
        self.assertEqual((res["n"], res["sha"]), (len(body), hashlib.sha256(body).hexdigest()))

    def test_a_refusal_before_stdin_is_read_is_reported(self):
        with self.assertRaises(AnnexError) as cm:
            self.call('say(ok=False, error="demo is being deployed or rolled back by kim@mac")\n',
                      input=b"x" * 5_000_000)
        self.assertIn("is being deployed or rolled back by kim@mac", str(cm.exception))

    def test_a_command_that_never_started_says_what_the_vm_said(self):
        with self.assertRaises(AnnexError) as cm:
            self.call('sys.stderr.write("annex_vm: error: argument cmd: invalid choice: \'deploy\'\\n")\nsys.exit(2)\n')
        msg = str(cm.exception)
        self.assertIn("invalid choice", msg)
        self.assertIn("run scripts/push.sh first", msg)

    def test_the_command_runs_annex_vm_under_bash(self):
        r = remote("")
        argv = r._argv("python3 /srv/annex/platform/vm/annex_vm.py deploy demo")
        self.assertEqual(argv[:-1], r.ssh)
        self.assertEqual(argv[-1], "bash -c 'python3 /srv/annex/platform/vm/annex_vm.py deploy demo'")


class DeployState(unittest.TestCase):
    def test_the_status_line_for_a_running_and_a_finished_run(self):
        from annex_cli import ops
        cur = {"kind": "deploy", "release": 5, "step": "gate", "actor": "kim@mac", "pid": 4242,
               "started": "2026-10-22T11:00:00+0200"}
        running = ops.deploy_state_text({"running": True, "current": cur}, "demo")
        self.assertIn("deploy of r5 running on the VM at step gate", running)
        self.assertIn("pid 4242", running)
        done = ops.deploy_state_text({"running": False, "current": {**cur, "result": "live"}}, "demo")
        self.assertIn("none running; the last was a deploy of r5", done)
        self.assertTrue(done.endswith(": live"))
        self.assertIsNone(ops.deploy_state_text({"running": False, "current": None}, "demo"))


if __name__ == "__main__":
    unittest.main()
