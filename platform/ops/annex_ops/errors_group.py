"""Error groups (M2.3): what counts as "the same error", pure functions only.

Input is annex_ops.logs rows (dicts, oldest first): structured error lines, and the lines of a Python traceback
that an app printed (stored one row per line, D63). Output is `Event`s: one per error, each with a fingerprint.
Two errors that differ only in numbers, ids, quoted values or file paths have the same fingerprint, so "cannot load
client 123" and "cannot load client 456" are one group; a traceback is identified by its exception type and the
last frame's file and function, whatever the message says.

Everything that decides something is here and unit-tested; errors_run.py does the database and the sending.
"""
import hashlib
import re
from dataclasses import dataclass

TRACE_GAP_S = 3.0   # a traceback's lines follow each other within this many seconds

# --- normalising a message ------------------------------------------------------------------
_NORMALISERS = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"(?<![\w.<])(?:/[\w.@+-]+){2,}"), "<path>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
]
FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')
JS_FRAME = re.compile(r"^\s*at (?P<func>[^\s(]+) \((?P<file>[^:)]+)[:\d]*\)")
EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s*(?P<text>.*))?$")
TRACE_START = "Traceback (most recent call last)"
CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


def normalise(msg: str) -> str:
    """The message with numbers, ids, quoted values and paths replaced by placeholders (so a group isn't one per value)."""
    for pattern, placeholder in _NORMALISERS:
        msg = pattern.sub(placeholder, msg)
    return re.sub(r"\s+", " ", msg).strip()[:300]


def exception_type(text: str | None) -> str | None:
    """`ValueError: bad value` -> `ValueError` (a dotted class name stays whole); None if it isn't an exception line."""
    m = EXC_LINE.match((text or "").strip())
    return m["type"] if m and (m["type"].endswith(("Error", "Exception", "Exit", "Warning", "Interrupt")) or "." in m["type"]
                               or m["type"][0].isupper() and m["text"] is not None) else None


LIBRARY = ("site-packages", "dist-packages", "/usr/lib/python", "/usr/local/lib/python", "node_modules", "node:internal", "<frozen")


def own_code(file: str) -> bool:
    return not any(marker in file for marker in LIBRARY)


def last_frame(stack: str | list[str] | None) -> tuple[str, str] | None:
    """(file, function) of the frame where the app's own code failed: the innermost frame that isn't in a library
    or the standard library (a framework's wrapper functions would make every error in an app look the same),
    else the innermost frame at all. For Python stacks and Node stacks; None if there is no frame."""
    lines = stack.splitlines() if isinstance(stack, str) else (stack or [])
    py = [(m["file"], m["func"]) for ln in lines if (m := FRAME.match(ln))]
    js = [(m["file"], m["func"]) for ln in lines if (m := JS_FRAME.match(ln))]
    if py:  # innermost last
        return next((f for f in reversed(py) if own_code(f[0])), py[-1])
    if js:  # innermost first
        return next((f for f in js if own_code(f[0])), js[0])
    return None


def fingerprint(app: str, key: str) -> str:
    return hashlib.sha1(f"{app}\0{key}".encode()).hexdigest()[:16]


@dataclass
class Event:
    app: str
    fingerprint: str
    kind: str                 # server | browser | job
    title: str                # the normalised summary a group is shown by
    sample: str               # the first real message, for the alert
    ts: float                 # epoch seconds
    h: str                    # the hash of the line that started it: an event is stored once, however often it is read
    release: int | None = None
    request_id: str | None = None
    user: str | None = None
    container: str = ""


def _kind(row: dict, fields: dict | None) -> str:
    if fields and fields.get("source") == "browser":
        return "browser"
    return "job" if str(row.get("container", "")).startswith("job:") else "server"


