"""Secret set, remove, migrate-networks and run on the VM (control-plane V2, D102), against the `run` seam (D96): the
order of the Docker calls, the app's lock (a held lock refuses naming the holder and changes nothing), and that
`remove` never unlinks `.lock.guard`. Stdlib unittest: python3 -m unittest discover -s vm/tests"""
import argparse
import io
import json
import os
import subprocess
import unittest
import unittest.mock

from deploy_fakes import Base as DeployBase
from deploy_fakes import FakeDocker, dep, vm

import annex_apps as apps  # noqa: E402  (deploy_fakes put vm/ on the path)

try:
    import yaml
except ImportError:  # the migrate-networks tests read a compose file with PyYAML, as the VM does
    yaml = None

RECORD = {"name": "demo", "type": "web", "postgres": True, "files": False, "views": ["orders"], "secrets": ["API_KEY"],
          "schedule": [], "notify_email": True, "notify_slack": ["#ops"], "webhooks": [], "port": 8080, "egress": []}
OLD_COMPOSE = {
    "name": "annex-app-demo",
    "services": {"demo": {
        "image": "annex-app/demo:r3", "container_name": "annex-app-demo",
        "environment": {"ANNEX_GATEWAY_URL": "http://gateway:3000", "PORT": "8080"},
        "labels": {"com.kaldi.annex": "true", "traefik.enable": "true"},
        "networks": ["edge", "appdata"]}},
    "networks": {"edge": {"external": True, "name": "annex-edge"}, "appdata": {"external": True, "name": "annex-appdata"}},
}


class Docker(FakeDocker):
    """FakeDocker plus what these operations ask: containers by label, `docker image ls`, compose down."""

    def __init__(self, ps="", **kw):
        super().__init__(**kw)
        self.ps = ps

    def __call__(self, argv, **kw):
        res = super().__call__(argv, **kw)
        if list(argv[:2]) == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, self.ps if kw.get("text") else self.ps.encode(), "")
        return res


class Base(DeployBase):
    def setUp(self):
        super().setUp()
        self.docker = Docker(**self.docker_kw)
        patches = [
            unittest.mock.patch.object(vm, "run", self.docker),
            unittest.mock.patch.object(vm, "SECRETS", self.base / "secrets"),
            unittest.mock.patch.object(vm, "audit"),
            unittest.mock.patch("time.sleep"),  # hold_app_lock's retries
        ]
        self.audit = patches[2].start()
        self.addCleanup(patches[2].stop)
        for p in patches[:2] + patches[3:]:
            p.start()
            self.addCleanup(p.stop)
        self.d = self.base / "deploy" / "demo"
        self.d.mkdir(parents=True)

    def record(self, **kw):
        (self.d / "deploy.json").write_text(json.dumps({**RECORD, **kw}))

    def serve(self, command, pairs=None, **kw):
        out = io.StringIO()
        a = argparse.Namespace(app="demo", actor="kim@mac", keep_data=kw.get("keep_data", False),
                               egress=kw.get("egress", False))
        code = apps.serve(command, a, pairs=pairs, stdout=out, base=self.base, signals=False)
        return code, [json.loads(x) for x in out.getvalue().splitlines()]

    def names(self):
        return [" ".join(c[:3]) for c in self.docker.calls]

    def hold(self):
        fd = vm.hold_app_lock(self.base, "demo")
        self.held = fd
        self.addCleanup(lambda: os.close(self.held) if self.held is not None else None)
        return fd

    def lock_is_free(self):
        fd = vm.hold_app_lock(self.base, "demo", tries=1)
        os.close(fd)
        return True


