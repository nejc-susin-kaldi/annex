"""The weekly digest: which tools are used, which are rising, which are dormant. Never logins.

`render` and `audience` are pure; `collect`/`send` read the database and send email. It covers the last
complete ISO week (a Monday morning digest summarises the week that just ended), with the 4-week trend and the
last 7 days alongside.
"""
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db, directory, docker_api, email, usage
from .errors import OpsError


def trend_numbers(row: dict) -> str:
    """The last 4 complete weeks' users, oldest first: "3 → 4 → 4 → 6" ("-" where the app didn't exist yet)."""
    return " → ".join("-" if row.get(f"week{i}") is None else str(row[f"week{i}"]) for i in range(1, 5))


def change(row: dict) -> str:
    w3, w4 = row.get("week3"), row.get("week4")
    if w4 is None:
        return "new"
    if w3 is None:
        return "first week"
    d = w4 - w3
    return f"+{d}" if d > 0 else str(d) if d < 0 else "no change"


def app_line(row: dict) -> str:
    """One line per app (monospace in the email)."""
    users = row.get("week4")
    p95 = row.get("p95_week")
    rate = row.get("error_rate_week", 0)
    return (f"{row['app']:<22} {users if users is not None else 0:>3} users ({change(row)}), "
            f"{row.get('requests_week', 0):>5} requests, {rate * 100:.1f}% errors, "
            f"p95 {f'{p95} ms' if p95 is not None else 'n/a'}, trend {trend_numbers(row)}, {row['users_7d']} in the last 7 days")


