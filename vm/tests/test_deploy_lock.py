"""The deploy lock and pending release (C1, D92; the flock and current.json of D101): file operations on a temp data
dir. Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402

T0 = 1_760_000_000.0
LIVE4 = {"release": 4, "image": "annex-app/x:r4", "result": "live", "deployed_at": "2026-10-22T10:00:00+0200",
         "deployed_by": "sam@mac", "manifest": {"name": "x"}}
LINE = {"manifest": {"name": "x"}, "deployed_at": "2026-10-22T11:00:00+0200", "deployed_by": "kim@mac", "via": "cli"}
RECORD = {"name": "x", "type": "web", "schedule": [], "url": "https://x.annex.home.arpa"}

HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(60)
"""


def state(image, status="running", health="healthy"):
    return {"image": image, "status": status, "health": health}


def current(actor="kim@mac", step="begin", started=T0):
    return {"pid": 4242, "actor": actor, "via": "cli", "kind": "deploy", "release": None, "image": None, "step": step,
            "step_at": started, "started_at": started, "started": vm._iso(started)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def history(self, *lines):
        vm.write_releases(self.base, "x", list(lines))

    def lines(self):
        return vm.read_releases(self.base, "x")

    def begin(self, cur=None, inspect=lambda app, m: None, wait=0, line=None, record=RECORD):
        return vm.begin(self.base, "x", dict(line or LINE), dict(record) if record else None, cur or current(),
                        inspect=inspect, wait=wait, sleep=lambda s: None)

    def died_at(self, step, cur=None):
        """A run that numbered its release, reached `step` and died (its current.json says where)."""
        cur = cur or current()
        res = self.begin(cur)
        cur.update(step=step)
        vm.write_current(self.base, "x", cur)
        return res

    def hold_elsewhere(self):
        """The app's lock, held by another process (as a running deploy holds it)."""
        vm.guard_path(self.base, "x").parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(vm.guard_path(self.base, "x"))],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        return p


class Lock(Base):
    def test_the_lock_is_an_flock_another_process_holds(self):
        self.hold_elsewhere()
        vm.write_current(self.base, "x", {**current(step="build"), "release": 7, "started_at": time.time() - 90})
        with self.assertRaises(vm.LockHeld) as cm:
            vm.hold_app_lock(self.base, "x", tries=1)
        msg = str(cm.exception)
        for part in ("x is being deployed or rolled back by kim@mac", "r7", "at step build", "1 min 30 s ago",
                     "pid 4242 on the VM", "finishes even if its terminal closed"):
            self.assertIn(part, msg)
        self.assertEqual(cm.exception.holder["step"], "build")
        self.assertTrue(vm.lock_held(self.base, "x"))

    def test_a_held_lock_without_current_json_still_refuses(self):
        self.hold_elsewhere()
        with self.assertRaises(vm.LockHeld) as cm:
            vm.hold_app_lock(self.base, "x", tries=1)
        self.assertIn("no current.json", str(cm.exception))

    def test_the_lock_dies_with_its_process(self):
        p = self.hold_elsewhere()
        p.kill()
        p.wait()
        os.close(vm.hold_app_lock(self.base, "x", tries=1))
        self.assertFalse(vm.lock_held(self.base, "x"))

    def test_the_probe_does_not_take_the_lock(self):
        self.assertFalse(vm.lock_held(self.base, "x"))  # no guard file yet
        fd = vm.hold_app_lock(self.base, "x", tries=1)
        self.assertTrue(vm.lock_held(self.base, "x"))
        os.close(fd)
        self.assertFalse(vm.lock_held(self.base, "x"))
        os.close(vm.hold_app_lock(self.base, "x", tries=1))  # and it is free again

    def test_current_json_rewrites_leave_no_temp_files(self):
        cur = current()
        for step in ("begin", "upload", "build", "gate", "finish"):
            cur.update(step=step)
            vm.write_current(self.base, "x", cur)
        self.assertEqual(vm.read_current(self.base, "x")["step"], "finish")
        self.assertEqual(sorted(p.name for p in (self.base / "deploy" / "x").iterdir()), ["current.json"])