class SecretSet(Base):
    def test_a_deployed_app_gets_its_environment_rewritten_and_the_container_recreated_after_a_drain(self):
        self.record()
        code, lines = self.serve("secret-set", [("API_KEY", "s3cr3t-value")])
        self.assertEqual(code, 0)
        final = lines[-1]
        self.assertEqual(final["ok"], True)
        self.assertEqual(final["set"], ["API_KEY"])
        self.assertEqual(final["applied"], "restarted with new values")
        self.assertNotIn("warning", final)
        # provision runs from deploy.json (the notify grants, the port, the views), before the container is replaced
        [prov] = self.provisioned
        self.assertEqual((prov["postgres"], prov["views"], prov["secrets"], prov["notify_email"], prov["slack"]),
                         (True, ["orders"], ["API_KEY"], True, ["#ops"]))
        order = [i for i, c in enumerate(self.docker.calls) if "logs-drain" in c or c[:3] == ["docker", "compose", "up"]]
        self.assertEqual(len(order), 2)
        self.assertIn("logs-drain", self.docker.calls[order[0]])
        self.assertEqual(self.docker.calls[order[1]], ["docker", "compose", "up", "-d"])

    def test_the_value_is_stored_and_never_on_a_command_line_or_in_the_output(self):
        self.record()
        _, lines = self.serve("secret-set", [("API_KEY", "s3cr3t-value")])
        self.assertIn("API_KEY='s3cr3t-value'", (self.base / "secrets/apps/demo/secrets.env").read_text())
        self.assertFalse([c for c in self.docker.calls if "s3cr3t-value" in " ".join(c)])
        self.assertNotIn("s3cr3t-value", json.dumps(lines))
        self.audit.assert_called_once()
        self.assertEqual(self.audit.call_args.args[:3], ("secret.set", "kim@mac", "demo"))
        self.assertEqual(self.audit.call_args.args[3], {"names": ["API_KEY"]})  # names only

    def test_an_app_that_is_not_deployed_yet_only_stores_the_value(self):
        code, lines = self.serve("secret-set", [("API_KEY", "v")])
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["applied"], "stored; injected on the next deploy")
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.provisioned, [])

    def test_a_key_the_manifest_does_not_declare_is_warned_about(self):
        self.record(secrets=[])
        _, lines = self.serve("secret-set", [("OTHER", "v")])
        self.assertIn("OTHER not declared under secrets", lines[-1]["warning"])

    def test_a_held_lock_refuses_naming_the_holder_and_stores_nothing(self):
        self.record()
        vm.write_current(self.base, "demo", {"pid": 4242, "actor": "sam@mac", "kind": "deploy", "release": 5,
                                             "step": "build", "started_at": 1.0})
        self.hold()
        with self.assertRaises(vm.LockHeld) as cm:
            self.serve("secret-set", [("API_KEY", "v")])
        msg = str(cm.exception)
        self.assertIn("demo is being deployed or rolled back by sam@mac (r5, at step build", msg)
        self.assertIn("pid 4242", msg)
        self.assertFalse((self.base / "secrets/apps/demo/secrets.env").exists())
        self.assertEqual(self.docker.calls, [])

    def test_a_secret_set_holding_the_lock_is_named_as_one_to_a_deploy(self):
        self.record()
        vm.write_current(self.base, "demo", {"pid": 7, "actor": "kim@mac", "kind": "secret-set", "step": "provision",
                                             "started_at": 1.0})
        self.hold()
        with self.assertRaises(vm.LockHeld) as cm:
            vm.hold_app_lock(self.base, "demo", tries=1)
        self.assertIn("demo is having a secret set by kim@mac (at step provision", str(cm.exception))

    def test_a_failed_recreate_says_why_ends_the_run_and_frees_the_lock(self):
        self.record()
        self.docker.fail = ("compose up",)
        code, lines = self.serve("secret-set", [("API_KEY", "v")])
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["ok"], False)
        self.assertIn("docker compose up failed for demo", lines[-1]["error"])
        self.assertEqual(lines[-1]["step"], "switch")
        self.assertTrue(self.lock_is_free())
        self.assertEqual(vm.read_current(self.base, "demo")["result"], "failed")

    def test_the_lock_is_free_after_a_good_run_and_current_json_says_what_ran(self):
        self.record()
        self.serve("secret-set", [("API_KEY", "v")])
        self.assertTrue(self.lock_is_free())
        cur = vm.read_current(self.base, "demo")
        self.assertEqual((cur["kind"], cur["actor"], cur["result"]), ("secret-set", "kim@mac", "ok"))
        self.assertEqual([p.name for p in self.d.iterdir() if p.name.endswith(".tmp")], [])

    def test_parse_refuses_bad_lines_before_anything_is_locked_or_written(self):
        for bad in ("lower=1", "NOEQUALS", "A=it's", "1A=x"):
            with self.subTest(bad=bad), self.assertRaises(vm.Fail):
                vm.parse_secret_lines(bad + "\n")
        self.assertEqual(vm.parse_secret_lines("A=1\n\nB=x=y\n"), [("A", "1"), ("B", "x=y")])

    def test_without_recreate_the_command_keeps_its_old_one_line_answer(self):
        a = argparse.Namespace(app="demo", actor="kim@mac", recreate=False)
        out = io.StringIO()
        with unittest.mock.patch("sys.stdin", io.StringIO("API_KEY=v\n")), unittest.mock.patch("sys.stdout", out):
            vm.cmd_secret_set(a)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "set": ["API_KEY"], "all_keys": ["API_KEY"]})
        self.assertEqual(self.docker.calls, [])


