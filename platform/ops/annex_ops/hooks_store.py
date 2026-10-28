"""annex_ops.hook_events: store, list, replay and clean up incoming webhook events."""
from datetime import datetime

from psycopg.types.json import Jsonb

from . import db
from .errors import OpsError

META = "id, received_at, app, hook, status, attempts, delivered_at, last_error, replay_of"


def _meta(r) -> dict:
    iso = lambda t: t.isoformat(timespec="seconds") if t else None  # noqa: E731
    return {"id": r[0], "received_at": iso(r[1]), "app": r[2], "hook": r[3], "status": r[4], "attempts": r[5],
            "delivered_at": iso(r[6]), "last_error": r[7], "replay_of": r[8]}


def insert_pending(app: str, hook: str, content_type: str | None, headers: dict, body: bytes) -> int:
    with db.connect() as conn:
        return conn.execute(
            "INSERT INTO annex_ops.hook_events (app, hook, status, content_type, headers, body, next_attempt_at) "
            "VALUES (%s, %s, 'pending', %s, %s, %s, now()) RETURNING id",
            (app, hook, content_type, Jsonb(headers), body)).fetchone()[0]


def insert_rejected(app: str, hook: str, reason: str) -> None:
    """A request that failed its signature check: logged with the reason, never its body."""
    with db.connect() as conn:
        conn.execute("INSERT INTO annex_ops.hook_events (app, hook, status, last_error) VALUES (%s, %s, 'rejected', %s)",
                     (app, hook, reason[:200]))


def pending() -> list[dict]:
    """Pending events, oldest first, without bodies (for choosing what is due and what is failing)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, app, hook, extract(epoch FROM received_at), attempts, "
                            "extract(epoch FROM next_attempt_at) FROM annex_ops.hook_events WHERE status = 'pending' "
                            "ORDER BY id LIMIT 2000").fetchall()
    return [{"id": r[0], "app": r[1], "hook": r[2], "received_at": float(r[3]), "attempts": r[4],
             "next_attempt_at": None if r[5] is None else float(r[5])} for r in rows]


def full_event(event_id: int) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT id, app, hook, content_type, headers, body, attempts, replay_of "
                         "FROM annex_ops.hook_events WHERE id = %s AND status = 'pending'", (event_id,)).fetchone()
    return None if not r else {"id": r[0], "app": r[1], "hook": r[2], "content_type": r[3], "headers": r[4],
                               "body": bytes(r[5] or b""), "attempts": r[6], "replay_of": r[7]}


def mark_delivered(event_id: int, attempts: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE annex_ops.hook_events SET status = 'delivered', attempts = %s, delivered_at = now(), "
                     "next_attempt_at = NULL, last_error = NULL WHERE id = %s", (attempts, event_id))


def mark_retry(event_id: int, attempts: int, error: str, delay_s: float) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE annex_ops.hook_events SET attempts = %s, last_error = %s, "
                     "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                     (attempts, error[:300], delay_s, event_id))


def mark_failed(event_id: int, reason: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE annex_ops.hook_events SET status = 'failed', next_attempt_at = NULL, last_error = %s "
                     "WHERE id = %s AND status = 'pending'", (reason[:300], event_id))


def housekeeping(max_age_s: float, known: dict[str, set[str]]) -> dict:
    """Fail pending events that are too old or whose app no longer declares the hook; delete events older than 7 days."""
    out = {"expired": 0, "orphaned": 0, "deleted": 0}
    with db.connect() as conn:
        out["expired"] = conn.execute(
            "UPDATE annex_ops.hook_events SET status = 'failed', next_attempt_at = NULL, "
            "last_error = coalesce(last_error || ' | ', '') || 'gave up: the event is older than the retry period' "
            "WHERE status = 'pending' AND received_at < now() - make_interval(secs => %s)", (max_age_s,)).rowcount
        for app, hooks in known.items():  # apps with a hooks file: hooks it no longer declares
            out["orphaned"] += conn.execute(
                "UPDATE annex_ops.hook_events SET status = 'failed', next_attempt_at = NULL, "
                "last_error = 'the app no longer declares this webhook' WHERE status = 'pending' AND app = %s "
                "AND hook <> ALL(%s)", (app, list(hooks))).rowcount
        out["orphaned"] += conn.execute(
            "UPDATE annex_ops.hook_events SET status = 'failed', next_attempt_at = NULL, last_error = 'app removed' "
            "WHERE status = 'pending' AND app <> ALL(%s)", (list(known),)).rowcount
    return out


def purge(retention_days: int = 7) -> int:
    with db.connect() as conn:
        return conn.execute("DELETE FROM annex_ops.hook_events WHERE received_at < now() - make_interval(days => %s)",
                            (retention_days,)).rowcount


# --- for ctl ----------------------------------------------------------------------------------------
def list_events(app: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    where = ["TRUE"] + (["app = %s"] if app else []) + (["status = %s"] if status else [])
    args = [x for x in (app, status) if x]
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {META} FROM annex_ops.hook_events WHERE {' AND '.join(where)} "
                            "ORDER BY id DESC LIMIT %s", (*args, limit)).fetchall()
    return [_meta(r) for r in rows]


def show_event(event_id: int) -> dict:
    """Headers and the body (truncated to 64 KB): the only place a body is ever shown. Human-only (annex hooks show)."""
    with db.connect() as conn:
        r = conn.execute(f"SELECT {META}, content_type, headers, body FROM annex_ops.hook_events WHERE id = %s",
                         (event_id,)).fetchone()
    if not r:
        raise OpsError(404, f"no webhook event {event_id}. List them with: annex hooks <app>")
    body = bytes(r[11] or b"")
    return {**_meta(r[:9]), "content_type": r[9], "headers": r[10], "body_bytes": len(body),
            "body": body[:65536].decode(errors="replace"), "truncated": len(body) > 65536}


def replay(app: str, event_id: int | None = None, failed: bool = False, since_s: float | None = None) -> list[int]:
    """Copy events as new pending ones (replay_of points at the original, so the app sees the same event id)."""
    if sum(x is not None and x is not False for x in (event_id, failed or None, since_s)) != 1:
        raise OpsError(400, "choose what to replay: --id N, --failed, or --since 1h")
    if event_id is not None:
        where, args = "id = %s AND status <> 'rejected'", (event_id,)
    elif failed:
        where, args = "status = 'failed'", ()
    else:
        where, args = "status <> 'rejected' AND received_at > now() - make_interval(secs => %s)", (since_s,)
    with db.connect() as conn:
        rows = conn.execute("INSERT INTO annex_ops.hook_events (app, hook, status, content_type, headers, body, next_attempt_at, replay_of) "
                            "SELECT app, hook, 'pending', content_type, headers, body, now(), coalesce(replay_of, id) "
                            f"FROM annex_ops.hook_events WHERE app = %s AND body IS NOT NULL AND {where} ORDER BY id "
                            "RETURNING id", (app, *args)).fetchall()
    if not rows:
        raise OpsError(404, f"nothing to replay for {app} with those options (rejected requests have no body to replay; "
                            "events older than 7 days are gone). List events with: annex hooks " + app)
    return [r[0] for r in rows]