class Begin(Base):
    def test_begin_numbers_the_release_and_appends_a_pending_line(self):
        self.history(LIVE4)
        cur = current()
        res = self.begin(cur)
        self.assertEqual((res["release"], res["image"]), (5, "annex-app/x:r5"))
        last = self.lines()[-1]
        self.assertEqual((last["release"], last["result"], last["deployed_by"]), (5, "pending", "kim@mac"))
        self.assertEqual((last["record"]["release"], last["record"]["image"]), (5, "annex-app/x:r5"))
        held = vm.read_current(self.base, "x")
        self.assertEqual((held["actor"], held["release"], held["image"]), ("kim@mac", 5, "annex-app/x:r5"))
        self.assertEqual(cur["release"], 5)  # the caller's dict is the one kept up to date

    def test_first_release_is_r1(self):
        self.assertEqual(self.begin()["release"], 1)

    def test_a_rollback_keeps_its_targets_image(self):
        self.history(LIVE4)
        res = self.begin(line={**LINE, "image": "annex-app/x:r2"}, record=None)
        self.assertEqual((res["release"], res["image"]), (5, "annex-app/x:r2"))
        self.assertNotIn("record", self.lines()[-1])

    def test_an_old_lock_directory_is_removed_with_a_note(self):
        old = vm.old_lock_dir(self.base, "x")
        old.mkdir(parents=True)
        (old / "owner.json").write_text(json.dumps({"actor": "sam@mac", "token": "ab" * 16, "step": "gate",
                                                    "release": 5, "started_at": T0}))
        self.history(LIVE4, {**LINE, "release": 5, "image": "annex-app/x:r5", "result": "pending"})
        res = self.begin(inspect=lambda app, m: state("annex-app/x:r5"))
        self.assertFalse(old.exists())
        self.assertIn("lock directory from before deploys ran on the VM (held by sam@mac, r5, at step gate)",
                      res["notes"][0])
        # its owner file still says where the dead run stopped
        self.assertIn("interrupted at gate", res["reconciled"][0]["message"])
        self.assertEqual(res["release"], 6)


class End(Base):
    def test_end_rewrites_the_pending_line(self):
        self.history(LIVE4)
        res = self.begin()
        final = {**LINE, "image": res["image"], "snapshot": "pre-r5", "result": "live"}
        out = vm.end(self.base, "x", 5, "live", final)
        self.assertEqual(out["result"], "live")
        lines = self.lines()
        self.assertEqual([(x["release"], x["result"]) for x in lines], [(4, "live"), (5, "live")])
        self.assertEqual(lines[1]["snapshot"], "pre-r5")
        self.assertNotIn("record", lines[1])
        self.assertEqual(self.begin()["release"], 6)  # numbers never repeat

    def test_a_failed_end_keeps_the_error(self):
        self.begin()
        vm.end(self.base, "x", 1, "failed", {**LINE, "error": "build failed"})
        self.assertEqual((self.lines()[0]["result"], self.lines()[0]["error"], self.lines()[0]["image"]),
                         ("failed", "build failed", "annex-app/x:r1"))

    def test_end_without_a_pending_line_refuses(self):
        self.history(LIVE4)
        with self.assertRaises(vm.Fail) as cm:
            vm.end(self.base, "x", 4, "live", dict(LINE))
        self.assertIn("no pending line", str(cm.exception))

    def test_no_temp_files_are_left(self):
        self.begin()
        vm.end(self.base, "x", 1, "live", dict(LINE))
        self.assertEqual(sorted(p.name for p in (self.base / "apps" / "x").iterdir()), ["releases.jsonl"])

    def test_a_pending_line_never_reaches_the_database(self):
        self.assertIsNone(vm.release_row("x", {**LIVE4, "result": "pending"}))


