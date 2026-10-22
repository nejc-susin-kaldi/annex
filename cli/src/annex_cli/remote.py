"""Talking to the Annex VM over SSH (the only transport, see decision D1)."""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
from functools import cached_property
from pathlib import Path

REPO = Path(os.environ.get("ANNEX_REPO", Path(__file__).resolve().parents[3]))


class AnnexError(Exception):
    """An error with a message meant for the user or the agent."""


class Remote:
    step: str | None = None  # what a deploy or rollback is doing, for the timeout message (set by ops._Lock)

    def __init__(self) -> None:
        self.host = os.environ.get("ANNEX_SSH_HOST", "annex")
        self.data = os.environ.get("ANNEX_DATA_DIR", "/srv/annex")
        ctl = REPO / ".build"
        ctl.mkdir(exist_ok=True)
        # one multiplexed connection per minute instead of a new two-hop SSH for every call
        self.ssh = ["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}/ssh-%C",
                    "-o", "ControlPersist=120", "-o", "BatchMode=yes", self.host]

    def run(self, script: str, *, input: bytes | str | None = None, check: bool = True,
            timeout: int = 900) -> subprocess.CompletedProcess:
        """Run a bash script on the VM and capture its output."""
        data = input.encode() if isinstance(input, str) else input
        try:
            r = subprocess.run([*self.ssh, "bash -c " + shlex.quote(script)], input=data,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AnnexError(self.timeout_message(timeout)) from None
        if check and r.returncode != 0:
            raise AnnexError(self._msg(r))
        return r

    def timeout_message(self, timeout: int) -> str:
        step = f" at the {self.step} step" if self.step else ""
        return (f"The VM ({self.host}) didn't answer within {timeout} s{step}: the SSH connection or the command hung. "
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
        return subprocess.run([*self.ssh, "-t", "bash -c " + shlex.quote(script)]).returncode

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

    def _argv(self, script: str) -> list[str]:
        """The command that runs a bash script on the VM (the one place call() builds it)."""
        return [*self.ssh, "bash -c " + shlex.quote(script)]

    def call(self, *args: str, input: bytes | str | None = None, progress=None, silent: int = 180) -> dict:
        """Run an `annex_vm.py` command that streams JSON lines (deploy, rollback; D101) and return its final
        {"ok": true, ...} object. Progress and note lines go to `progress` as they arrive; heartbeats only prove the
        connection is alive. `silent` is the longest gap between lines (the VM sends a heartbeat every 30 s), not a
        limit on the whole run, so a long build isn't cut. {"ok": false} raises AnnexError with its error. A
        connection that drops, goes silent or is interrupted (Ctrl-C) after the run started raises the detach message:
        the run carries on on the VM."""
        cmd = "python3 " + shlex.quote(f"{self.data}/platform/vm/annex_vm.py") + " " + " ".join(map(shlex.quote, args))
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
        return (f"The connection to the VM dropped at the {step} step ({why}); the {kind} continues on the VM. "
                f"`annex status {app}` shows its step and `annex releases {app}` its outcome")

    @cached_property
    def settings(self) -> dict[str, str]:
        """The deployed platform's settings (.env written by scripts/render.py)."""
        r = self.run(f"cat {self.data}/platform/.env", check=False)
        if r.returncode != 0:
            raise AnnexError(f"No Annex platform found on {self.host} ({self.data}/platform/.env). "
                             "Run scripts/bootstrap.sh and scripts/push.sh first.")
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
