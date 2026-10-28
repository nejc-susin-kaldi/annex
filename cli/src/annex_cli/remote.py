"""Talking to the Annex platform: the VM over SSH (D1), or the local stack on this machine's Docker (D99)."""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
from functools import cached_property
from pathlib import Path

REPO = Path(os.environ.get("ANNEX_REPO", Path(__file__).resolve().parents[3]))
ENVS = {"vm": ".env", "local": ".env.local"}  # ANNEX_ENV -> settings file, as in scripts/lib.sh and render.py
TARGET_KEYS = ("ANNEX_TARGET", "ANNEX_SSH_HOST", "ANNEX_DATA_DIR", "ANNEX_DOCKER_CONTEXT")
FILE_ONLY = ("ANNEX_TARGET", "ANNEX_DOCKER_CONTEXT")


class AnnexError(Exception):
    """An error with a message meant for the user or the agent."""


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a settings file (comments and blank lines skipped, quotes stripped), as render.py reads it."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def target_settings(environ=None, repo: Path | None = None) -> dict[str, str]:
    """Where the platform is, from the settings file ANNEX_ENV selects: the file scripts/lib.sh sources, so `./annex`
    and the scripts agree (D99). The file wins over the process environment, as in lib.sh; the environment only fills
    keys the file leaves out, except ANNEX_TARGET and ANNEX_DOCKER_CONTEXT, which come from the file alone (a value
    left in a shell by the other stack must never redirect this one)."""
    environ = os.environ if environ is None else environ
    repo = REPO if repo is None else repo
    name = environ.get("ANNEX_ENV") or "vm"
    if name not in ENVS:
        raise AnnexError(f"ANNEX_ENV={name} must be vm (the default: .env) or local (.env.local)")
    path = repo / ENVS[name]
    if not path.exists():
        path = repo / (ENVS[name] + ".example")
    conf = read_env_file(path) if path.exists() else {}
    out = {"ANNEX_ENV": name}
    for k in TARGET_KEYS:
        out[k] = conf.get(k) or ("" if k in FILE_ONLY else environ.get(k, ""))
    out["ANNEX_TARGET"] = out["ANNEX_TARGET"] or "ssh"
    if out["ANNEX_TARGET"] not in ("ssh", "local"):
        raise AnnexError(f"ANNEX_TARGET={out['ANNEX_TARGET']} in {path.name} must be ssh (the VM) or local")
    out["ANNEX_SSH_HOST"] = out["ANNEX_SSH_HOST"] or "annex"
    out["ANNEX_DATA_DIR"] = str(Path(os.path.expandvars(out["ANNEX_DATA_DIR"] or "/srv/annex")).expanduser())
    return out