class Reconcile(Base):
    def pending(self, step="gate"):
        self.history(LIVE4)
        return self.died_at(step)

    def next_run(self, inspect, **kw):
        return self.begin(current(actor="sam@mac", started=T0 + 60), inspect=inspect, **kw)["reconciled"][0]

    def test_healthy_pending_container_is_live_and_gets_its_deploy_json(self):
        self.pending()
        (self.base / "deploy" / "x" / "deploy.json").write_text(json.dumps({"release": 4, "schedule": []}))
        r = self.next_run(lambda app, m: state("annex-app/x:r5"))
        self.assertEqual(r["result"], "live")
        self.assertIn("r5 of x was interrupted at gate", r["message"])
        self.assertEqual(self.lines()[1]["result"], "live")
        self.assertEqual(self.lines()[1]["interrupted_at"], "gate")
        rec = json.loads((self.base / "deploy" / "x" / "deploy.json").read_text())
        self.assertEqual((rec["release"], rec["image"]), (5, "annex-app/x:r5"))
        self.assertFalse(r["schedule_changed"])

    def test_the_new_run_is_in_current_json_and_the_next_release_follows(self):
        self.pending()
        res = self.begin(current(actor="sam@mac"), inspect=lambda app, m: state("annex-app/x:r4"))
        self.assertEqual(res["release"], 6)
        self.assertEqual(vm.read_current(self.base, "x")["actor"], "sam@mac")
        self.assertEqual([(x["release"], x["result"]) for x in self.lines()], [(4, "live"), (5, "failed"), (6, "pending")])

    def test_a_schedule_change_asks_for_the_scheduler_restart(self):
        self.pending()
        (self.base / "deploy" / "x" / "deploy.json").write_text(json.dumps({"schedule": [{"name": "nightly"}]}))
        self.assertTrue(self.next_run(lambda app, m: state("annex-app/x:r5"))["schedule_changed"])

    def test_a_job_app_without_healthcheck_counts_as_healthy(self):
        self.pending()
        self.assertEqual(self.next_run(lambda app, m: state("annex-app/x:r5", health="none"))["result"], "live")

    def test_starting_health_is_waited_for(self):
        self.pending()
        answers = iter([state("annex-app/x:r5", health="starting"), state("annex-app/x:r5", health="starting"),
                        state("annex-app/x:r5")])
        r = self.next_run(lambda app, m: next(answers), wait=10)  # noqa: B023
        self.assertEqual(r["result"], "live")

    def test_unhealthy_pending_container_is_failed_and_says_to_roll_back(self):
        self.pending()
        before = (self.base / "deploy" / "x" / "deploy.json")
        before.write_text(json.dumps({"release": 4}))
        r = self.next_run(lambda app, m: state("annex-app/x:r5", health="unhealthy"))
        self.assertEqual(r["result"], "failed")
        self.assertIn("No healthy container of x is running", r["message"])
        self.assertIn("annex rollback x --to 4", r["message"])
        self.assertEqual(json.loads(before.read_text())["release"], 4)  # deploy.json untouched
        self.assertEqual(self.lines()[1]["error"], r["message"])

    def test_previous_release_still_running_means_failed_before_the_switch(self):
        self.pending(step="build")
        r = self.next_run(lambda app, m: state("annex-app/x:r4"))
        self.assertEqual(r["result"], "failed")
        self.assertIn("interrupted at build", r["message"])
        self.assertIn("r4's image", r["message"])
        self.assertNotIn("No healthy container", r["message"])

    def test_no_container_at_all(self):
        self.pending(step="switch")
        r = self.next_run(lambda app, m: None)
        self.assertEqual(r["result"], "failed")
        self.assertIn("there is no container of x", r["message"])
        self.assertIn("annex rollback x --to 4", r["message"])

    def test_first_release_with_nothing_to_roll_back_to(self):
        self.died_at("gate")
        r = self.next_run(lambda app, m: state("annex-app/x:r1", status="restarting", health="unhealthy"))
        self.assertIn("Fix the app and deploy again", r["message"])

    def test_nothing_pending_nothing_reconciled(self):
        self.history(LIVE4)
        self.assertEqual(vm.reconcile(self.base, "x", None, inspect=lambda app, m: self.fail("no inspect")), [])

    def test_a_current_json_of_another_release_gives_an_unknown_step(self):
        self.history(LIVE4, {**LINE, "release": 5, "image": "annex-app/x:r5", "result": "pending"})
        vm.write_current(self.base, "x", {**current(step="gate"), "release": 3})
        r = self.begin(inspect=lambda app, m: state("annex-app/x:r4"))
        self.assertIn("interrupted at an unknown step", r["reconciled"][0]["message"])
        self.assertEqual(r["release"], 6)

    def test_dead_step_picks_the_holder_of_the_pending_release(self):
        rels = [LIVE4, {"release": 5, "result": "pending"}]
        self.assertEqual(vm.dead_step(rels, {"release": 5, "step": "gate"}), "gate")
        self.assertEqual(vm.dead_step(rels, None, {"release": 5, "step": "build"}), "build")
        self.assertIsNone(vm.dead_step(rels, {"release": 4, "step": "gate"}))
        self.assertIsNone(vm.dead_step([LIVE4], {"release": 4, "step": "gate"}))