def sections(rows: list[dict], now: datetime, s: usage.UsageSettings, dormant_days: int | None = None,
             min_age_days: int | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """(all apps, graduation candidates, dormant apps) for these rows."""
    grad, dorm = [], []
    for r in rows:
        sig = usage.signal_for(r, now, s, dormant_days, min_age_days)
        (grad if sig == "graduation" else dorm if sig == "dormant" else []).append(r)
    return rows, grad, dorm


def render(rows: list[dict], now: datetime, s: usage.UsageSettings, dashboard_url: str = "",
           dormant_days: int | None = None, min_age_days: int | None = None) -> tuple[str, str, str]:
    """(subject, body, details) for one recipient's apps."""
    days = dormant_days or s.dormant_days
    all_rows, grad, dorm = sections(rows, now, s, dormant_days, min_age_days)
    body = ["How the tools on Annex were used last week (signed-in page requests; health checks and static files "
            "aren't counted, and logins aren't shown).", ""]
    body += ["Graduation candidates (rising use, "
             f"at least {s.graduation_min_users} users last week): " + (", ".join(r["app"] for r in grad) or "none") + ".",
             "Worth turning into a proper project." if grad else ""]
    body += ["", f"Dormant (no users in {days} days): " + (", ".join(r["app"] for r in dorm) or "none") + ".",
             "Consider retiring them: annex remove <app>." if dorm else ""]
    if dashboard_url:
        body += ["", f"The dashboard: {dashboard_url}"]
    details = "\n".join(app_line(r) for r in all_rows) or "(no apps)"
    return f"[Annex] Weekly usage: {len(all_rows)} tool(s), {len(grad)} to graduate, {len(dorm)} dormant", \
        "\n".join(x for x in body).strip(), details


def audience(rows: list[dict], owners: dict[str, str | None], users: dict[str, dict]) -> dict[str, list[dict]]:
    """email address -> the rows that person gets: admins get every app, each owner their own."""
    out: dict[str, list[dict]] = {}
    for login, u in users.items():
        if "admins" in (u.get("groups") or []) and u.get("email"):
            out[u["email"]] = list(rows)
    for r in rows:
        owner = owners.get(r["app"])
        addr = (users.get(owner) or {}).get("email") if owner else None
        if addr and addr not in out:
            out.setdefault(addr, [])
        if addr and all(x["app"] != r["app"] for x in out[addr]):
            out[addr].append(r)
    return {a: rs for a, rs in out.items() if rs}


def due(now: datetime, weekday: int, hour: int, sent_weeks: set) -> bool:
    """Is it time for this week's digest? From the scheduled moment (weekday, hour) for 24 hours, if the week
    isn't already recorded: so a restart during that day still sends it once, and a new install on a Wednesday
    doesn't send one at once."""
    week_start = (now - timedelta(days=now.weekday())).date()
    scheduled = datetime.combine(week_start + timedelta(days=weekday), datetime.min.time(), tzinfo=now.tzinfo) + timedelta(hours=hour)
    return timedelta(0) <= now - scheduled < timedelta(hours=24) and week_start not in sent_weeks


# --- data ---------------------------------------------------------------------------------------------
def collect() -> list[dict]:
    """usage_apps rows plus last week's requests, error rate and worst daily p95 per app."""
    usage.rollup()
    rows = usage.usage_rows()
    with db.connect() as conn:
        week = conn.execute("SELECT app, sum(requests)::int, sum(errors)::int, max(p95_ms) FROM annex_ops.usage_daily "
                            "WHERE day >= date_trunc('week', now() AT TIME ZONE %(tz)s)::date - 7 "
                            "AND day < date_trunc('week', now() AT TIME ZONE %(tz)s)::date GROUP BY app", {"tz": config.USAGE_TZ}).fetchall()
    stats = {a: (req, err, p95) for a, req, err, p95 in week}
    for r in rows:
        req, err, p95 = stats.get(r["app"], (0, 0, None))
        r["requests_week"], r["p95_week"] = req, p95
        r["error_rate_week"] = (err / req) if req else 0
    return rows


def owners() -> dict[str, str | None]:
    out = {}
    try:
        for c in docker_api.containers():
            labels = c.get("Labels") or {}
            if labels.get("com.kaldi.annex.app"):
                out[labels["com.kaldi.annex.app"]] = labels.get("com.kaldi.annex.owner")
    except (docker_api.DockerError, OSError):
        pass
    return out


def plan(now: datetime, to: str | None = None, dormant_days: int | None = None,
         min_age_days: int | None = None) -> list[dict]:
    """The emails to send: {to, subject, body, details, apps}. `to` limits it to one login (preview and tests)."""
    s = config.usage_settings()
    users = directory.users()
    rows = collect()
    groups = audience(rows, owners(), users)
    out = []
    for addr, mine in sorted(groups.items()):
        if to and (users.get(to) or {}).get("email") != addr:
            continue
        subject, body, details = render(mine, now, s, config.METABASE_URL and f"{config.METABASE_URL}/search?q=Annex%20usage",
                                        dormant_days, min_age_days)
        out.append({"to": addr, "subject": subject, "body": body, "details": details, "apps": [r["app"] for r in mine]})
    return out


def send(now: datetime, to: str | None = None, dormant_days: int | None = None, min_age_days: int | None = None) -> list[dict]:
    sent = plan(now, to, dormant_days, min_age_days)
    for m in sent:
        try:
            email.send_email("_platform", [m["to"]], m["subject"], template="notice",
                             data={"title": "Weekly usage", "body": m["body"], "details": m["details"],
                                   **({"link": config.METABASE_URL + "/search?q=Annex%20usage", "link_text": "Open the usage dashboard"}
                                      if config.METABASE_URL else {})})
            m["result"] = "sent"
        except OpsError as e:
            m["result"] = f"failed: {e.message}"
    return sent


# --- the thread -----------------------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"digest: {msg}", file=sys.stderr, flush=True)


class Worker(threading.Thread):
    """Once a minute: is this week's digest due? The week is recorded before sending, so it goes out once."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="digest")

    def run(self) -> None:
        while True:
            time.sleep(60)
            try:
                s = config.usage_settings()
                now = datetime.now(ZoneInfo(config.USAGE_TZ))
                with db.connect() as conn:
                    sent_weeks = {r[0] for r in conn.execute("SELECT week FROM annex_ops.digests").fetchall()}
                if not due(now, s.digest_weekday, s.digest_hour, sent_weeks):
                    continue
                week_start = (now - timedelta(days=now.weekday())).date()
                with db.connect() as conn:
                    got = conn.execute("INSERT INTO annex_ops.digests (week, sent_at) VALUES (%s, now()) ON CONFLICT DO NOTHING "
                                       "RETURNING week", (week_start,)).fetchone()
                if got:
                    results = send(now)
                    log(f"sent {len(results)} digest(s) for the week of {week_start}")
            except Exception:  # noqa: BLE001: the digest thread must never die
                log("error:\n" + traceback.format_exc())
