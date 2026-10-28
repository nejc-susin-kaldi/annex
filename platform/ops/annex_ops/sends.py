"""The send log (never bodies) and the per-app rate limit."""
from datetime import datetime, timedelta, timezone

from . import db
from .errors import OpsError

WINDOW = timedelta(hours=1)


def log_send(app: str, channel: str, recipients: list[str], subject: str | None, status: str,
             error: str | None = None) -> int:
    with db.connect() as conn:
        return conn.execute(
            "INSERT INTO annex_ops.sends (app, channel, recipients, subject, status, error) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (app, channel, recipients, subject and subject[:200], status, error and error[:500])).fetchone()[0]


def recent_sent(app: str, channel: str) -> list[datetime]:
    """Timestamps of this app's successful sends in the last hour, oldest first."""
    with db.connect() as conn:
        rows = conn.execute("SELECT ts FROM annex_ops.sends WHERE app = %s AND channel = %s AND status = 'sent' "
                            "AND ts > now() - interval '1 hour' ORDER BY ts", (app, channel)).fetchall()
    return [r[0] for r in rows]


def seconds_until_allowed(sent: list[datetime], limit: int, now: datetime) -> int:
    """0 if one more send fits in the hourly limit, else how long until the oldest ones age out."""
    if limit > 0 and len(sent) < limit:
        return 0
    if limit <= 0:
        return int(WINDOW.total_seconds())
    frees_at = sent[len(sent) - limit] + WINDOW  # the send whose expiry opens a slot
    return max(1, int((frees_at - now).total_seconds()) + 1)


def check_rate(app: str, channel: str, limit: int, what: str, now: datetime | None = None) -> None:
    wait = seconds_until_allowed(recent_sent(app, channel), limit, now or datetime.now(timezone.utc))
    if wait:
        raise OpsError(429, f"{app} reached its limit of {limit} {what} per hour. It can send again in about "
                            f"{max(1, round(wait / 60))} minute(s). Send fewer, larger messages (one summary "
                            "instead of one per record)")


def list_sends(app: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ("WHERE app = %s", (app,)) if app else ("", ())
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id, ts, app, channel, recipients, subject, status, error "
                            f"FROM annex_ops.sends {where} ORDER BY ts DESC, id DESC LIMIT %s",
                            (*args, limit)).fetchall()
    return [{"id": r[0], "ts": r[1].isoformat(timespec="seconds"), "app": r[2], "channel": r[3],
             "recipients": list(r[4]), "subject": r[5], "status": r[6], "error": r[7]} for r in rows]


def failure_status(e: OpsError) -> str:
    """How a failed attempt is logged: 'failed' when delivery broke (502), 'unavailable' when a
    dependency isn't there (503, e.g. Slack not connected), else 'rejected' (the caller's fault)."""
    return {502: "failed", 503: "unavailable"}.get(e.status, "rejected")


def log_failure(app: str, channel: str, recipients: list[str], subject: str | None, e: OpsError) -> None:
    """Log a failed attempt. If the log itself is unavailable, the original error is what the caller gets."""
    try:
        log_send(app, channel, recipients, subject, failure_status(e), e.message)
    except OpsError:
        pass
