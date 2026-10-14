"""Talking to the Annex VM over SSH (the only transport, see decision D1)."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from functools import cached_property
from pathlib import Path

REPO = Path(os.environ.get("ANNEX_REPO", Path(__file__).resolve().parents[3]))


class AnnexError(Exception):
    """An error with a message meant for the user or the agent."""


class Remote:
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
        r = subprocess.run([*self.ssh, "bash -c " + shlex.quote(script)], input=data,
                           capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AnnexError(self._msg(r))
        return r

    def out(self, script: str, **kw) -> str:
        return self.run(script, **kw).stdout.decode(errors="replace")

    def stream(self, script: str) -> int:
        """Run a script with output going straight to the terminal (logs -f)."""
        return subprocess.run([*self.ssh, "-t", "bash -c " + shlex.quote(script)]).returncode

    def vm(self, *args: str, input: str | None = None) -> dict:
        """Call vm/annex_vm.py on the VM; it always answers with one JSON object."""
        cmd = "python3 " + shlex.quote(f"{self.data}/platform/vm/annex_vm.py") + " " + " ".join(map(shlex.quote, args))
        r = self.run(cmd, input=input, check=False)
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
