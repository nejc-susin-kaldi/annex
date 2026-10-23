"""The deploy and rollback sequence on the VM (D101), without the fork: the order of the steps, the release line on
every exit path, the gate's switch back, reconcile notes, rollback targets. Fakes in deploy_fakes.py.
Stdlib unittest, runs on the Mac: python3 -m unittest discover -s vm/tests"""
import json
import unittest
import unittest.mock

from deploy_fakes import Base, dep, deploy_stdin, manifest, vm

LIVE4 = {"release": 4, "image": "annex-app/demo:r4", "result": "live", "deployed_at": "2026-10-22T10:00:00+0200",
         "deployed_by": "sam@mac", "via": "cli", "manifest": manifest().to_dict()}
LIVE3 = {**LIVE4, "release": 3, "image": "annex-app/demo:r3"}


class Deploy(Base):
    def test_steps_in_order_and_the_release_ends_live_last(self):
        self.history(LIVE4)
        lines, run = self.go("deploy", deploy_stdin(manifest()))
        self.assertEqual(self.steps, ["upload", "build", "snapshot", "provision", "switch", "gate", "finish"])
        final = lines[-1]
        self.assertTrue(final["ok"], final)
        self.assertEqual((final["release"], final["image"], final["previous_release"], final["snapshot"]),
                         (5, "annex-app/demo:r5", 4, "pre-r5"))
        self.assertEqual(final["url"], "https://demo.annex.home.arpa:8443")
        self.assertIn("PORT", final["injected_env"])
        last = self.releases()[-1]
        self.assertEqual((last["release"], last["result"], last["snapshot"], last["deployed_by"]),
                         (5, "live", "pre-r5", "kim@mac"))
        self.assertEqual(self.snapshots, [("demo", 5, 4)])
        cur = vm.read_current(self.base, "demo")
        self.assertEqual((cur["step"], cur["result"], cur["release"]), ("finish", "live", 5))
        self.assertTrue(all("step" in x for x in lines if "progress" in x))

    def test_the_source_is_unpacked_and_the_compose_file_is_json_compose_reads(self):
        lines, _ = self.go("deploy", deploy_stdin(manifest()))
        d = self.base / "deploy" / "demo"
        self.assertEqual(sorted(p.name for p in (d / "src").iterdir()), ["Dockerfile", "annex.yaml", "app.py"])
        self.assertFalse((d / "src.new").exists())
        compose = json.loads((d / "compose.yaml").read_text())
        self.assertEqual(compose["services"]["demo"]["image"], "annex-app/demo:r1")
        self.assertEqual(json.loads((d / "deploy.json").read_text())["release"], 1)
        self.assertTrue(self.docker.ran("docker", "compose", "up", "-d", "--remove-orphans"))
        build = self.docker.ran("docker", "build")[0]
        self.assertEqual(build[-1], str(d / "src"))
        self.assertIn("annex-app/demo:r1", build)

    def test_a_first_deploy_has_no_snapshot(self):
        self.go("deploy", deploy_stdin(manifest()))
        self.assertNotIn("snapshot", self.steps)
        self.assertEqual(self.snapshots, [])

    def test_an_exception_ends_the_release_failed_at_its_step(self):
        self.history(LIVE4)
        with unittest.mock.patch.object(vm, "provision", side_effect=vm.Fail("secrets declared but not set: KEY")):
            lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertEqual(lines[-1], {"ok": False, "error": "secrets declared but not set: KEY", "step": "provision"})
        last = self.releases()[-1]
        self.assertEqual((last["result"], last["interrupted_at"]), ("failed", "provision"))
        self.assertIn("broke off at provision: secrets declared", last["error"])

    def test_a_build_failure_is_a_failed_release_with_the_build_tail(self):
        self.docker.build_rc = 1
        lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("ERROR: pip install failed", lines[-1]["error"])
        self.assertEqual(self.releases()[-1]["error"], "broke off at build: Docker build failed for demo. Fix the "
                         "Dockerfile or code and deploy again. The running version is unchanged.")

    def test_sigterm_at_the_gate_still_ends_the_release(self):
        self.history(LIVE4)
        with unittest.mock.patch.object(dep, "gate", side_effect=dep.Terminated()):
            lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertIn("stopped (SIGTERM) at the gate step", lines[-1]["error"])
        self.assertEqual((self.releases()[-1]["result"], self.releases()[-1]["interrupted_at"]), ("failed", "gate"))

    def test_a_failed_gate_switches_back_and_ends_once_with_rolled_back_to(self):
        self.history(LIVE4)
        with unittest.mock.patch.object(dep, "gate", side_effect=[("GET /healthz did not return 200", "log"), None]):
            lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertFalse(lines[-1]["ok"])
        self.assertIn("rolled back to r4, which is live again", lines[-1]["error"])
        self.assertIn("back-provision", self.steps)
        rels = self.releases()
        self.assertEqual([(x["release"], x["result"]) for x in rels], [(4, "live"), (5, "failed")])
        self.assertEqual((rels[1]["rolled_back_to"], rels[1]["error"]), (4, "GET /healthz did not return 200"))
        self.assertEqual(json.loads((self.base / "deploy" / "demo" / "deploy.json").read_text())["release"], 4)
        self.assertTrue(self.docker.ran("docker", "rmi", "annex-app/demo:r5"))
        self.assertEqual([p["app"] for p in self.provisioned], ["demo", "demo"])  # the old manifest again

    def test_a_failed_first_release_is_left_as_is(self):
        with unittest.mock.patch.object(dep, "gate", return_value=("it crashed", "Traceback")):
            lines, _ = self.go("deploy", deploy_stdin(manifest()))
        self.assertIn("no earlier release to roll back to", lines[-1]["error"])
        self.assertEqual(self.releases()[-1]["result"], "failed")

    def test_reconcile_notes_are_shown_and_returned(self):
        self.history(LIVE4, {**LIVE4, "release": 5, "image": "annex-app/demo:r5", "result": "pending"})
        lines, _ = self.go("deploy", deploy_stdin(manifest()))
        notes = [x["note"] for x in lines if "note" in x]
        self.assertTrue(any("r5 of demo was interrupted" in n for n in notes), notes)
        self.assertTrue(any("r5 of demo was interrupted" in w for w in lines[-1]["warnings"]))
        self.assertEqual(lines[-1]["release"], 6)

    def test_a_truncated_upload_is_a_failed_release(self):
        self.history(LIVE4)
        tar = deploy_stdin(manifest()).getvalue().split(b"\n", 1)[1][:40]
        lines, _ = self.go("deploy", deploy_stdin(manifest(), tar=tar))
        self.assertIn("upload of demo's source broke off", lines[-1]["error"])
        self.assertEqual((self.releases()[-1]["result"], self.releases()[-1]["interrupted_at"]), ("failed", "upload"))
        self.assertEqual(self.docker.ran("docker", "build"), [])

    def test_a_meta_for_another_app_is_refused_before_any_line(self):
        lines, _ = self.go("deploy", deploy_stdin(manifest(name="other")))
        self.assertIn("not 'demo'", lines[-1]["error"])
        self.assertEqual(self.releases(), [])

    def test_ops_warnings_and_the_log_check_join_the_result(self):
        self.docker.slack, self.docker.logins = False, []
        lines, _ = self.go("deploy", deploy_stdin(manifest(notify_slack=["#x"])))
        warnings = " ".join(lines[-1]["warnings"])
        self.assertIn("sam isn't in the user directory", warnings)
        self.assertIn("notify.slack is declared", warnings)
        self.assertNotIn("aren't structured", warnings)  # the fake logs JSON lines


