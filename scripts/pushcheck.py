#!/usr/bin/env python3
"""The push stamp: what scripts/push.sh compares before it overwrites the platform on the VM.

`.build/<env>/platform/VERSION` (written by render.py; <env> is vm or local, D99) holds `commit=`, `dirty=` and `env_sha256=`.
push.sh passes the VM's copy of that file (or `-`, on a first push) to `check`, which refuses
when the VM's commit is not an ancestor of the local HEAD, so an older checkout can't quietly
roll the platform back. The decision is a pure function (`decide`); the git calls are in
`main`. Stdlib only.

  pushcheck.py check <local VERSION> <VM VERSION file, or - for none> [--force]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            out[k] = v
    return out


def _desc(stamp: dict[str, str]) -> str:
    return f"{stamp.get('commit', '?')[:12]}{' (dirty)' if stamp.get('dirty') == 'true' else ''}"


def decide(vm: dict[str, str] | None, local: dict[str, str], known: bool, ancestor: bool, force: bool) -> tuple[bool, str]:
    """(proceed, message). `known`: the VM's commit exists in this checkout; `ancestor`: it is an ancestor of the local one."""
    if not vm or not vm.get("commit"):
        return True, "the VM has no platform/VERSION yet: first push, going ahead."
    desc = f"VM {_desc(vm)}, local {_desc(local)}"
    if force:
        return True, f"{desc}: --force, not checking."
    if not known:
        return False, (f"{desc}: the VM's commit is not in this checkout. Run `git fetch` (it may be on another "
                       "branch or machine), or push with --force if you mean to replace it.")
    if not ancestor:
        return False, (f"{desc}: the VM's commit is not an ancestor of this checkout, so this push would roll "
                       "back or drop changes someone pushed. Merge or rebase onto it, or push with --force.")
    return True, f"{desc}: ok."


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True).returncode == 0


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[1] != "check":
        print(__doc__, file=sys.stderr)
        return 2
    local = parse(Path(argv[2]).read_text())
    vm = None if argv[3] == "-" else (parse(Path(argv[3]).read_text()) or None)
    vc = (vm or {}).get("commit", "")
    known = bool(vc) and git_ok("cat-file", "-e", f"{vc}^{{commit}}")
    ancestor = known and git_ok("merge-base", "--is-ancestor", vc, local.get("commit") or "HEAD")
    ok, msg = decide(vm, local, known, ancestor, "--force" in argv[4:])
    if ok:
        print(f"push: {msg}")
    else:
        print(f"push: refused: {msg}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