class Remove(Base):
    def setUp(self):
        super().setUp()
        (self.d / "compose.yaml").write_text("{}")
        (self.d / "src").mkdir()
        (self.d / "src" / "app.py").write_text("x")
        self.docker.images = ["annex-app/demo:r1", "annex-app/demo:r2"]
        patches = [unittest.mock.patch.object(vm, "drop_product_role"), unittest.mock.patch.object(vm, "ops_db_exists",
                                                                                                     return_value=False),
                   unittest.mock.patch.object(vm, "drop_database"), unittest.mock.patch.object(vm, "as_root"),
                   unittest.mock.patch.object(vm, "remove_app_networks", return_value=["annex-app-demo"])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_sequence_drain_down_images_deprovision(self):
        self.record(schedule=[{"name": "nightly", "cron": "0 3 * * *", "command": ["python", "x.py"]}])
        code, lines = self.serve("remove")
        self.assertEqual(code, 0)
        kinds = []
        for c in self.docker.calls:
            if "logs-drain" in c:
                kinds.append("drain")
            elif c[:3] == ["docker", "compose", "down"]:
                kinds.append("down")
            elif c[:2] == ["docker", "rmi"]:
                kinds.append("rmi")
            elif c[:2] == ["docker", "restart"]:
                kinds.append("restart")
        self.assertEqual(kinds, ["drain", "down", "rmi", "restart"])
        self.assertEqual(self.docker.ran("rmi")[0], ["docker", "rmi", "-f", "annex-app/demo:r1", "annex-app/demo:r2"])
        self.assertEqual(lines[-1]["ok"], True)
        self.assertEqual(lines[-1]["kept_data"], False)
        self.assertIn("deploy files", lines[-1]["removed"])
        self.assertIn("networks", lines[-1]["removed"])

    def test_a_job_less_app_does_not_restart_the_scheduler(self):
        self.record()
        self.serve("remove")
        self.assertEqual(self.docker.ran("restart"), [])

    def test_keep_data_is_passed_on(self):
        self.record()
        _, lines = self.serve("remove", keep_data=True)
        self.assertEqual(lines[-1]["kept_data"], True)
        self.assertNotIn("database", lines[-1]["removed"])

    def test_an_app_with_no_record_and_no_container_is_refused_and_nothing_runs(self):
        code, lines = self.serve("remove")
        self.assertEqual(code, 1)
        self.assertIn("No app named 'demo'", lines[-1]["error"])
        self.assertEqual(self.docker.ran("compose"), [])
        self.assertTrue(self.lock_is_free())

    def test_a_container_without_a_record_is_still_removed(self):
        self.docker.ps = "abc123\n"
        (self.d / "compose.yaml").unlink()
        code, _ = self.serve("remove")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.ran("rm", "-f", "annex-app-demo")[0][:2], ["docker", "rm"])

    def test_a_failed_compose_down_stops_before_anything_is_deprovisioned(self):
        self.record()
        self.docker.fail = ("compose down",)
        with unittest.mock.patch.object(vm, "deprovision") as dp:
            code, lines = self.serve("remove")
        self.assertEqual(code, 1)
        self.assertIn("docker compose down failed", lines[-1]["error"])
        dp.assert_not_called()
        self.assertTrue(self.lock_is_free())

    def test_a_held_lock_refuses_naming_the_holder_and_removes_nothing(self):
        self.record()
        vm.write_current(self.base, "demo", {"pid": 99, "actor": "sam@mac", "kind": "deploy", "release": 6,
                                             "step": "gate", "started_at": 1.0})
        self.hold()
        with unittest.mock.patch.object(vm, "deprovision") as dp, self.assertRaises(vm.LockHeld) as cm:
            self.serve("remove")
        self.assertIn("sam@mac (r6, at step gate", str(cm.exception))
        dp.assert_not_called()
        self.assertEqual(self.docker.calls, [])
        self.assertTrue((self.d / "deploy.json").exists())

    def test_the_lock_file_survives_the_removal_and_the_lock_is_free_after(self):
        self.record()
        guard = vm.guard_path(self.base, "demo")
        guard.write_text("")
        inode = guard.stat().st_ino
        code, _ = self.serve("remove")
        self.assertEqual(code, 0)
        self.assertTrue(guard.exists())
        self.assertEqual(guard.stat().st_ino, inode)  # the same file: a deploy that opened it earlier still agrees
        self.assertFalse((self.d / "deploy.json").exists())
        self.assertFalse((self.d / "compose.yaml").exists())
        self.assertFalse((self.d / "src").exists())
        self.assertTrue(self.lock_is_free())

    def test_a_deploy_that_holds_the_lock_keeps_it_when_deprovision_clears_the_folder(self):
        self.record()
        fd = self.hold()
        vm.clear_deploy_dir(self.base, "demo")  # what `annex_vm.py deprovision` does on its own, with no lock of its own
        self.assertTrue(vm.guard_path(self.base, "demo").exists())
        with self.assertRaises(vm.LockHeld):
            vm.hold_app_lock(self.base, "demo", tries=1)  # still one holder, on the one file
        os.close(fd)
        self.held = None
        self.assertTrue(self.lock_is_free())

    def test_a_dead_deploys_step_is_still_known_to_the_next_run_after_a_secret_set(self):
        vm.write_current(self.base, "demo", {"pid": 1, "actor": "sam@mac", "kind": "deploy", "release": 7,
                                             "step": "gate", "started_at": 1.0})
        self.record()
        self.serve("secret-set", [("API_KEY", "v")])
        cur = vm.read_current(self.base, "demo")
        self.assertEqual(cur["kind"], "secret-set")
        pending = [{"release": 7, "result": "pending"}]
        self.assertEqual(vm.dead_step(pending, vm.read_current(self.base, "demo")), "gate")
        self.serve("secret-set", [("API_KEY", "w")])  # and across a second one
        self.assertEqual(vm.dead_step(pending, vm.read_current(self.base, "demo")), "gate")


