"""The other operations that replace an app's container, on the VM (control-plane V2, D102): `secret set --recreate`,
`remove`; and `run-job`, which only runs a command in it.

Each of the first two is one process: it takes the app's lock (the flock on deploy/<app>/.lock.guard that deploys
hold, D101), so it can't interleave with a deploy, a rollback or another of them; it writes deploy/<app>/current.json
like a deploy does (kind, actor, step) so a refused caller is told who holds the lock; it streams the deploy's line
protocol on stdout ({"progress": ..., "step": ...}, {"note": ...}, one last {"ok": ...}); and it ignores SIGHUP and
SIGPIPE, so a dropped SSH session doesn't stop it half-way through (a half-removed app is worse than a slow one).
There is no detaching fork and no run.log: these finish in seconds, and the lock dies with the process.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

import annex_deploy as dep
import annex_vm as vm

LABELS = {"secret-set": "secret set", "remove": "removal"}


class Op:
    """One locked operation on one app. dep.drain() only needs `progress` and `kind` from it."""

    def __init__(self, base: Path, app: str, command: str, actor: str, out):
        self.base, self.app, self.command, self.actor, self.out = base, app, command, actor, out
        self.kind = LABELS.get(command, command)
        self.at = "begin"
        self.fd: int | None = None
        now = time.time()
        self.current = {"pid": os.getpid(), "actor": actor, "via": "cli", "kind": command, "release": None,
                        "image": None, "step": "begin", "step_at": now, "started_at": now, "started": vm._iso(now)}

    def take(self) -> None:
        """The app's lock (LockHeld, naming the holder, when another run has it), then this run's current.json."""
        self.fd = vm.hold_app_lock(self.base, self.app)
        previous = vm.read_current(self.base, self.app)
        # A deploy that died mid-way left its step in current.json, and the next deploy reads it to say where it
        # stopped. Keep that record where dead_step() looks (D102).
        if previous and previous.get("result") is None and previous.get("kind") in ("deploy", "rollback"):
            self.current["interrupted"] = previous
        elif previous and previous.get("interrupted"):
            self.current["interrupted"] = previous["interrupted"]
        self.write_current()

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write_current(self) -> None:
        vm.write_current(self.base, self.app, self.current)

    def step(self, name: str) -> None:
        self.at = name
        self.current.update(step=name, step_at=time.time())
        self.write_current()

    def emit(self, obj: dict) -> None:
        if self.out is None:
            return
        try:
            self.out.write(json.dumps(obj, default=str) + "\n")
            self.out.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.out = None  # the caller left: carry on, say nothing

    def progress(self, msg: str) -> None:
        self.emit({"progress": msg, "step": self.at})

    def note(self, msg: str) -> None:
        self.emit({"note": msg})

    def finish(self, result: str) -> None:
        self.current.update(result=result, ended_at=time.time())
        try:
            self.write_current()
        except OSError:
            pass


def failure_message(op: Op, e: BaseException) -> str:
    if isinstance(e, dep.Terminated):
        return f"the {op.kind} process on the VM was stopped (SIGTERM) at the {op.at} step"
    if isinstance(e, KeyboardInterrupt):
        return f"the {op.kind} process on the VM was interrupted at the {op.at} step"
    if isinstance(e, vm.Fail):
        return str(e)
    if isinstance(e, subprocess.CalledProcessError):
        return f"{' '.join(map(str, e.cmd))[:200]} failed (exit {e.returncode})"
    traceback.print_exc()
    return f"{type(e).__name__}: {e} (a bug in Annex; the VM process's stderr has the traceback)"


def serve(command: str, a, pairs: list[tuple[str, str]] | None = None, stdout=None, base: Path | None = None,
          signals: bool = True) -> int:
    """annex_vm.py secret-set --recreate | remove. A held lock raises vm.LockHeld before any line
    (annex_vm prints it as the one {"ok": false} line, as a deploy's refusal); anything after the lock is a final
    line either way."""
    stdout = stdout if stdout is not None else sys.stdout
    base = base if base is not None else vm.DATA
    if signals:  # the tests keep their own process's handlers
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, dep._terminate)
    op = Op(base, a.app, command, a.actor, stdout)
    op.take()
    try:
        job: Callable[[], dict]
        if command == "secret-set":
            job = lambda: secret_set(op, pairs or [])  # noqa: E731
        else:
            job = lambda: remove(op, a.keep_data)  # noqa: E731
        res = job()
    except BaseException as e:  # noqa: BLE001
        op.finish("failed")
        op.emit({"ok": False, "error": failure_message(op, e), "step": op.at})
        op.release()
        return 1
    op.finish("ok")
    op.emit({"ok": True, **res})
    op.release()
    return 0


# --- the operations: each runs with the lock held ------------------------------------------------------------------
def read_record(base: Path, app: str) -> dict | None:
    try:
        rec = json.loads((base / "deploy" / app / "deploy.json").read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def compose_up(op: Op, *extra: str) -> None:
    d = op.base / "deploy" / op.app
    up = vm.run(["docker", "compose", "up", "-d", *extra], cwd=str(d), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
    if up.returncode != 0:
        tail = "\n".join((up.stdout or "").strip().splitlines()[-40:])
        raise vm.Fail(f"docker compose up failed for {op.app}:\n{tail}")


def secret_set(op: Op, pairs: list[tuple[str, str]]) -> dict:
    """Store the values; when the app is deployed, write its runtime env again (provision, from deploy.json) and
    recreate the container after a drain. The values never leave this process's memory and the env file."""
    app = op.app
    op.step("store")
    keys, all_keys = vm.store_secrets(app, pairs, op.actor)
    res: dict = {"set": keys, "all_keys": all_keys}
    record = read_record(op.base, app)
    if not record:
        res["applied"] = "stored; injected on the next deploy"
        return res
    undeclared = [k for k in keys if k not in record.get("secrets", [])]
    op.step("provision")
    op.progress("Writing the app's environment")
    webhooks = record.get("webhooks", [])
    vm.provision(app, postgres=record["postgres"], files=record["files"], views=record["views"],
                 secrets=record["secrets"], notify_email=record.get("notify_email", False),
                 slack=record.get("notify_slack", []), hooks=webhooks, hook_port=(record.get("port") or 0) if webhooks else 0)
    dep.drain(op, app)
    op.step("switch")
    op.progress(f"Restarting {dep.container(app)} with the new values")
    compose_up(op)
    res["applied"] = "restarted with new values"
    if undeclared:
        res["warning"] = (f"{', '.join(undeclared)} not declared under secrets: in annex.yaml, "
                          "so not injected. Add them there and deploy again")
    return res


def app_containers(app: str) -> list[str]:
    r = vm.run(["docker", "ps", "-aq", "--filter", f"label=com.kaldi.annex.app={app}"], capture_output=True, text=True)
    return (r.stdout or "").split() if r.returncode == 0 else []


def remove(op: Op, keep_data: bool) -> dict:
    app, d = op.app, op.base / "deploy" / op.app
    record = read_record(op.base, app)
    if not record and not app_containers(app):
        raise vm.Fail(f"No app named {app!r}. See deployed apps with: annex list")
    op.step("drain")
    dep.drain(op, app)
    op.step("down")
    op.progress(f"Removing {dep.container(app)} and its image")
    if (d / "compose.yaml").is_file():
        down = vm.run(["docker", "compose", "down", "--remove-orphans"], cwd=str(d), stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
        if down.returncode != 0:
            tail = "\n".join((down.stdout or "").strip().splitlines()[-40:])
            raise vm.Fail(f"docker compose down failed for {app}:\n{tail}")
    else:
        vm.run(["docker", "rm", "-f", dep.container(app)], capture_output=True, text=True)
    ids = vm.run(["docker", "image", "ls", "-q", f"annex-app/{app}"], capture_output=True, text=True).stdout.split()
    if ids:
        vm.run(["docker", "rmi", "-f", *dict.fromkeys(ids)], capture_output=True, text=True)  # best effort
    op.step("deprovision")
    op.progress("Removing its data access" + ("" if keep_data else ", database, files and secrets"))
    res = vm.deprovision(app, op.actor, keep_data)  # keeps .lock.guard: see vm.clear_deploy_dir
    if record and record.get("schedule"):
        op.step("scheduler")
        r = vm.run(["docker", "restart", dep.OFELIA], capture_output=True, text=True)
        if r.returncode != 0:
            op.note(f"Warning: couldn't restart the scheduler ({dep.OFELIA}); its jobs for {app} stay in its memory "
                    f"until it restarts")
    return res


# --- run-job: no lock (it replaces nothing) -----------------------------------------------------------------------
def run_job(a, base: Path | None = None, stdout=None, stderr=None) -> int:
    """Run the job's command, from deploy.json, in the app's container. Its output goes to stdout as it comes; the
    last line of stderr is the trailer the CLI reads, and the exit code is the job's (1 for a refusal)."""
    base = base if base is not None else vm.DATA
    stderr = stderr if stderr is not None else sys.stderr
    record = read_record(base, a.app)
    if not record:
        return refuse(stderr, f"No app named {a.app!r}. See deployed apps with: annex list")
    jobs = {j["name"]: j for j in record.get("schedule", [])}
    if a.job not in jobs:
        return refuse(stderr, f"{a.app} has no job {a.job!r}. Jobs: {', '.join(jobs) or 'none'} (schedule in annex.yaml)")
    t0 = time.time()
    # stdout=None: the job's output is this process's own, unbuffered by us; stderr into it, as `2>&1` was
    res = vm.run(["docker", "exec", dep.container(a.app), *jobs[a.job]["command"]], stdout=stdout, stderr=subprocess.STDOUT)
    stderr.write(json.dumps({"ok": True, "exit_code": res.returncode, "seconds": round(time.time() - t0, 1)}) + "\n")
    stderr.flush()
    return res.returncode if 0 <= res.returncode < 256 else 1


def refuse(stderr, message: str) -> int:
    stderr.write(json.dumps({"ok": False, "error": message}) + "\n")
    stderr.flush()
    return 1