def _structured_event(row: dict) -> Event:
    fields = row.get("fields") or {}
    exc = fields.get("exc") if isinstance(fields.get("exc"), str) else None
    etype = exception_type(exc)
    frame = last_frame(fields.get("stack") if isinstance(fields.get("stack"), str) else None)
    if etype and frame:
        key, title = f"{etype}|{frame[0]}|{frame[1]}", f"{etype} in {frame[1]}"
    elif etype:
        key, title = f"{etype}|{normalise(row['msg'])}", f"{etype}: {normalise(row['msg'])}"
    else:
        key = title = normalise(row["msg"])
    sample = row["msg"] + (f" ({exc})" if exc else "")
    return Event(row["app"], fingerprint(row["app"], key), _kind(row, fields), title[:200], sample[:500], row["ts"].timestamp()
                 if hasattr(row["ts"], "timestamp") else float(row["ts"]), row["h"], row.get("release"), row.get("request_id"),
                 fields.get("user") if isinstance(fields.get("user"), str) else None, row.get("container", ""))


def _trace_event(first: dict, frames: list[tuple[str, str]], exc_line: str | None) -> Event:
    etype = exception_type(exc_line)
    frame = frames[-1] if frames else None
    if etype and frame:
        key, title = f"{etype}|{frame[0]}|{frame[1]}", f"{etype} in {frame[1]}"
    elif etype:
        key, title = etype, etype
    elif frame:
        key, title = f"trace|{frame[0]}|{frame[1]}", f"traceback in {frame[1]}"
    else:
        key = title = "traceback"
    ts = first["ts"].timestamp() if hasattr(first["ts"], "timestamp") else float(first["ts"])
    return Event(first["app"], fingerprint(first["app"], key), _kind(first, None), title[:200], (exc_line or "traceback")[:500],
                 ts, first["h"], first.get("release"), first.get("request_id"), None, first.get("container", ""))


def _seconds(row: dict) -> float:
    ts = row["ts"]
    return ts.timestamp() if hasattr(ts, "timestamp") else float(ts)


def assemble(rows: list[dict], force_before: float | None = None, window_end: float | None = None) -> tuple[list[Event], dict[str, float]]:
    """Events from rows of one or more apps and containers, oldest first within each container.

    Returns (events, open_traces): `open_traces` maps a container to when a traceback that has not ended (no
    exception line yet, or one that might be followed by a chained trace) began. The caller doesn't move past
    that point, so the next read starts at its first line. A trace that began before `force_before` (epoch seconds)
    is finished as it is instead: a program that died in the middle of printing one must not hold the reader back.
    `window_end` is where the rows stop (default: the newest row): a trace whose exception line is at least
    TRACE_GAP_S before it can't be continued by a chained trace any more, so it is complete."""
    events: list[Event] = []
    open_traces: dict[str, float] = {}
    by_container: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_container.setdefault((r["app"], r["container"]), []).append(r)
    for (_, container), seq in by_container.items():
        seq.sort(key=lambda r: (r["ts"], r.get("ns", 0)))
        trace: dict | None = None  # {first, frames, exc, last}
        for row in seq:
            msg, now = row["msg"], _seconds(row)
            structured = row.get("fields") is not None
            if trace is not None:
                near = now - trace["last"] < TRACE_GAP_S and not structured
                if near and msg[:1] in (" ", "\t"):
                    if (m := FRAME.match(msg)):
                        trace["frames"].append((m["file"], m["func"]))
                    trace["last"] = now
                    continue
                if near and msg.startswith(CHAINED):
                    trace["exc"], trace["chain"] = None, True  # the real exception comes after the chained trace
                    trace["last"] = now
                    continue
                if near and trace["chain"] and msg.startswith(TRACE_START):
                    trace["frames"], trace["chain"], trace["last"] = [], False, now
                    continue
                if near and trace["exc"] is None and exception_type(msg):
                    trace["exc"], trace["last"] = msg, now
                    continue
                events.append(_trace_event(trace["first"], trace["frames"], trace["exc"]))
                trace = None
            if not structured and msg.startswith(TRACE_START):
                trace = {"first": row, "frames": [], "exc": None, "last": now, "chain": False}
            elif row.get("level") == "error":
                events.append(_structured_event(row) if structured else _plain_event(row))
        if trace is not None:
            if (trace["exc"] is not None and (window_end if window_end is not None else _seconds(seq[-1])) - trace["last"] >= TRACE_GAP_S) or (
                    force_before is not None and _seconds(trace["first"]) < force_before):
                events.append(_trace_event(trace["first"], trace["frames"], trace["exc"]))
            else:
                open_traces[container] = _seconds(trace["first"])
    events.sort(key=lambda e: e.ts)
    return events, open_traces


