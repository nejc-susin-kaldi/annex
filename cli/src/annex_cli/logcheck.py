"""The logging convention's warn-only check: do the app's first log lines follow it?

Pure functions (no I/O), so they're unit-tested. `annex deploy` reads the new container's first lines after the
health gate passes and, if none follow the convention, adds a warning to the result. It never refuses a deploy.
"""
from __future__ import annotations

import json

LEVELS = ("debug", "info", "warn", "error")
MESSAGE = ("logs aren't structured: use `log` from annex_runtime.py instead of print() and plain logging, so every "
           "line is JSON with ts, level and msg (see `annex contract logging`)")


def parse_log_line(line: str) -> dict | None:
    """The line as a dict if it follows the convention (a JSON object with a valid `level` and a `msg`), else None."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("level") in LEVELS and isinstance(obj.get("msg"), str):
        return obj
    return None


def structured(lines: list[str]) -> bool | None:
    """True if at least one line follows the convention, False if there are lines but none do, None if there are
    no lines at all (a quiet app: nothing to judge, so no warning)."""
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return None
    return any(parse_log_line(ln) for ln in lines)


def warning(lines: list[str]) -> str | None:
    """The deploy warning for these first log lines, or None."""
    return MESSAGE if structured(lines) is False else None


def structured_rows(rows: list[dict]) -> bool | None:
    """The same question for lines annex-ops has stored (their `structured` flag is the convention's check, made
    when the line arrived): True if one follows it, False if there are lines and none do, None if there are none."""
    if not rows:
        return None
    return any(r.get("structured") for r in rows)


def rows_warning(rows: list[dict]) -> str | None:
    """The status warning for a job-only app's first stored lines, or None."""
    return MESSAGE if structured_rows(rows) is False else None