class Commands(Base):
    def call(self, fn, **args):
        buf = io.StringIO()
        with unittest.mock.patch.object(vm, "DATA", self.base), contextlib.redirect_stdout(buf):
            fn(argparse.Namespace(**args))
        return json.loads(buf.getvalue())

    def test_deploy_state_says_whether_a_run_holds_the_lock(self):
        vm.write_current(self.base, "x", {**current(step="gate"), "release": 5})
        res = self.call(vm.cmd_deploy_state, app="x")
        self.assertEqual((res["running"], res["current"]["step"]), (False, "gate"))
        self.hold_elsewhere()
        self.assertTrue(self.call(vm.cmd_deploy_state, app="x")["running"])

    def test_deploy_reconcile_refuses_while_a_run_holds_the_lock_and_names_its_pid(self):
        self.hold_elsewhere()
        vm.write_current(self.base, "x", {**current(step="gate"), "release": 5})
        with unittest.mock.patch.object(vm, "DATA", self.base), self.assertRaises(vm.Fail) as cm:
            vm.cmd_deploy_reconcile(argparse.Namespace(app="x", actor="me"))
        self.assertIn("kill 4242", str(cm.exception))

    def test_deploy_reconcile_settles_a_dead_run_with_its_step(self):
        self.history(LIVE4)
        self.died_at("gate")
        with unittest.mock.patch.object(vm, "container_state", return_value=state("annex-app/x:r4")), \
                unittest.mock.patch.object(vm, "after_reconcile", return_value=[]):
            res = self.call(vm.cmd_deploy_reconcile, app="x", actor="me")
        self.assertEqual(res["reconciled"][0]["result"], "failed")
        self.assertIn("interrupted at gate", res["reconciled"][0]["message"])
        self.assertFalse(vm.lock_held(self.base, "x"))

    def test_an_old_cli_is_stopped_at_its_first_call(self):
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["annex_vm.py", "deploy-begin", "x", "--actor", "kim"]), \
                contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            vm.main()
        res = json.loads(buf.getvalue())
        self.assertFalse(res["ok"])
        self.assertIn("update your checkout", res["error"])


class ContainerState(unittest.TestCase):
    WEB = {"type": "web", "port": 8080, "healthcheck": "/nope"}

    def state(self, inspect_out, probe_rc, manifest):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return vm.subprocess.CompletedProcess(cmd, 0 if inspect_out else 1, inspect_out, "")
            return vm.subprocess.CompletedProcess(cmd, probe_rc, b"", b"")
        with unittest.mock.patch.object(vm, "run", side_effect=run):
            return vm.container_state("x", manifest), calls

    def test_a_web_app_is_judged_by_the_gates_probe_not_dockers_healthcheck(self):
        st, calls = self.state("annex-app/x:r5|running|healthy\n", 1, self.WEB)
        self.assertEqual(st["health"], "unhealthy")
        self.assertIn("http://annex-app-x:8080/nope", calls[1])
        self.assertEqual(calls[1][:3], ["docker", "exec", "annex-traefik-1"])

    def test_a_probe_that_answers_is_healthy_and_a_starting_container_waits(self):
        self.assertEqual(self.state("i|running|starting", 0, self.WEB)[0]["health"], "healthy")
        self.assertEqual(self.state("i|running|starting", 1, self.WEB)[0]["health"], "starting")

    def test_a_job_app_keeps_dockers_answer_and_no_container_is_none(self):
        st, calls = self.state("i|running|none", 1, {"type": "job"})
        self.assertEqual((st["health"], len(calls)), ("none", 1))
        self.assertIsNone(self.state("", 1, self.WEB)[0])

    def test_reconcile_asks_with_the_pending_releases_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vm.write_releases(base, "x", [{**LINE, "release": 1, "image": "i", "result": "pending", "manifest": self.WEB}])
            seen = []
            vm.reconcile(base, "x", "gate", inspect=lambda app, m: seen.append(m))
            self.assertEqual(seen, [self.WEB])


class Decide(unittest.TestCase):
    def test_a_rollback_whose_target_image_runs_healthy_is_live(self):
        rels = [LIVE4, {**LIVE4, "release": 2, "image": "annex-app/x:r2"}]
        result, _ = vm.decide("x", {"release": 5, "image": "annex-app/x:r2"}, "gate", state("annex-app/x:r2"), rels)
        self.assertEqual(result, "live")


if __name__ == "__main__":
    unittest.main()
