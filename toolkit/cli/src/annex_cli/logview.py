"""How stored log lines are shown by `annex logs` (pure, so it's unit-tested)."""
from __future__ import annotations

import json
import re

PLAIN = re.compile(r"^[\w./:@+-]+$")


def value(v) -> str:
    """A field value for k=v: bare when it's a simple word or number, JSON otherwise (spaces and quotes stay readable)."""
    if isinstance(v, str):
        return v if PLAIN.match(v) else json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, default=str)


def render(row: dict) -> str:
    """`2026-10-16T10:50:00.952Z r44 info  job started  month=latest`. A structured line shows its level and fields;
    any other line is shown as it was written. Lines of a job or of the gateway are tagged."""
    head = [row["ts"][:23] + "Z"]
    if row.get("release") is not None:
        head.append(f"r{row['release']}")
    c = row.get("container") or ""
    if c == "gateway" or c.startswith("job:"):
        head.append(f"[{c}]")
    if row.get("level"):
        head.append(f"{row['level']:5}")
    text = row["msg"]
    if row.get("request_id"):
        text += f"  request_id={row['request_id']}"
    fields = row.get("fields") or {}
    if fields:
        text += "  " + " ".join(f"{k}={value(v)}" for k, v in fields.items())
    return " ".join(head) + " " + text


def render_all(rows: list[dict]) -> str:
    return "".join(render(r) + "\n" for r in rows)


def release_number(text: str | None) -> int | None:
    """r4 or 4 -> 4; None -> None."""
    if text is None:
        return None
    m = re.fullmatch(r"r?(\d+)", text.strip())
    if not m:
        raise ValueError(f"--release {text!r} is not a release. Use e.g. r4 (see: annex releases <app>)")
    return int(m[1])
