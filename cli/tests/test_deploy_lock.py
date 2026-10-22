"""The deploy lock in the CLI (C1, D92): deploy and rollback take it before anything changes on the VM, record every
step, and end it on every exit path, a failure (or Ctrl-C) as `failed`. Runs against a fake Remote."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from annex_cli import ops
    from annex_cli.remote import AnnexError, Remote
except ImportError as e:  # the CLI's dependencies (PyYAML) aren't in this Python
    raise unittest.SkipTest(f"annex_cli not importable here: {e}")

TOKEN = "ab" * 16
LIVE4 = {"release": 4, "image": "annex-app/demo:r4", "result": "live", "deployed_at": "2026-10-22T10:00:00+0200",
         "deployed_by": "sam@mac", "via": "cli", "manifest": None}
LIVE3 = {**LIVE4, "release": 3, "image": "annex-app/demo:r3"}


class FakeRemote:
    """Records annex_vm calls; `fail` maps an annex_vm command or a step name to the exception it raises."""
    host, data, step, views = "annex", "/srv/annex", None, []
    settings = {"ANNEX_TLS_MODE": "local", "ANNEX_HOST_PATTERN": "{name}.annex.home.arpa", "ANNEX_URL_HOOKS": ""}

    def __init__(self, releases=(), fail=None, notes=()):
        self.releases, self.fail, self.notes = list(releases), dict(fail or {}), list(notes)
        self.calls: list[tuple] = []

    def vm(self, *args, input=None, timeout=900):
        self.calls.append((*args, json.loads(input) if input else None))
        if args[0] in self.fail:
            raise self.fail[args[0]]
        if args[0] == "deploy-step" and args[3] in self.fail:
            raise self.fail[args[3]]
        if args[0] == "deploy-begin":
            return {"release": 5, "image": json.loads(input)["line"].get("image") or "annex-app/demo:r5",
                    "token": TOKEN, "reconciled": [{"release": 9, "result": "failed", "message": m} for m in self.notes],
                    "took_over": None, "warnings": []}
        return {
            "views": {"views": self.views},
            "snapshot": {"snapshot": {"name": "pre-r5"}},
            "snapshots": {"snapshots": [{"from_release": 3}]},
            "provision": {"injected": []},
        }.get(args[0], {"ok": True})

    def run(self, script, *, input=None, check=True, timeout=900):
        out = b""
        if "releases.jsonl" in script:
            out = "".join(json.dumps(x) + "\n" for x in self.releases).encode()
        elif script.startswith("docker build") and "build" in self.fail:
            raise self.fail["build"]
        elif ".Config.User" in script:
            out = b"10001"
        elif ".Config.Labels" in script:
            out = b"{}"
        elif "docker image ls" in script:
            out = "\n".join(x["image"] for x in self.releases).encode()
        elif "cat " in script and "deploy.json" in script and "cat >" not in script:
            return subprocess.CompletedProcess(script, 1, b"", b"")
        return subprocess.CompletedProcess(script, 0, out, b"")

    def out(self, script, **kw):
        return self.run(script, **kw).stdout.decode()

    def path(self, *parts):
        return "/".join((self.data, *parts))

    def host_for(self, app):
        return f"{app}.annex.home.arpa"

    def url_for(self, app):
        return f"https://{app}.annex.home.arpa"

    # what the tests read
    def names(self):
        return [c[0] if c[0] != "deploy-step" else f"step:{c[3]}" for c in self.calls]

    def ends(self):
        return [c for c in self.calls if c[0] == "deploy-end"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = ops.init("python-web", "demo", Path(self.tmp.name) / "demo", "sam")
        manifest = ops.mf.load(self.folder).to_dict()
        LIVE4["manifest"] = LIVE3["manifest"] = manifest
        FakeRemote.views = manifest["views"]
        for p in (mock.patch.object(ops, "_git", return_value=None),
                  mock.patch.object(ops, "_warnings", return_value=[]),
                  mock.patch.object(ops, "_drain")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def deploy(self, fake):
        with mock.patch.object(ops, "Remote", return_value=fake):
            return ops.deploy(self.folder)

    def rollback(self, fake, **kw):
        with mock.patch.object(ops, "Remote", return_value=fake):
            return ops.rollback("demo", **kw)


class Deploy(Base):
    def test_the_lock_is_taken_before_the_upload_and_ended_live_last(self):
        fake = FakeRemote(releases=[LIVE4])
        res = self.deploy(fake)
        names = fake.names()
        self.assertEqual(names[:4], ["views", "deploy-begin", "step:upload", "step:build"])
        self.assertLess(names.index("step:snapshot"), names.index("snapshot"))
        self.assertEqual([n for n in names if n.startswith("step:")],
                         ["step:upload", "step:build", "step:snapshot", "step:provision", "step:switch", "step:gate",
                          "step:finish"])
        self.assertEqual(names[-1], "deploy-end")
        app, token, n, result, line = fake.ends()[0][1:]
        self.assertEqual((app, token, n, result), ("demo", TOKEN, "5", "live"))
        self.assertEqual((line["release"], line["image"], line["snapshot"], line["result"]),
                         (5, "annex-app/demo:r5", "pre-r5", "live"))
        self.assertEqual((res["release"], res["image"]), (5, "annex-app/demo:r5"))

    def test_begin_gets_the_line_and_the_record_without_number_or_image(self):
        fake = FakeRemote()
        self.deploy(fake)
        begin = next(c for c in fake.calls if c[0] == "deploy-begin")
        self.assertEqual(begin[1:3], ("demo", "--actor"))
        body = begin[-1]
        self.assertNotIn("release", body["line"])
        self.assertNotIn("image", body["line"])
        self.assertEqual(body["line"]["manifest"]["name"], "demo")
        self.assertIsNone(body["record"]["release"])
        self.assertEqual(body["record"]["url"], "https://demo.annex.home.arpa")

    def test_an_exception_after_begin_ends_the_release_as_failed_and_propagates(self):
        boom = AnnexError("provision failed: no such view")
        fake = FakeRemote(releases=[LIVE4], fail={"provision": boom})
        with self.assertRaises(AnnexError) as cm:
            self.deploy(fake)
        self.assertIs(cm.exception, boom)
        [end] = fake.ends()
        self.assertEqual(end[4], "failed")
        self.assertEqual(end[5]["interrupted_at"], "provision")
        self.assertIn("broke off at provision: provision failed", end[5]["error"])

    def test_a_build_failure_is_a_failed_release_too(self):
        fake = FakeRemote(fail={"build": AnnexError("Docker build failed for demo.\nstep 3")})
        with self.assertRaises(AnnexError):
            self.deploy(fake)
        self.assertEqual(fake.ends()[0][5]["error"], "broke off at build: Docker build failed for demo.")

    def test_ctrl_c_at_the_gate_still_ends_the_release(self):
        fake = FakeRemote(releases=[LIVE4], fail={"gate": KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            self.deploy(fake)
        self.assertEqual(fake.ends()[0][4], "failed")
        self.assertEqual(fake.ends()[0][5]["interrupted_at"], "gate")

    def test_an_unreachable_vm_at_the_end_does_not_hide_the_first_error(self):
        boom = AnnexError("provision failed")
        fake = FakeRemote(fail={"provision": boom, "deploy-end": AnnexError("ssh: connection refused")})
        with self.assertRaises(AnnexError) as cm:
            self.deploy(fake)
        self.assertIs(cm.exception, boom)

    def test_a_held_lock_stops_the_deploy_before_anything_changes(self):
        fake = FakeRemote(fail={"deploy-begin": AnnexError("demo is being deployed or rolled back by kim@mac (...)")})
        with self.assertRaises(AnnexError) as cm:
            self.deploy(fake)
        self.assertIn("kim@mac", str(cm.exception))
        self.assertEqual(fake.names(), ["views", "deploy-begin"])

    def test_a_failed_gate_with_a_live_release_ends_failed_once_with_rolled_back_to(self):
        fake = FakeRemote(releases=[LIVE4])
        with mock.patch.object(ops, "_gate", side_effect=[("GET /healthz did not return 200", "log"), None]), \
                self.assertRaises(AnnexError) as cm:
            self.deploy(fake)
        self.assertIn("rolled back to r4", str(cm.exception))
        [end] = fake.ends()
        self.assertEqual((end[4], end[5]["rolled_back_to"], end[5]["error"]),
                         ("failed", 4, "GET /healthz did not return 200"))
        self.assertIn("step:back-provision", fake.names())  # the old manifest is provisioned again

    def test_reconcile_notes_are_shown_and_returned(self):
        seen = []
        fake = FakeRemote(notes=["r9 of demo was interrupted at gate"])
        with mock.patch.object(ops, "Remote", return_value=fake):
            res = ops.deploy(self.folder, seen.append)
        self.assertIn("r9 of demo was interrupted at gate", seen)
        self.assertIn("r9 of demo was interrupted at gate", res["warnings"])


class Rollback(Base):
    def test_rollback_locks_with_the_targets_image_and_ends_live(self):
        fake = FakeRemote(releases=[LIVE3, LIVE4])
        res = self.rollback(fake)
        begin = next(c for c in fake.calls if c[0] == "deploy-begin")
        self.assertEqual(begin[-1]["line"]["image"], "annex-app/demo:r3")
        self.assertEqual(begin[-1]["line"]["rollback"], {"from": 4, "to": 3, "with_data": False})
        self.assertEqual(fake.names()[-1], "deploy-end")
        self.assertEqual(fake.ends()[0][4], "live")
        self.assertEqual(res["release"], 5)

    def test_a_bad_target_is_refused_before_locking(self):
        fake = FakeRemote(releases=[LIVE3, LIVE4])
        with self.assertRaises(AnnexError):
            self.rollback(fake, to=99)
        self.assertNotIn("deploy-begin", fake.names())

    def test_an_exception_ends_the_rollback_as_failed(self):
        fake = FakeRemote(releases=[LIVE3, LIVE4], fail={"snapshot": AnnexError("disk full")})
        with self.assertRaises(AnnexError):
            self.rollback(fake)
        self.assertEqual((fake.ends()[0][4], fake.ends()[0][5]["interrupted_at"]), ("failed", "snapshot"))

    def test_the_live_release_is_a_target_only_when_its_container_is_not_running(self):
        fake = FakeRemote(releases=[LIVE3, LIVE4])
        with mock.patch.object(ops, "_running_image", return_value="annex-app/demo:r4"), self.assertRaises(AnnexError):
            self.rollback(fake, to=4)
        with mock.patch.object(ops, "_running_image", return_value="annex-app/demo:r5"):
            res = self.rollback(fake, to=4)
        self.assertEqual((res["rolled_back_to"], res["image"]), (4, "annex-app/demo:r4"))


class Timeout(unittest.TestCase):
    def test_a_timeout_names_the_step_and_the_lock(self):
        r = Remote.__new__(Remote)
        r.host, r.ssh, r.step = "annex", ["true"], "build"
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 900)), \
                self.assertRaises(AnnexError) as cm:
            r.run("docker build .")
        msg = str(cm.exception)
        self.assertIn("at the build step", msg)
        self.assertIn("30 minutes", msg)


if __name__ == "__main__":
    unittest.main()