@unittest.skipUnless(yaml, "PyYAML (the VM has python3-yaml)")
class MigrateNetworks(Base):
    def setUp(self):
        super().setUp()
        self.compose = self.d / "compose.yaml"
        self.compose.write_text(yaml.safe_dump(OLD_COMPOSE, sort_keys=False))
        self.record(egress=[])
        self.synced = []
        p = unittest.mock.patch.object(vm, "sync_app", side_effect=lambda *a: self.synced.append(a) or {"platform": ["traefik", "ops"]})
        p.start()
        self.addCleanup(p.stop)

    def test_rewires_connects_and_recreates_in_that_order(self):
        code, lines = self.serve("migrate-networks")
        self.assertEqual(code, 0)
        self.assertEqual(self.synced, [("demo", True, True, True, False)])  # web, postgres, views, egress
        doc = json.loads(self.compose.read_text())
        self.assertEqual(doc["services"]["demo"]["networks"], ["app"])
        self.assertEqual(doc["networks"], {"app": {"external": True, "name": "annex-app-demo"}})
        self.assertEqual(doc["services"]["demo"]["labels"]["traefik.docker.network"], "annex-app-demo")
        self.assertEqual(self.docker.ran("compose", "up")[0], ["docker", "compose", "up", "-d", "--remove-orphans"])
        drain = next(i for i, c in enumerate(self.docker.calls) if "logs-drain" in c)
        up = next(i for i, c in enumerate(self.docker.calls) if c[:3] == ["docker", "compose", "up"])
        self.assertLess(drain, up)
        final = lines[-1]
        self.assertEqual(final["uses"], {"web": True, "postgres": True, "views": True, "egress": False})
        self.assertEqual(final["platform"], ["traefik", "ops"])
        self.assertEqual(final["backup"], str(self.d / "compose.yaml.pre-d88"))
        self.assertEqual(final["networks"], ["healthy"])  # FakeDocker's answer to any other `docker inspect`

    def test_the_old_file_is_kept_once_and_egress_is_recorded(self):
        self.serve("migrate-networks", egress=True)
        backup = self.d / "compose.yaml.pre-d88"
        self.assertEqual(yaml.safe_load(backup.read_text()), OLD_COMPOSE)
        self.assertEqual(json.loads((self.d / "deploy.json").read_text())["egress"], ["*"])
        self.assertEqual(self.synced[0][-1], True)
        self.serve("migrate-networks")  # a second run must not overwrite the backup with the migrated file
        self.assertEqual(yaml.safe_load(backup.read_text()), OLD_COMPOSE)
        self.assertEqual(json.loads((self.d / "deploy.json").read_text())["egress"], [])

    def test_no_compose_file_is_refused(self):
        self.compose.unlink()
        code, lines = self.serve("migrate-networks")
        self.assertEqual(code, 1)
        self.assertIn("has no compose file on the VM", lines[-1]["error"])
        self.assertEqual(self.synced, [])

    def test_a_held_lock_refuses_and_changes_nothing(self):
        self.hold()
        before = self.compose.read_text()
        with self.assertRaises(vm.LockHeld):
            self.serve("migrate-networks")
        self.assertEqual(self.compose.read_text(), before)
        self.assertEqual(self.synced, [])
        self.assertEqual(self.docker.calls, [])

    def test_a_failed_recreate_keeps_the_rewritten_file_and_the_backup(self):
        self.docker.fail = ("compose up",)
        code, lines = self.serve("migrate-networks")
        self.assertEqual(code, 1)
        self.assertIn("docker compose up failed", lines[-1]["error"])
        self.assertTrue((self.d / "compose.yaml.pre-d88").exists())
        self.assertTrue(self.lock_is_free())


