"""The detach (D101): the worker is a forked process in its own session that keeps the lock; the parent forwards
run.log to the caller. A caller that goes away doesn't stop the worker; a worker that dies leaves its release pending,
the lock free and a final line that says so. Real fork, real files; jobs are plain functions.
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from deploy_fakes import SETTINGS, dep, vm

# kills the worker once it has written its own pid to current.json (the parent's pid is there before the fork)
KILLER = """
import json, os, sys, time
path, parent, sig = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for _ in range(300):
    try:
        pid = json.load(open(path)).get("pid")
    except Exception:
        pid = None
    if pid and pid != parent:
        time.sleep(0.3)
        os.kill(pid, sig)
        break
    time.sleep(0.05)
"""


class Gone(io.StringIO):
    """A caller that reads `keep` lines and then goes away (its pipe is closed)."""

    def __init__(self, keep: int):
        super().__init__()
        self.keep = keep

    def write(self, s):
        if self.keep <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.keep -= 1
        return super().write(s)


class Detach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for p in (unittest.mock.patch.object(dep, "HEARTBEAT_S", 0.2),
                  unittest.mock.patch.object(dep, "POLL_S", 0.02),
                  unittest.mock.patch.object(vm, "record_release_db", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def start(self, out):
        fd = vm.hold_app_lock(self.base, "demo", tries=1)
        run = dep.Run(self.base, "demo", "deploy", "kim@mac", "cli", {}, out, settings=dict(SETTINGS))
        self.addCleanup(run.log.close)
        run.open({"deployed_at": "2026-10-22T11:00:00+02:00", "deployed_by": "kim@mac", "via": "cli"}, None)
        run.step("build")
        return run, fd

    def log_lines(self):
        return [json.loads(x) for x in (self.base / "deploy" / "demo" / "run.log").read_text().splitlines()]

    def wait_free(self, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            if not vm.lock_held(self.base, "demo"):
                return True
            time.sleep(0.05)
        return False

    def killer(self, sig):
        p = subprocess.Popen([sys.executable, "-c", KILLER, str(vm.current_path(self.base, "demo")), str(os.getpid()),
                              str(int(sig))])
        self.addCleanup(p.wait)
        return p

    def test_progress_heartbeats_and_the_result_reach_the_caller_and_run_log(self):
        out = io.StringIO()
        run, fd = self.start(out)

        def job():
            run.progress("Building r1 on the VM")
            time.sleep(0.6)  # silent: the parent sends heartbeats
            run.progress("Done")
            return {"release": run.n}
        self.assertEqual(dep.detach(run, fd, job), 0)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"progress": "Building r1 on the VM", "step": "build"})
        self.assertIn({"heartbeat": "build"}, lines)
        self.assertEqual(lines[-1], {"ok": True, "release": 1})
        self.assertEqual(self.log_lines()[-1], {"ok": True, "release": 1})
        self.assertTrue(self.wait_free())
        worker = vm.read_current(self.base, "demo")["pid"]
        self.assertNotEqual(worker, os.getpid())  # the worker said who it is

    def test_a_caller_that_leaves_does_not_stop_the_worker(self):
        out = Gone(keep=1)
        run, fd = self.start(out)

        def job():
            run.progress("Building r1 on the VM")
            time.sleep(0.8)
            run.progress("Done")
            return {"release": run.n}
        t0 = time.time()
        self.assertEqual(dep.detach(run, fd, job), 0)
        self.assertLess(time.time() - t0, 0.8)  # the parent left as soon as its caller had
        self.assertTrue(vm.lock_held(self.base, "demo"))  # the worker holds the lock, alone
        self.assertTrue(self.wait_free())
        self.assertEqual(self.log_lines()[-1], {"ok": True, "release": 1})

    def test_a_killed_worker_ends_without_a_result_leaves_the_release_pending_and_the_lock_free(self):
        out = io.StringIO()
        run, fd = self.start(out)
        self.killer(signal.SIGKILL)

        def job():
            run.step("gate")
            time.sleep(30)
            return {}
        self.assertEqual(dep.detach(run, fd, job), 1)
        last = json.loads(out.getvalue().splitlines()[-1])
        self.assertFalse(last["ok"])
        self.assertIn("ended at the gate step without a result (killed?); r1 of demo is pending", last["error"])
        self.assertTrue(self.wait_free(2))
        self.assertEqual(vm.read_releases(self.base, "demo")[-1]["result"], "pending")

    def test_sigterm_records_the_release_failed_at_its_step(self):
        out = io.StringIO()
        run, fd = self.start(out)
        self.killer(signal.SIGTERM)

        def job():
            run.step("gate")
            time.sleep(30)
            return {}
        self.assertEqual(dep.detach(run, fd, job), 1)
        last = json.loads(out.getvalue().splitlines()[-1])
        self.assertIn("stopped (SIGTERM) at the gate step", last["error"])
        rel = vm.read_releases(self.base, "demo")[-1]
        self.assertEqual((rel["result"], rel["interrupted_at"]), ("failed", "gate"))
        self.assertTrue(self.wait_free())

    def test_the_worker_is_in_its_own_session(self):
        out = io.StringIO()
        run, fd = self.start(out)

        def job():
            return {"sid": os.getsid(0), "pid": os.getpid()}
        dep.detach(run, fd, job)
        res = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(res["sid"], res["pid"])  # a session leader: the SSH session's hangup can't reach it
        self.assertNotEqual(res["sid"], os.getsid(0))


class Command(unittest.TestCase):
    """annex_vm.py deploy as a process: a refusal before the lock is one {"ok": false} line (annex_deploy's Fail is
    the Fail annex_vm's main() catches, though annex_vm runs as __main__)."""
    VM = str(Path(__file__).resolve().parents[1] / "annex_vm.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "platform").mkdir()
        (self.base / "platform" / ".env").write_text("".join(f"{k}={v}\n" for k, v in SETTINGS.items()))

    def call(self, *args, stdin=b""):
        r = subprocess.run([sys.executable, self.VM, *args], input=stdin, capture_output=True,
                           env={**os.environ, "ANNEX_DATA_DIR": str(self.base)}, timeout=30)
        return r.returncode, [json.loads(x) for x in r.stdout.decode().splitlines()], r.stderr.decode()

    def test_bad_meta_is_one_refusal_line(self):
        rc, lines, err = self.call("deploy", "demo", "--actor", "kim", stdin=b"not json\n")
        self.assertEqual(rc, 1, err)
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["ok"])
        self.assertIn("JSON meta", lines[0]["error"])

    def test_a_held_lock_is_one_refusal_line_naming_the_holder(self):
        vm.guard_path(self.base, "demo").parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                                   "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
                                   "print('held', flush=True)\ntime.sleep(30)", str(vm.guard_path(self.base, "demo"))],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        vm.write_current(self.base, "demo", {"pid": holder.pid, "actor": "racer-one", "step": "build", "release": 3,
                                             "started_at": time.time()})
        rc, lines, err = self.call("rollback", "demo", "--actor", "racer-two", stdin=b"{}\n")
        self.assertEqual((rc, len(lines)), (1, 1), err)
        self.assertIn("demo is being deployed or rolled back by racer-one (r3, at step build", lines[0]["error"])
        self.assertIn(f"pid {holder.pid} on the VM", lines[0]["error"])

    def test_deploy_state_from_the_command_line(self):
        rc, lines, err = self.call("deploy-state", "demo")
        self.assertEqual((rc, lines), (0, [{"ok": True, "running": False, "current": None}]), err)


if __name__ == "__main__":
    unittest.main()
