"""Talking to the Annex platform: the VM over SSH (D1), or the local stack on this machine's Docker (D99)."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
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
    step: str | None = None  # what a deploy or rollback is doing, for the timeout message (set by ops._Lock)
    local = False            # the local stack (D99) instead of the VM
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
        step = f" at the {self.step} step" if self.step else ""
        hung = "the command" if self.local else "the SSH connection or the command"
        return (f"{self.where[0].upper()}{self.where[1:]} didn't answer within {timeout} s{step}: {hung} hung. "
                "If a deploy or rollback was running, Annex has tried to record it as failed and release its lock; "
                "if the VM was unreachable, the lock expires after 30 minutes and the next deploy of the app takes it "
                "over and reconciles the release it left (see annex releases <app>)")

    def path(self, *parts: str) -> str:
        """A path under the data directory, quoted for use in a script."""
        return shlex.quote("/".join((self.data, *parts)))

    def out(self, script: str, **kw) -> str:
        return self.run(script, **kw).stdout.decode(errors="replace")

    def stream(self, script: str) -> int:
        """Run a script with output going straight to the terminal (logs -f)."""
        return subprocess.run(self._argv(script, tty=True)).returncode

    def vm(self, *args: str, input: str | None = None, timeout: int = 900) -> dict:
        """Call vm/annex_vm.py on the VM; it always answers with one JSON object."""
        cmd = "python3 " + shlex.quote(f"{self.data}/platform/vm/annex_vm.py") + " " + " ".join(map(shlex.quote, args))
        r = self.run(cmd, input=input, check=False, timeout=timeout)
        try:
            res = json.loads(r.stdout.decode().strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise AnnexError(f"annex_vm {args[0]} failed: {self._msg(r)}") from None
        if not res.get("ok", True):
            raise AnnexError(res.get("error", "unknown error"))
        return res

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