class Rewire(unittest.TestCase):
    """Moved from cli/tests/test_networks.py with the function (rewire_compose is VM-side now)."""

    def test_moves_a_web_app_with_postgres_and_views(self):
        old = json.loads(json.dumps(OLD_COMPOSE))
        doc, uses = dep.rewire_compose(OLD_COMPOSE, "demo", egress=False)
        self.assertEqual(uses, {"web": True, "postgres": True, "views": True, "egress": False})
        svc = doc["services"]["demo"]
        self.assertEqual(svc["networks"], ["app"])
        self.assertEqual(doc["networks"], {"app": {"external": True, "name": "annex-app-demo"}})
        self.assertEqual(svc["labels"]["traefik.docker.network"], "annex-app-demo")
        self.assertEqual(svc["image"], "annex-app/demo:r3")  # everything else is left alone
        self.assertEqual(OLD_COMPOSE, old)  # the input isn't changed

    def test_egress_keeps_internet_access(self):
        doc, uses = dep.rewire_compose(OLD_COMPOSE, "demo", egress=True)
        self.assertEqual(doc["services"]["demo"]["networks"], ["app", "egress"])
        self.assertTrue(uses["egress"])

    def test_an_edge_only_job(self):
        old = {"services": {"jj": {"labels": {"traefik.enable": "false"}, "environment": {}, "networks": ["edge"]}},
               "networks": {"edge": {"external": True, "name": "annex-edge"}}}
        doc, uses = dep.rewire_compose(old, "jj", egress=False)
        self.assertEqual(uses, {"web": False, "postgres": False, "views": False, "egress": False})
        self.assertNotIn("traefik.docker.network", doc["services"]["jj"]["labels"])

    def test_the_result_is_json_and_so_yaml_compose_can_read(self):
        doc, _ = dep.rewire_compose(OLD_COMPOSE, "demo", egress=False)
        self.assertEqual(json.loads(json.dumps(doc)), doc)

    def test_refuses_a_file_annex_didnt_write(self):
        with self.assertRaises(vm.Fail):
            dep.rewire_compose({"services": {"a": {}, "b": {}}}, "demo", egress=False)
        with self.assertRaises(vm.Fail):
            dep.rewire_compose({"services": {"a": {"labels": ["traefik.enable=true"]}}}, "demo", egress=False)


