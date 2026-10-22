"""The deploy lock and pending release (C1, D92): pure file operations on a temp data dir.
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import json
import sys
import tempfile
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


def state(image, status="running", health="healthy"):
    return {"image": image, "status": status, "health": health}


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

    def begin(self, now=T0, actor="kim@mac", inspect=lambda app, m: None, wait=0, **kw):
        return vm.begin(self.base, "x", actor, dict(LINE), dict(RECORD), now, inspect=inspect, wait=wait,
                        sleep=lambda s: None, **kw)


class Lock(Base):
    def test_begin_numbers_the_release_and_appends_a_pending_line(self):
        self.history(LIVE4)
        res = self.begin()
        self.assertEqual((res["release"], res["image"]), (5, "annex-app/x:r5"))
        self.assertRegex(res["token"], vm.TOKEN_RE)
        last = self.lines()[-1]
        self.assertEqual((last["release"], last["result"], last["deployed_by"]), (5, "pending", "kim@mac"))
        self.assertEqual((last["record"]["release"], last["record"]["image"]), (5, "annex-app/x:r5"))
        owner = vm.read_lock(self.base, "x")
        self.assertEqual((owner["actor"], owner["release"], owner["token"]), ("kim@mac", 5, res["token"]))

    def test_first_release_is_r1(self):
        self.assertEqual(self.begin()["release"], 1)

    def test_a_rollback_keeps_its_targets_image(self):
        self.history(LIVE4)
        res = vm.begin(self.base, "x", "kim", {**LINE, "image": "annex-app/x:r2"}, None, T0, inspect=lambda a, m: None)
        self.assertEqual((res["release"], res["image"]), (5, "annex-app/x:r2"))
        self.assertNotIn("record", self.lines()[-1])

    def test_a_second_deploy_is_refused_and_told_who_holds_the_lock(self):
        self.begin()
        vm.set_step(self.base, "x", vm.read_lock(self.base, "x")["token"], "build", T0 + 5)
        with self.assertRaises(vm.Fail) as cm:
            self.begin(now=T0 + 90, actor="sam@mac")
        msg = str(cm.exception)
        for part in ("kim@mac", "r1", "build", "1 min 30 s ago", "30 minutes"):
            self.assertIn(part, msg)
        self.assertEqual([x["release"] for x in self.lines()], [1])  # no second pending line

    def test_a_stale_lock_is_taken_over_and_named(self):
        self.history(LIVE4)
        old = self.begin()
        vm.set_step(self.base, "x", old["token"], "gate", T0 + 10)
        res = self.begin(now=T0 + vm.LOCK_STALE_S + 1, actor="sam@mac")
        self.assertIn("kim@mac", res["took_over"])
        self.assertIn("gate", res["took_over"])
        self.assertEqual(res["release"], 6)
        self.assertEqual([x["result"] for x in res["reconciled"]], ["failed"])
        self.assertIn("interrupted at gate", res["reconciled"][0]["message"])
        self.assertEqual([(x["release"], x["result"]) for x in self.lines()], [(4, "live"), (5, "failed"), (6, "pending")])

    def test_a_young_lock_can_be_broken_with_stale_after_zero(self):
        self.begin()
        self.assertEqual(self.begin(now=T0 + 1, stale_after=0)["release"], 2)

    def test_a_lock_dir_without_owner_file_counts_as_held(self):
        vm.lock_dir(self.base, "x").mkdir(parents=True)
        with self.assertRaises(vm.Fail) as cm:
            vm.take_lock(self.base, "x", "sam", vm.lock_dir(self.base, "x").stat().st_mtime + 5)
        self.assertIn("unknown", str(cm.exception))

    def test_step_and_end_need_the_token(self):
        res = self.begin()
        with self.assertRaises(vm.Fail):
            vm.set_step(self.base, "x", "0" * 32, "build", T0)
        with self.assertRaises(vm.Fail) as cm:
            vm.end(self.base, "x", "0" * 32, 1, "live", dict(LINE), T0)
        self.assertIn("taken over by kim@mac", str(cm.exception))
        self.assertEqual(self.lines()[-1]["result"], "pending")  # untouched
        self.assertTrue(vm.lock_dir(self.base, "x").exists())   # still held by its owner
        vm.end(self.base, "x", res["token"], 1, "live", dict(LINE), T0)

    def test_a_deploy_whose_lock_was_taken_over_cannot_end(self):
        self.history(LIVE4)
        old = self.begin()
        self.begin(now=T0 + vm.LOCK_STALE_S + 1, actor="sam@mac")
        with self.assertRaises(vm.Fail) as cm:
            vm.end(self.base, "x", old["token"], 5, "live", dict(LINE), T0 + vm.LOCK_STALE_S + 2)
        self.assertIn("sam@mac", str(cm.exception))
        self.assertEqual(self.lines()[1]["result"], "failed")  # what reconcile decided stays

    def test_release_lock_only_with_the_token(self):
        res = self.begin()
        self.assertFalse(vm.release_lock(self.base, "x", "0" * 32))
        self.assertTrue(vm.release_lock(self.base, "x", res["token"]))
        self.assertIsNone(vm.read_lock(self.base, "x"))


class End(Base):
    def test_end_rewrites_the_pending_line_and_unlocks(self):
        self.history(LIVE4)
        res = self.begin()
        final = {**LINE, "image": res["image"], "snapshot": "pre-r5", "result": "live"}
        out = vm.end(self.base, "x", res["token"], 5, "live", final, T0 + 30)
        self.assertEqual(out["result"], "live")
        lines = self.lines()
        self.assertEqual([(x["release"], x["result"]) for x in lines], [(4, "live"), (5, "live")])
        self.assertEqual(lines[1]["snapshot"], "pre-r5")
        self.assertNotIn("record", lines[1])
        self.assertIsNone(vm.read_lock(self.base, "x"))
        self.assertEqual(self.begin(now=T0 + 60)["release"], 6)  # free again, and numbers never repeat

    def test_a_failed_end_keeps_the_error(self):
        res = self.begin()
        vm.end(self.base, "x", res["token"], 1, "failed", {**LINE, "error": "build failed"}, T0)
        self.assertEqual((self.lines()[0]["result"], self.lines()[0]["error"], self.lines()[0]["image"]),
                         ("failed", "build failed", "annex-app/x:r1"))

    def test_no_temp_files_are_left(self):
        res = self.begin()
        vm.end(self.base, "x", res["token"], 1, "live", dict(LINE), T0)
        self.assertEqual(sorted(p.name for p in (self.base / "apps" / "x").iterdir()), ["releases.jsonl"])

    def test_a_pending_line_never_reaches_the_database(self):
        self.assertIsNone(vm.release_row("x", {**LIVE4, "result": "pending"}))


class Reconcile(Base):
    def pending(self, step="gate"):
        self.history(LIVE4)
        res = self.begin()
        vm.set_step(self.base, "x", res["token"], step, T0 + 1)
        return res

    def takeover(self, inspect, **kw):
        return self.begin(now=T0 + vm.LOCK_STALE_S + 1, actor="sam@mac", inspect=inspect, **kw)["reconciled"][0]

    def test_healthy_pending_container_is_live_and_gets_its_deploy_json(self):
        self.pending()
        (self.base / "deploy" / "x" / "deploy.json").write_text(json.dumps({"release": 4, "schedule": []}))
        r = self.takeover(lambda app, m: state("annex-app/x:r5"))
        self.assertEqual(r["result"], "live")
        self.assertIn("r5 of x was interrupted at gate", r["message"])
        self.assertEqual(self.lines()[1]["result"], "live")
        self.assertEqual(self.lines()[1]["interrupted_at"], "gate")
        rec = json.loads((self.base / "deploy" / "x" / "deploy.json").read_text())
        self.assertEqual((rec["release"], rec["image"]), (5, "annex-app/x:r5"))
        self.assertFalse(r["schedule_changed"])

    def test_a_schedule_change_asks_for_the_scheduler_restart(self):
        self.pending()
        (self.base / "deploy" / "x" / "deploy.json").write_text(json.dumps({"schedule": [{"name": "nightly"}]}))
        self.assertTrue(self.takeover(lambda app, m: state("annex-app/x:r5"))["schedule_changed"])

    def test_a_job_app_without_healthcheck_counts_as_healthy(self):
        self.pending()
        self.assertEqual(self.takeover(lambda app, m: state("annex-app/x:r5", health="none"))["result"], "live")

    def test_starting_health_is_waited_for(self):
        self.pending()
        answers = iter([state("annex-app/x:r5", health="starting"), state("annex-app/x:r5", health="starting"),
                        state("annex-app/x:r5")])
        r = self.begin(now=T0 + vm.LOCK_STALE_S + 1, inspect=lambda app, m: next(answers), wait=10)  # noqa: B023
        self.assertEqual(r["reconciled"][0]["result"], "live")

    def test_unhealthy_pending_container_is_failed_and_says_to_roll_back(self):
        self.pending()
        before = (self.base / "deploy" / "x" / "deploy.json")
        before.write_text(json.dumps({"release": 4}))
        r = self.takeover(lambda app, m: state("annex-app/x:r5", health="unhealthy"))
        self.assertEqual(r["result"], "failed")
        self.assertIn("No healthy container of x is running", r["message"])
        self.assertIn("annex rollback x --to 4", r["message"])
        self.assertEqual(json.loads(before.read_text())["release"], 4)  # deploy.json untouched
        self.assertEqual(self.lines()[1]["error"], r["message"])

    def test_previous_release_still_running_means_failed_before_the_switch(self):
        self.pending(step="build")
        r = self.takeover(lambda app, m: state("annex-app/x:r4"))
        self.assertEqual(r["result"], "failed")
        self.assertIn("interrupted at build", r["message"])
        self.assertIn("r4's image", r["message"])
        self.assertNotIn("No healthy container", r["message"])

    def test_no_container_at_all(self):
        self.pending(step="switch")
        r = self.takeover(lambda app, m: None)
        self.assertEqual(r["result"], "failed")
        self.assertIn("there is no container of x", r["message"])
        self.assertIn("annex rollback x --to 4", r["message"])

    def test_first_release_with_nothing_to_roll_back_to(self):
        res = self.begin()
        vm.set_step(self.base, "x", res["token"], "gate", T0)
        r = self.takeover(lambda app, m: state("annex-app/x:r1", status="restarting", health="unhealthy"))
        self.assertIn("Fix the app and deploy again", r["message"])

    def test_nothing_pending_nothing_reconciled(self):
        self.history(LIVE4)
        self.assertEqual(vm.reconcile(self.base, "x", None, inspect=lambda app, m: self.fail("no inspect")), [])

    def test_a_pending_line_without_a_lock_is_reconciled_with_an_unknown_step(self):
        self.history(LIVE4, {**LINE, "release": 5, "image": "annex-app/x:r5", "result": "pending"})
        r = self.begin(inspect=lambda app, m: state("annex-app/x:r4"))
        self.assertIsNone(r["took_over"])
        self.assertIn("interrupted at an unknown step", r["reconciled"][0]["message"])
        self.assertEqual(r["release"], 6)


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