class Overrides(Base):
    def test_only_the_three_keys_are_kept(self):
        got = dep.overrides({"overrides": {"ANNEX_IMAGE_MAX_MB": 50, "ANNEX_HEALTH_TIMEOUT": "5", "PATH": "/evil",
                                           "ANNEX_BUILD_MIN_FREE_GB": ""}})
        self.assertEqual(got, {"ANNEX_IMAGE_MAX_MB": "50", "ANNEX_HEALTH_TIMEOUT": "5"})
        self.assertEqual(dep.overrides({"overrides": "x"}), {})

    def test_an_image_cap_from_the_caller_refuses_the_build(self):
        lines, _ = self.go("deploy", deploy_stdin(manifest(), overrides={"ANNEX_IMAGE_MAX_MB": "50"}))
        self.assertIn("above the limit of 50 MB", lines[-1]["error"])

    def test_the_health_timeout_from_the_caller(self):
        lines, _ = self.go("deploy", deploy_stdin(manifest(), overrides={"ANNEX_HEALTH_TIMEOUT": "7"}))
        self.assertTrue(any("waiting up to 7 s" in x.get("progress", "") for x in lines))


class Rollback(Base):
    docker_kw = {"images": ["annex-app/demo:r3", "annex-app/demo:r4"], "running": "annex-app/demo:r4"}

    def rollback(self, **kw):
        import io
        meta = {"line": {"deployed_at": "2026-10-22T12:00:00.000+02:00", "deployed_by": "kim@mac", "via": "cli"}}
        return self.go("rollback", io.BytesIO(json.dumps(meta).encode() + b"\n"), **kw)

    def test_rollback_uses_the_targets_image_and_ends_live(self):
        self.history(LIVE3, LIVE4)
        lines, _ = self.rollback()
        final = lines[-1]
        self.assertTrue(final["ok"], final)
        self.assertEqual((final["release"], final["rolled_back_from"], final["rolled_back_to"], final["image"]),
                         (5, 4, 3, "annex-app/demo:r3"))
        last = self.releases()[-1]
        self.assertEqual((last["image"], last["result"], last["rollback"]),
                         ("annex-app/demo:r3", "live", {"from": 4, "to": 3, "with_data": False}))
        self.assertEqual(self.steps, ["snapshot", "provision", "switch", "gate", "finish"])

    def test_a_bad_target_is_refused_before_any_release_line(self):
        self.history(LIVE3, LIVE4)
        lines, _ = self.rollback(to=99)
        self.assertIn("Can't roll demo back to r99. Possible targets: r3", lines[-1]["error"])
        self.assertEqual(len(self.releases()), 2)

    def test_the_live_release_is_a_target_only_when_its_container_is_not_running(self):
        self.history(LIVE3, LIVE4)
        lines, _ = self.rollback(to=4)
        self.assertIn("Possible targets", lines[-1]["error"])
        self.docker.running = "annex-app/demo:r5"
        lines, _ = self.rollback(to=4)
        self.assertEqual((lines[-1]["rolled_back_to"], lines[-1]["image"]), (4, "annex-app/demo:r4"))

    def test_with_data_restores_and_ends_the_maintenance_window(self):
        self.history(LIVE3, LIVE4)
        snap = self.base / "snapshots" / "demo" / "pre-r4"
        snap.mkdir(parents=True)
        (snap / "meta.json").write_text(json.dumps({"name": "pre-r4", "from_release": 3}))
        lines, _ = self.rollback(with_data=True)
        self.assertTrue(lines[-1]["ok"], lines[-1])
        self.assertIn("restore", self.steps)
        vm.maintenance.assert_called_with("demo", None)

    def test_with_data_needs_a_snapshot(self):
        self.history(LIVE3, LIVE4)
        lines, _ = self.rollback(with_data=True)
        self.assertIn("no data snapshot from while r3 was live", lines[-1]["error"])

    def test_an_exception_ends_the_rollback_failed(self):
        self.history(LIVE3, LIVE4)
        with unittest.mock.patch.object(vm, "snapshot", side_effect=vm.Fail("disk full")):
            lines, _ = self.rollback()
        self.assertEqual((self.releases()[-1]["result"], self.releases()[-1]["interrupted_at"]), ("failed", "snapshot"))


if __name__ == "__main__":
    unittest.main()


class HeadAndTail(unittest.TestCase):
    def test_short_logs_are_whole_and_long_ones_keep_the_start(self):
        import annex_deploy as d
        self.assertEqual(d.head_and_tail("a\nb\n", 40), "a\nb\n")
        text = "\n".join(f"l{i}" for i in range(100)) + "\n"
        out = d.head_and_tail(text, 5, head=3)
        self.assertEqual(out.splitlines(), ["l0", "l1", "l2", "... (92 line(s) skipped) ...", "l95", "l96", "l97", "l98", "l99"])