class RunJob(Base):
    JOBS = [{"name": "work", "cron": "0 3 * * *", "command": ["python", "jobs/work.py", "--full"]},
            {"name": "tick", "cron": "* * * * *", "command": ["python", "tick.py"]}]

    def run_job(self, job="work", rc=0):
        self.docker.fail = ("docker exec",) if rc else ()
        err = io.StringIO()
        a = argparse.Namespace(app="demo", job=job)
        code = apps.run_job(a, base=self.base, stderr=err)
        lines = [json.loads(x) for x in err.getvalue().splitlines()]
        return code, lines

    def test_the_command_comes_from_deploy_json_not_from_the_caller(self):
        self.record(schedule=self.JOBS)
        code, lines = self.run_job("work")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.calls, [["docker", "exec", "annex-app-demo", "python", "jobs/work.py", "--full"]])
        self.assertEqual(lines[-1]["ok"], True)
        self.assertEqual(lines[-1]["exit_code"], 0)
        self.assertIn("seconds", lines[-1])

    def test_the_output_is_the_processs_own_stdout_with_stderr_folded_in(self):
        self.record(schedule=self.JOBS)
        seen = {}

        def fake(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0)
        with unittest.mock.patch.object(vm, "run", fake):
            self.run_job("tick")
        self.assertIsNone(seen["stdout"])  # inherited: lines reach the caller as the job writes them
        self.assertEqual(seen["stderr"], subprocess.STDOUT)

    def test_the_jobs_exit_code_is_passed_on_in_the_trailer_and_as_the_exit_code(self):
        self.record(schedule=self.JOBS)
        code, lines = self.run_job("work", rc=1)
        self.assertEqual((code, lines[-1]["ok"], lines[-1]["exit_code"]), (1, True, 1))

    def test_an_unknown_job_names_the_jobs_there_are(self):
        self.record(schedule=self.JOBS)
        code, lines = self.run_job("nothing")
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["ok"], False)
        self.assertIn("demo has no job 'nothing'. Jobs: work, tick (schedule in annex.yaml)", lines[-1]["error"])
        self.assertEqual(self.docker.calls, [])

    def test_an_app_that_is_not_deployed(self):
        (self.d / "deploy.json").unlink(missing_ok=True)
        code, lines = self.run_job()
        self.assertEqual((code, lines[-1]["ok"]), (1, False))
        self.assertIn("No app named 'demo'", lines[-1]["error"])

    def test_a_run_does_not_take_the_lock_so_it_works_beside_a_deploy(self):
        self.record(schedule=self.JOBS)
        self.hold()
        code, _ = self.run_job("work")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
