"""Shared fakes for the VM-side deploy tests (vm/annex_deploy.py, D101): a stand-in for annex_vm.run that answers the
Docker commands a deploy sends, a Run on a temp data dir, and a deploy's stdin (meta line + tar.gz)."""
import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_deploy as dep  # noqa: E402
import annex_vm as vm  # noqa: E402

MB = 1024 ** 2
SETTINGS = {"ANNEX_HOST_PATTERN": "{name}.annex.home.arpa", "ANNEX_PORT_SUFFIX": ":8443", "ANNEX_TLS_MODE": "local",
            "ANNEX_URL_HOOKS": "https://hooks.annex.home.arpa:8443", "ANNEX_PUBLIC_PORT": "8443"}


def manifest(**kw):
    base = dict(name="demo", owner="sam", description="", type="web", port=8080, healthcheck="/healthz",
                postgres=False, files=False, views=[], schedule=[], env={}, secrets=[], egress=[])
    return dep.mf.Manifest(**{**base, **kw})


class FakeDocker:
    """annex_vm.run for a deploy: records each argv; `gate` is the list of return codes of the health probe (then 0),
    `fail` substrings of the joined argv that return 1."""

    def __init__(self, size=100 * MB, user="10001", labels="{}", build_rc=0, gate=(), images=(), running="",
                 fail=(), status="running", slack=True, logins=("sam",)):
        self.size, self.user, self.labels, self.build_rc = size, user, labels, build_rc
        self.gate, self.images, self.running, self.fail = list(gate), list(images), running, fail
        self.status, self.slack, self.logins = status, slack, list(logins)
        self.calls: list[list[str]] = []

    def __call__(self, argv, input=None, capture_output=False, text=False, check=False, stdin=None, stdout=None,
                 stderr=None, cwd=None, timeout=None):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        joined = " ".join(argv)
        rc, out = (1 if any(f in joined for f in self.fail) else 0), ""
        if argv[:2] == ["docker", "build"]:
            rc = rc or self.build_rc
            if hasattr(stdout, "write"):
                stdout.write("#1 load build definition\n" + ("ERROR: pip install failed\n" if rc else "done\n"))
        elif argv[:3] == ["docker", "image", "inspect"]:
            fmt = argv[4]
            out = str(self.size) if "Size" in fmt else self.user if "User" in fmt else self.labels
        elif argv[:3] == ["docker", "image", "ls"]:
            out = "\n".join(self.images)
        elif argv[:2] == ["docker", "exec"] and "wget" in argv:
            rc = rc or (self.gate.pop(0) if self.gate else 0)
        elif argv[:2] == ["docker", "inspect"] and "{{.State.Status}}" in argv:
            out = self.status
        elif argv[:2] == ["docker", "inspect"] and "{{.Config.Image}}" in argv:
            out, rc = self.running, (0 if self.running else 1)
        elif argv[:2] == ["docker", "inspect"]:
            out = "healthy"
        elif argv[0] == "curl":
            out = "302"
        elif "annex_ops.ctl" in argv:
            if "logs-drain" in argv:
                out = json.dumps({"stored": True})
            elif "info" in argv:
                out = json.dumps({"slack_connected": self.slack})
            elif "users" in argv:
                out = json.dumps(self.logins)
        elif argv[:2] == ["docker", "logs"]:
            out = '{"level": "info", "msg": "started"}\n'
        if text:
            return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")
        return subprocess.CompletedProcess(argv, rc, out.encode() if out else b"", b"boom" if rc else b"")

    def ran(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


def source_tar(files=None) -> bytes:
    files = files or {"Dockerfile": "FROM scratch\nUSER 10001\n", "app.py": "print('hi')\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo("./" + name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def deploy_stdin(m, overrides=None, tar=None) -> io.BytesIO:
    meta = {"line": {"manifest": m.to_dict(), "deployed_at": "2026-10-22T11:00:00.000+02:00",
                     "deployed_by": "kim@mac", "via": "cli", "source": "~/apps/demo", "git": None, "snapshot": None}}
    if overrides:
        meta["overrides"] = overrides
    return io.BytesIO(json.dumps(meta).encode() + b"\n" + (source_tar() if tar is None else tar))


class Base(unittest.TestCase):
    """A temp data dir as annex_vm.DATA, a FakeDocker as annex_vm.run, and the heavy annex_vm calls replaced."""
    docker_kw: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.docker = FakeDocker(**self.docker_kw)
        self.snapshots: list[tuple] = []
        self.provisioned: list[dict] = []
        patches = [
            unittest.mock.patch.object(vm, "DATA", self.base),
            unittest.mock.patch.object(vm, "SNAPSHOTS", self.base / "snapshots"),
            unittest.mock.patch.object(vm, "run", self.docker),
            unittest.mock.patch.object(vm, "provision", side_effect=self.fake_provision),
            unittest.mock.patch.object(vm, "sync_app", return_value={}),
            unittest.mock.patch.object(vm, "snapshot", side_effect=self.fake_snapshot),
            unittest.mock.patch.object(vm, "restore_snapshot", return_value={"snapshot": "pre-r4"}),
            unittest.mock.patch.object(vm, "prune_snapshots", return_value={}),
            unittest.mock.patch.object(vm, "record_release_db", return_value=None),
            unittest.mock.patch.object(vm, "after_reconcile", return_value=[]),
            unittest.mock.patch.object(vm, "maintenance"),
            unittest.mock.patch.object(dep, "sleep", lambda s: None),
            unittest.mock.patch.object(dep.shutil, "disk_usage", return_value=unittest.mock.Mock(free=100 * 1024 ** 3)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = [0.0]

        def tick():
            self.clock[0] += 1.0
            return self.clock[0]
        p = unittest.mock.patch.object(dep, "monotonic", tick)
        p.start()
        self.addCleanup(p.stop)

    def fake_provision(self, app, **kw):
        self.provisioned.append({"app": app, **kw})
        return {"env_file": "x", "injected": ["DATABASE_URL"] if kw.get("postgres") else [], "views": kw["views"]}

    def fake_snapshot(self, app, release, from_release):
        self.snapshots.append((app, release, from_release))
        return {"name": f"pre-r{release}"}

    def make_run(self, kind="deploy", meta=None, app="demo", settings=None):
        out = io.StringIO()
        run = dep.Run(self.base, app, kind, "kim@mac", "cli", meta or {}, out,
                      settings=dict(SETTINGS) if settings is None else settings)
        self.addCleanup(run.log.close)
        run.inspect = lambda a, m: None
        self.steps: list[str] = []
        original = run.step

        def step(name):
            self.steps.append(name)
            original(name)
        run.step = step
        return run

    def args(self, app="demo", **kw):
        return argparse.Namespace(app=app, actor="kim@mac", via="cli", to=kw.get("to"), with_data=kw.get("with_data", False))

    def go(self, kind, stdin, **kw) -> tuple[list[dict], "dep.Run"]:
        """serve() without the fork: prepare, then the job, then the final line; returns (lines, run)."""
        meta = dep.read_meta(stdin)
        run = self.make_run(kind, meta)
        try:
            job = (dep.prepare_deploy if kind == "deploy" else dep.prepare_rollback)(run, stdin, self.args(**kw))
            res = job()
        except BaseException as e:  # noqa: BLE001
            run.fail(e)
        else:
            run.emit({"ok": True, **res})
        return [json.loads(x) for x in run.out.getvalue().splitlines()], run

    def releases(self, app="demo"):
        return vm.read_releases(self.base, app)

    def history(self, *lines, app="demo"):
        vm.write_releases(self.base, app, list(lines))