def _plain_event(row: dict) -> Event:
    title = normalise(row["msg"])
    return Event(row["app"], fingerprint(row["app"], title), _kind(row, None), title[:200], row["msg"][:500], _seconds(row), row["h"],
                 row.get("release"), row.get("request_id"), None, row.get("container", ""))


# --- when a group is worth an email -------------------------------------------------------------------
def should_alert(prev: dict | None, ts: float, release: int | None, quiet_s: float) -> bool:
    """`prev` is the group as it was before this event (None: a new group). Alert for a new group, for one that was
    quiet (not seen for `quiet_s`), and for one that comes back in a newer release than it was last seen in
    (so it was gone from the releases in between, or at least it is a new deploy's problem). Not for more of
    the same."""
    if prev is None:
        return True
    if ts - prev["last_seen"] > quiet_s:
        return True
    return release is not None and prev.get("last_release") is not None and release > prev["last_release"]


def group_state(last_seen: float, last_release: int | None, current_release: int | None, now: float, quiet_s: float) -> str:
    """`active`, or `quiet` when it hasn't been seen for `quiet_s` or the live release is newer than the last one
    that had it (a fix went out and it hasn't come back)."""
    if now - last_seen > quiet_s:
        return "quiet"
    if current_release is not None and last_release is not None and current_release > last_release:
        return "quiet"
    return "active"


def plan_alerts(due: list[dict], sent_last_hour: int, limit: int) -> tuple[list[dict], list[dict]]:
    """(groups to send one email each, groups to summarise in one email) for the app's due groups, oldest first.
    At most `limit` emails an hour: if the due groups don't fit, the last slot is a summary of all the rest."""
    room = max(0, limit - sent_last_hour)
    if room == 0 or not due:
        return [], []
    if len(due) <= room:
        return due, []
    return due[: room - 1], due[room - 1:]


def alert_subject(app: str, group: dict, again: bool) -> str:
    title = group["title"] if len(group["title"]) <= 90 else group["title"][:87] + "..."
    return f"[Annex] {'error is back in' if again else 'new error in'} {app}: {title}"


def summary_subject(app: str, n: int) -> str:
    return f"[Annex] {app}: {n} more kind{'s' if n != 1 else ''} of error"


def alert_body(app: str, group: dict) -> str:
    where = {"browser": "in the browser", "job": "in a scheduled job"}.get(group["kind"], "in the app")
    rel = f" in r{group['last_release']}" if group.get("last_release") is not None else ""
    lines = [f"A kind of error was seen {where}{rel}: {group['sample_msg']}", "",
             f"First seen {group['first_seen']:%Y-%m-%d %H:%M}, seen {group['count']} time{'s' if group['count'] != 1 else ''} so far."]
    if group.get("last_user"):
        lines.append(f"Last affected user: {group['last_user']}")
    lines += ["", "Find it:", f"  annex logs {app} --level error --since 1h"]
    if group.get("sample_request_id"):
        lines.append(f"  annex logs {app} --request {group['sample_request_id']}   (every line of one request)")
    lines += ["", "Only the first of a kind of error is emailed; more of the same are counted. You hear again if it comes "
              "back after a quiet day or in a newer release."]
    return "\n".join(lines)


def summary_body(app: str, groups: list[dict]) -> str:
    lines = [f"{len(groups)} more kinds of error were seen in {app} (the hourly limit of emails was reached):", ""]
    lines += [f"- {g['title']} ({g['count']}x, first {g['first_seen']:%H:%M})" for g in groups[:20]]
    if len(groups) > 20:
        lines.append(f"- ... and {len(groups) - 20} more")
    lines += ["", f"See them all: annex errors {app}"]
    return "\n".join(lines)