def docker_endpoint(context: str) -> str:
    r = subprocess.run(["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AnnexError(f"No Docker context '{context}' (ANNEX_DOCKER_CONTEXT in .env.local); "
                         "`docker context ls` lists them")
    return r.stdout.strip()


class Remote:
    local = False            # the local stack (D99) instead of the VM
    python = "python3"       # the interpreter of annex_vm.py (on the local stack it resolves through the prefix's PATH)
    env_name = "vm"

    def __init__(self, environ=None) -> None:
        environ = os.environ if environ is None else environ
        t = target_settings(environ)
        self.env_name = t["ANNEX_ENV"]
        self.local = t["ANNEX_TARGET"] == "local"
        self.host = t["ANNEX_SSH_HOST"]
        self.data = t["ANNEX_DATA_DIR"]
        self.docker_context = t["ANNEX_DOCKER_CONTEXT"]
        ctl = REPO / ".build"
        ctl.mkdir(exist_ok=True)
        # one multiplexed connection per minute instead of a new two-hop SSH for every call
        self.ssh = ["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}/ssh-%C",
                    "-o", "ControlPersist=120", "-o", "BatchMode=yes", self.host]
        if self.local:
            # The local target is only another prefix: `env ... bash -c SCRIPT` instead of `ssh HOST "bash -c SCRIPT"`,
            # so every method (and any later one built on _argv) runs the same script on either. The environment is
            # what the VM gives implicitly: the data dir for annex_vm.py, the Docker context (exported for these
            # commands only, never switching the user's current one), and this CLI's own Python first on PATH, so
            # `python3 annex_vm.py` finds PyYAML (pinned in cli/pyproject.toml) whatever the system python3 has.
            env = ["env"]
            if self.docker_context:
                env += ["-u", "DOCKER_HOST", f"DOCKER_CONTEXT={self.docker_context}"]
            path = environ.get("PATH", os.defpath)
            env += [f"ANNEX_DATA_DIR={self.data}", f"PATH={Path(sys.executable).parent}{os.pathsep}{path}"]
            self.prefix = [*env, "bash", "-c"]

    @property
    def where(self) -> str:
        return f"the local stack ({self.data})" if self.local else f"the VM ({self.host})"

    @cached_property
    def _docker_checked(self) -> bool:
        """A local stack only ever talks to a local daemon: a context that reaches the VM is refused."""
        if self.docker_context:
            ep = docker_endpoint(self.docker_context)
            if not ep.startswith(("unix://", "npipe://")):
                raise AnnexError(f"Docker context '{self.docker_context}' points at {ep}, not a local socket. "
                                 "Set ANNEX_DOCKER_CONTEXT in .env.local to a local context (desktop-linux)")
        return True

    def _argv(self, script: str, tty: bool = False) -> list[str]:
        """The command line that runs a bash script where the platform is (the VM over ssh, or here)."""
        if self.local:
            if not self._docker_checked:  # pragma: no cover (raises instead)
                raise AnnexError("no local Docker")
            return [*self.prefix, script]
        return [*self.ssh, *(["-t"] if tty else []), "bash -c " + shlex.quote(script)]

    def hint(self, cmd: str) -> str:
        """A command for a human to run where the platform is, for error messages."""
        return cmd if self.local else f"ssh {self.host} {cmd}"

    def run(self, script: str, *, input: bytes | str | None = None, check: bool = True,
            timeout: int = 900) -> subprocess.CompletedProcess:
        """Run a bash script on the VM (or the local stack) and capture its output."""
        data = input.encode() if isinstance(input, str) else input
        try:
            r = subprocess.run(self._argv(script), input=data, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AnnexError(self.timeout_message(timeout)) from None
        if check and r.returncode != 0:
            raise AnnexError(self._msg(r))
        return r

    def timeout_message(self, timeout: int) -> str:
        hung = "the command" if self.local else "the SSH connection or the command"
        return (f"{self.where[0].upper()}{self.where[1:]} didn't answer within {timeout} s: {hung} hung. "
                "Deploys and the other container-replacing commands don't use this limit (they run on the VM and "
                "finish there, D101); this was a read or a short call")

    def path(self, *parts: str) -> str:
        """A path under the data directory, quoted for use in a script."""
        return shlex.quote("/".join((self.data, *parts)))

    def out(self, script: str, **kw) -> str:
        return self.run(script, **kw).stdout.decode(errors="replace")

    def stream(self, script: str) -> int:
        """Run a script with output going straight to the terminal (logs -f)."""
        return subprocess.run(self._argv(script, tty=True)).returncode

    def vm_command(self, *args: str) -> str:
        """`python3 <data>/platform/vm/annex_vm.py ARGS`, quoted. `python3` is self.python: the VM's stock python3, or
        on the local stack the CLI's own interpreter, first on the prefix's PATH (it has PyYAML, D99)."""
        return " ".join([self.python, shlex.quote(f"{self.data}/platform/vm/annex_vm.py"), *map(shlex.quote, args)])

    def vm(self, *args: str, input: str | None = None) -> dict:
        """Call platform/vm/annex_vm.py on the VM; it always answers with one JSON object."""
        cmd = self.vm_command(*args)
        r = self.run(cmd, input=input, check=False)
        try:
            res = json.loads(r.stdout.decode().strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise AnnexError(f"annex_vm {args[0]} failed: {self._msg(r)}") from None
        if not res.get("ok", True):
            raise AnnexError(res.get("error", "unknown error"))
        return res

    def call(self, *args: str, input: bytes | str | None = None, progress=None, silent: int = 180) -> dict:
        """Run an `annex_vm.py` command that streams JSON lines (deploy, rollback; D101) and return its final
        {"ok": true, ...} object. Progress and note lines go to `progress` as they arrive; heartbeats only prove the
        connection is alive. `silent` is the longest gap between lines (the VM sends a heartbeat every 30 s), not a
        limit on the whole run, so a long build isn't cut. {"ok": false} raises AnnexError with its error. A
        connection that drops, goes silent or is interrupted (Ctrl-C) after the run started raises the detach message:
        the run carries on on the VM."""
        cmd = self.vm_command(*args)
        data = input.encode() if isinstance(input, str) else (input or b"")
        proc = subprocess.Popen(self._argv(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines: queue.Queue = queue.Queue()
        err: list[bytes] = []

        def send() -> None:
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                pass  # the VM refused before reading it all: its answer says why
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        def read() -> None:
            for raw in proc.stdout:
                lines.put(raw)
            lines.put(None)

        def drain_err() -> None:
            for raw in proc.stderr:
                err.append(raw)
                del err[:-40]

        for target in (send, read, drain_err):
            threading.Thread(target=target, daemon=True).start()
        app = args[1] if len(args) > 1 else "the app"
        step, tail = None, []
        try:
            while True:
                try:
                    raw = lines.get(timeout=silent)
                except queue.Empty:
                    proc.kill()
                    raise AnnexError(self._detached(args[0], app, step, f"the VM sent nothing for {silent} s")) from None
                if raw is None:
                    break
                text = raw.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                if not isinstance(obj, dict):
                    tail = (tail + [text])[-40:]
                    continue
                if "ok" in obj:
                    self._finish(proc)
                    if obj["ok"]:
                        return obj
                    raise AnnexError(obj.get("error") or "unknown error")
                step = obj.get("step") or step
                message = obj.get("progress") or obj.get("note")
                if message and progress:
                    progress(message)
        except KeyboardInterrupt:
            proc.kill()
            raise AnnexError(self._detached(args[0], app, step, "interrupted")) from None
        self._finish(proc)
        if step is None:  # it never started: say what the VM said
            detail = "\n".join(tail) or b"".join(err).decode(errors="replace").strip() or f"exit code {proc.returncode}"
            if "invalid choice" in detail:
                detail += "\nThe platform on the VM is older than this CLI: run scripts/push.sh first"
            raise AnnexError(f"annex_vm {args[0]} failed on {self.host}: {detail}")
        raise AnnexError(self._detached(args[0], app, step, "the connection to the VM dropped"))

    @staticmethod
    def _finish(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    @staticmethod
    def _detached(kind: str, app: str, step: str | None, why: str) -> str:
        if step in (None, "begin", "upload"):
            return (f"The {kind} of {app} stopped before it was under way on the VM ({why}, at the "
                    f"{step or 'first'} step). If it had started, the VM records it as failed and nothing was "
                    f"switched: `annex status {app}` and `annex releases {app}` show where it is")
        if kind not in ("deploy", "rollback"):  # secret-set, remove (D102): no release to look up
            what = {"secret-set": "secret set", "remove": "removal"}.get(kind, kind)
            return (f"The connection to the VM dropped at the {step} step ({why}); the {what} continues on the VM "
                    f"and finishes there. `annex status {app}` shows its step while it runs")
        return (f"The connection to the VM dropped at the {step} step ({why}); the {kind} continues on the VM. "
                f"`annex status {app}` shows its step and `annex releases {app}` its outcome")

    @cached_property
    def settings(self) -> dict[str, str]:
        """The deployed platform's settings (.env written by scripts/render.py)."""
        r = self.run(f"cat {self.data}/platform/.env", check=False)
        if r.returncode != 0:
            pre = "" if self.env_name == "vm" else f"ANNEX_ENV={self.env_name} "
            raise AnnexError(f"No Annex platform found on {self.where} ({self.data}/platform/.env). "
                             f"Run {pre}scripts/bootstrap.sh and {pre}scripts/push.sh first.")
        env = {}
        for line in r.stdout.decode().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k] = v
        return env

    def host_for(self, app: str) -> str:
        return self.settings["ANNEX_HOST_PATTERN"].replace("{name}", app)

    def url_for(self, app: str) -> str:
        return f"https://{self.host_for(app)}{self.settings.get('ANNEX_PORT_SUFFIX', '')}"

    @staticmethod
    def _msg(r: subprocess.CompletedProcess) -> str:
        text = (r.stderr or b"").decode(errors="replace").strip() or (r.stdout or b"").decode(errors="replace").strip()
        lines = text.splitlines()
        return "\n".join(lines[-40:]) or f"exit code {r.returncode}"
