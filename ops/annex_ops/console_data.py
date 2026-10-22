"""What the console's pages show, read with the console's read-only database role (M2.4, reshaped in M2.7-M2.9).

Almost everything is a function that already exists for `annex status`, `annex usage`, `annex errors` and `annex logs`
(usage.usage_app, incidents.list_incidents, errors_run.list_groups, logstore.query_logs, audit.timeline), so a number on a
page and the same number from the CLI come from the same query. What's new here: the apps and their owners (from the
release records, `annex_ops.releases`, read for an app's latest life: removing an app restarts numbering, D65), the
health view (the health checker's results, `annex_ops.health_checks`), the warnings, the host samples and the
"needs attention" lists. The ranking and the day-series arithmetic are pure functions with unit tests.
"""
import calendar
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from . import audit, config, console_view as v, db, errors_run, health, host, incidents, logstore, threads, usage
from .errors import OpsError

NO_RECORD = "no release record"
SPARK_DAYS = 14
BAD, WARN = 0, 1   # the severity of an attention item


def _life_start(conn, app: str):
    """When the app's latest life began: the newest release 1 (numbering restarts after `annex remove`, D65)."""
    return conn.execute("SELECT max(deployed_at) FROM annex_ops.releases WHERE app = %s AND release = 1", (app,)).fetchone()[0]


def releases(app: str) -> list[dict]:
    """The app's releases of its latest life, newest first, the live one marked."""
    with db.connect() as conn:
        since = _life_start(conn, app)
        rows = conn.execute("SELECT release, result, deployed_at, deployed_by, via, git, note, manifest FROM annex_ops.releases "
                            "WHERE app = %s AND (%s::timestamptz IS NULL OR deployed_at >= %s) ORDER BY deployed_at DESC, release DESC",
                            (app, since, since)).fetchall()
    live = next((r[0] for r in rows if r[1] == "live"), None)
    return [{"release": r[0], "result": r[1], "live": r[0] == live and r[1] == "live", "deployed_at": v.when(r[2]), "deployed_at_raw": r[2],
             "deployed_by": r[3], "via": r[4], "git": ((r[5] or {}).get("sha", "") + ("+dirty" if (r[5] or {}).get("dirty") else "")) or None,
             "note": r[6], "manifest": r[7]} for r in rows]


def apps() -> list[dict]:
    """Apps that exist now: owner, live release and when it was deployed. Apps deployed before release records have
    no owner here (only admins see them)."""
    with db.connect() as conn:
        seen = [r[0] for r in conn.execute("SELECT app FROM annex_ops.app_seen WHERE removed_at IS NULL ORDER BY app").fetchall()]
    out = []
    for app in seen:
        rels = releases(app)
        live = next((r for r in rels if r["live"]), None)
        m = (live or {}).get("manifest") or {}
        out.append({"app": app, "owner": m.get("owner"), "live": live["release"] if live else None, "manifest": m or None,
                    "deployed_at": live["deployed_at"] if live else "", "deployed_at_raw": live["deployed_at_raw"] if live else None,
                    "deployed_by": live["deployed_by"] if live else "", "releases": rels})
    return out


# --- health ---------------------------------------------------------------------------------------------------
def health_snapshot() -> tuple[dict[str, dict], float | None]:
    """(the checker's newest result per check keyed `app:<name>` and `platform:front-door`: {"up": bool, "detail": ...}, when the
    newest result of any check was written). A result older than the checker's freshness limit is left out (it isn't running,
    or the app is new); ({}, None) if the table can't be read."""
    try:
        rows = health.latest()
    except OpsError:
        return {}, None
    now, out = time.time(), {}
    for key, row in rows.items():
        st = health.state_of(row, now, config.tuning().check_interval_s)
        if st["state"] != "unknown":
            out[key] = {"up": st["state"] == "up", "detail": st.get("detail", "")}
    return out, max((r["ts"] for r in rows.values()), default=None)


def health_states() -> dict[str, dict]:
    return health_snapshot()[0]


def checker_stale(newest: float | None, now: float, interval_s: float) -> bool:
    """The health checker isn't answering: nothing written in three intervals (at least `health.MIN_FRESH_S`)."""
    return newest is None or now - newest > max(health.MIN_FRESH_S, 3 * interval_s)


def app_health(app: str, manifest: dict | None, states: dict, open_incidents: list[dict]) -> tuple[str, str]:
    """(state, detail): down if the health check fails or an incident is open on the app; no check (job-only apps) is ok unless an incident is open."""
    g = states.get(f"app:{app}")
    if g is not None and not g["up"]:
        return "down", g["detail"] or "the health check fails"
    own = [i for i in open_incidents if i["subject"] == f"app:{app}"]
    if own:
        return "down", ", ".join(own[0]["signals"])
    if g is not None:
        return "up", ""
    if manifest and manifest.get("type") == "job":
        return ("down", "a job is failing") if open_incidents else ("ok", "job-only: no health check")
    return "unknown", "no check result yet" if states else "the health checker isn't answering"


def status_view() -> tuple[list[dict], list[dict]]:
    """(apps, platform): names and up/down for everyone signed in."""
    states = health_states()
    open_all = incidents.list_incidents(open_only=True, limit=200)
    out_apps = []
    for a in apps():
        state, detail = app_health(a["app"], a["manifest"], states, [i for i in open_all if i["app"] == a["app"]])
        out_apps.append({"app": a["app"], "state": state, "detail": detail})
    platform = []
    fd = states.get(health.FRONT_DOOR)
    platform.append({"name": "front door", "state": "unknown" if fd is None else "up" if fd["up"] else "down", "detail": fd["detail"] if fd and not fd["up"] else ""})
    for i in open_all:
        if i["subject"].startswith("platform:") and i["subject"] != "platform:front-door":
            platform.append({"name": i["subject"][9:], "state": "down", "detail": ", ".join(i["signals"])})
    return out_apps, platform


# --- warnings -------------------------------------------------------------------------------------------------
def structured_warning(app: str, release: int | None) -> str | None:
    """The logging convention's check on the app's first stored lines of its live release (as `annex status` does for jobs)."""
    if release is None:
        return None
    rows = logstore.query_logs(app, 50, release=release, oldest=True)
    if rows and not any(r["structured"] for r in rows):
        return "logs aren't structured: the app prints plain text; use `log` from annex_runtime.py (see the Logging section of `annex contract`)"
    return None


def warnings_for(a: dict, health: tuple[str, str], runs: list[dict]) -> list[str]:
    out = []
    m = a["manifest"] or {}
    if (w := structured_warning(a["app"], a["live"])):
        out.append(w)
    if m.get("type") in ("web", "web+job") and health[0] == "unknown":
        out.append("no health check result: the status page can't tell whether it is up")
    now = datetime.now(timezone.utc)
    for j in m.get("schedule") or []:
        run = next((r for r in runs if r["job"] == j["name"]), None)
        if run is None:
            continue
        if not run["ok"]:
            out.append(f"job {j['name']} failed its last run ({run['last_run']})")
    last_ok = _last_ok_runs(a["app"])
    for j in m.get("schedule") or []:
        ok = last_ok.get(j["name"])
        if ok is not None and now - ok > timedelta(days=7):
            out.append(f"job {j['name']} hasn't succeeded in 7 days (last: {ok.astimezone(v.TZ):%Y-%m-%d})")
    return out


def _last_ok_runs(app: str) -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT job, max(finished_at) FROM annex_ops.job_runs WHERE app = %s AND ok GROUP BY job", (app,)).fetchall()
    return dict(rows)


def trend_for(row: dict) -> str:
    return usage.trend(row["users_7d"], row["users_prev_7d"])


# --- day series (pure where it can be) ---------------------------------------------------------------------------
def fill_days(rows: dict[date, float], n: int, today: date) -> list[float]:
    """The last `n` days up to and including `today`, oldest first, zero where a day has no row."""
    return [rows.get(today - timedelta(days=n - 1 - i), 0) for i in range(n)]


def usage_today() -> date:
    return datetime.now(ZoneInfo(config.USAGE_TZ)).date()


def usage_series(app_names: list[str], n: int = SPARK_DAYS) -> dict[str, list[int]]:
    """Users per day of the last `n` days for each app (one query)."""
    if not app_names:
        return {}
    today = usage_today()
    with db.connect() as conn:
        rows = conn.execute("SELECT app, day, users FROM annex_ops.usage_daily WHERE app = ANY(%s) AND day > %s::date - %s",
                            (app_names, today, n)).fetchall()
    per: dict[str, dict[date, int]] = {a: {} for a in app_names}
    for app, day, users in rows:
        per[app][day] = users
    return {a: fill_days(per[a], n, today) for a in app_names}


# --- the host (M2.9) ----------------------------------------------------------------------------------------------
def host_latest(now: float | None = None) -> dict | None:
    """The newest stored host sample, or None (no table yet, no rows). `stale` when it is older than three storing intervals."""
    try:
        with db.connect() as conn:
            r = conn.execute("SELECT extract(epoch FROM ts), disk_used_pct, disk_free_gb, mem_available_mb, extract(epoch FROM backup_at) "
                             "FROM annex_ops.host_samples ORDER BY ts DESC LIMIT 1").fetchone()
    except (OpsError, psycopg.Error):
        return None
    if r is None:
        return None
    now = now or time.time()
    return {"ts": float(r[0]), "disk_used_pct": r[1], "disk_free_gb": r[2], "mem_available_mb": r[3],
            "backup_at": float(r[4]) if r[4] is not None else None, "stale": now - float(r[0]) > 3 * host.STORE_EVERY_S}


def host_series(days: int = 7) -> dict[str, list[float]]:
    """Hourly averages of disk used and memory available over the last `days` days, oldest first."""
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT date_trunc('hour', ts) h, avg(disk_used_pct), avg(mem_available_mb) FROM annex_ops.host_samples "
                                "WHERE ts > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 1", (days,)).fetchall()
    except (OpsError, psycopg.Error):
        return {"disk": [], "mem": []}
    return {"disk": [float(r[1]) for r in rows if r[1] is not None], "mem": [float(r[2]) for r in rows if r[2] is not None]}


# --- ranking and "needs attention" (pure) -----------------------------------------------------------------------
def severity(row: dict) -> int:
    """0 is the most urgent: down, 1 an incident, a failing job or a failed release, 2 unknown health, 3 open errors or warnings,
    4 a usage signal, 5 nothing."""
    if row["health"] == "down":
        return 0
    if row["incident"] or row["failing_jobs"] or row["failed_release"]:
        return 1
    if row["health"] == "unknown":
        return 2
    if row["open_errors"] or row["warnings"]:
        return 3
    if row["signal"]:
        return 4
    return 5


def needs_attention(row: dict) -> bool:
    return severity(row) <= 3


def attention_items(rows: list[dict], platform: list[dict], stale: bool, newest_age_s: float | None) -> list[dict]:
    """The ranked list for the top of the overview: {"sev": BAD|WARN, "text", "href"}. A stopped checker is itself an item, so a
    page of green dots never means "nobody is looking"."""
    out = []
    for r in sorted(rows, key=lambda r: (severity(r), r["app"])):
        a, href = r["app"], f"/apps/{r['app']}"
        if r["health"] == "down":
            out.append({"sev": BAD, "text": f"{a} is down" + (f": {r['health_detail']}" if r["health_detail"] else ""), "href": href})
        for j in r["failing_jobs"]:
            out.append({"sev": BAD if r["health"] != "down" else WARN, "text": f"{a}: job {j} failed its last run", "href": href})
        if r["failed_release"]:
            out.append({"sev": WARN, "text": f"{a}: a release failed {r['failed_release']}", "href": f"{href}/releases"})
        if r["health"] == "unknown":
            out.append({"sev": WARN, "text": f"{a} has no health result yet", "href": href})
        if r["new_errors"]:
            out.append({"sev": WARN, "text": f"{a}: {r['new_errors']} new error group{'s' if r['new_errors'] != 1 else ''} since yesterday", "href": f"{href}/errors"})
    for p in platform:
        out.append({"sev": BAD, "text": f"{p['subject'].removeprefix('platform:')}: {p['what']}", "href": "/platform"})
    if stale:
        when = "never" if newest_age_s is None else f"for {max(1, int(newest_age_s // 60))} min"
        out.append({"sev": WARN, "text": f"The health checker hasn't reported {when}: the health shown may be out of date", "href": "/status"})
    out.sort(key=lambda i: i["sev"])
    return out


def app_attention(d: dict, now: datetime) -> list[dict]:
    """One list of everything wrong with one app (its Summary tab): the incident and its signals, failed jobs, warnings, active
    error groups and a failed release of the last day. Each item says where to look."""
    app, out = d["app"], []
    if d["health"]["state"] == "down":
        out.append({"sev": BAD, "text": "Down" + (f": {d['health']['detail']}" if d["health"].get("detail") else ""), "href": f"/apps/{app}/logs?level=error&since=1d"})
    for i in d["incidents"]:
        sig = ", ".join(f"{k}: {x}" if x else k for k, x in i["signals"].items())
        out.append({"sev": BAD, "text": f"Open incident ({i['subject']}): {sig}", "href": f"/apps/{app}/logs?level=error&since=1d"})
    for j in d["jobs"]:
        if not j["ok"]:
            out.append({"sev": BAD, "text": f"Job {j['job']} failed its last run" + (f": {j['error']}" if j["error"] else ""),
                        "href": f"/apps/{app}/logs?source=all&grep={j['job']}"})
    job_text = {f"job {j['job']} failed its last run" for j in d["jobs"] if not j["ok"]}
    for w in d["warnings"]:
        if any(w.startswith(t) for t in job_text):
            continue  # already said, with its error
        out.append({"sev": WARN, "text": w, "href": f"/apps/{app}/config" if "health check" in w else f"/apps/{app}/logs"})
    for g in d["errors"]:
        if g["state"] == "active":
            out.append({"sev": WARN, "text": f"{g['title']} ({g['count']} times, since r{g['first_release']})", "href": f"/apps/{app}/errors"})
    fr = d.get("failed_release")
    if fr:
        out.append({"sev": WARN, "text": f"Release r{fr['release']} failed {fr['when']}", "href": f"/apps/{app}/releases"})
    out.sort(key=lambda i: i["sev"])
    return out


# --- the overview and the apps list -------------------------------------------------------------------------------
def _failed_recently(a: dict, now: datetime) -> dict | None:
    for r in a["releases"]:
        if r["result"] == "failed" and r["deployed_at_raw"] and now - r["deployed_at_raw"] < timedelta(days=1):
            return {"release": r["release"], "when": v.ago_text((now - r["deployed_at_raw"]).total_seconds())}
    return None


def _kpis(app_names: list[str], rows: list[dict], mine: list[dict], host_row: dict | None) -> dict:
    people, before = 0, 0
    if app_names:
        with db.connect() as conn:
            people, before = conn.execute(
                "SELECT count(DISTINCT login) FILTER (WHERE ts > now() - interval '7 days'), "
                "count(DISTINCT login) FILTER (WHERE ts <= now() - interval '7 days') FROM annex_ops.requests "
                "WHERE app = ANY(%s) AND ts > now() - interval '14 days'", (app_names,)).fetchone()
    now = datetime.now(timezone.utc)
    recent = [r for a in mine for r in a["releases"] if r["deployed_at_raw"] and now - r["deployed_at_raw"] < timedelta(days=7)]
    up = sum(1 for r in rows if r["health"] in ("up", "ok"))
    return {"apps": len(rows), "apps_up": up, "people_7d": people, "people_change": people - before,
            "open_errors": sum(r["open_errors"] for r in rows), "deploys_7d": len(recent), "deploys_failed_7d": sum(1 for r in recent if r["result"] == "failed"),
            "host": host_row}


def overview(who: v.Who) -> dict:
    """Everything the overview, the apps list and (for the status of the checker) the status page need, for what `who` may see."""
    now = datetime.now(timezone.utc)
    mine = [a for a in apps() if v.can_see_app(who, a["owner"])]
    states, newest = health_snapshot()
    open_all = incidents.list_incidents(open_only=True, limit=500)
    use = {r["app"]: r for r in usage.usage_rows()}
    names = [a["app"] for a in mine]
    series = usage_series(names)
    groups = errors_run.list_groups(None, "active", 1000)
    day_ago = time.time() - 86400
    rows = []
    for a in mine:
        inc = [i for i in open_all if i["app"] == a["app"]]
        h = app_health(a["app"], a["manifest"], states, inc)
        u = use.get(a["app"])
        runs = incidents.last_job_runs(a["app"])
        mine_groups = [g for g in groups if g["app"] == a["app"]]
        new = sum(1 for g in mine_groups if calendar.timegm(time.strptime(g["first_seen"], "%Y-%m-%dT%H:%M:%SZ")) > day_ago)
        warns = warnings_for(a, h, runs)
        rows.append({"app": a["app"], "owner": a["owner"], "health": h[0], "health_detail": h[1], "users_7d": u["users_7d"] if u else None,
                     "trend": trend_for(u) if u else "", "spark": series.get(a["app"], []), "open_errors": len(mine_groups), "new_errors": new,
                     "live": a["live"], "deployed_at": a["deployed_at_raw"], "signal": (u or {}).get("signal"), "incident": bool(inc),
                     "failing_jobs": [j["job"] for j in runs if not j["ok"]], "failed_release": (_failed_recently(a, now) or {}).get("when"),
                     "warnings": warns})
    platform = [{"subject": i["subject"], "opened_at": i["opened_at"], "what": ", ".join(i["signals"])}
                for i in open_all if i["subject"].startswith("platform:")] if who.admin else []
    interval = config.tuning().check_interval_s
    stale = checker_stale(newest, time.time(), interval)
    host_row = host_latest() if who.admin else None
    rows.sort(key=lambda r: (severity(r), r["app"]))
    fd = states.get(health.FRONT_DOOR)
    return {"rows": rows, "platform": platform, "host": host_row, "front_door": "unknown" if fd is None else "up" if fd["up"] else "down", "kpis": _kpis(names, rows, mine, host_row),
            "attention": attention_items(rows, platform, stale, None if newest is None else time.time() - newest),
            "stale": stale, "checked_at": newest, "feed": activity(8)[0] if who.admin else []}


# --- an app -----------------------------------------------------------------------------------------------------
def app_core(who: v.Who, app: str) -> dict | None:
    """What every tab of an app needs for its header: owner, live release, manifest, health, the open incidents and the jobs'
    last runs, the error groups and the releases. None if there is no such app."""
    found = next((a for a in apps() if a["app"] == app), None)
    if found is None:
        return None
    states = health_states()
    inc = incidents.list_incidents(open_only=True, app=app)
    h = app_health(app, found["manifest"], states, inc)
    g = states.get(f"app:{app}")
    runs = incidents.last_job_runs(app)
    errs = errors_run.list_groups(app, None, 100)
    return {"app": app, "owner": found["owner"], "live": found["live"], "manifest": found["manifest"], "found": found, "health_tuple": h,
            "health": {"state": h[0], "detail": h[1] or (g or {}).get("detail", "")}, "incidents": inc,
            "jobs": [{**j, "last_run_raw": j["last_run"], "last_run": j["last_run"].replace("T", " ")} for j in runs],
            "errors": errs, "active_errors": sum(1 for x in errs if x["state"] == "active"), "releases": found["releases"],
            "failed_release": _failed_recently(found, datetime.now(timezone.utc))}


def usage_part(who: v.Who, app: str) -> dict:
    """The usage of an app: totals (with the trend), the days, per person for admins, and 30-day series for the charts."""
    try:
        use = usage.usage_app(app, who.admin)
    except OpsError:
        use = None
    if use and use.get("app"):
        use["app"]["trend"] = usage.trend(use["app"]["users_7d"], use["app"]["users_prev_7d"])
    today = usage_today()
    by_day = {date.fromisoformat(x["day"]): x for x in (use or {}).get("days", [])}
    series = {k: fill_days({d: x[k] or 0 for d, x in by_day.items()}, 30, today) for k in ("users", "requests", "errors")}
    series["days"] = [(today - timedelta(days=29 - i)).strftime("%d.%m.") for i in range(30)]
    return {"usage": use, "series": series}


def app_detail(who: v.Who, app: str) -> dict | None:
    """Everything on an app's Summary tab, or None if there is no such app."""
    d = app_core(who, app)
    if d is None:
        return None
    try:
        recent = logstore.query_logs(app, 5, 7 * 86400, d["live"], "warn")  # the live release only: older ones are history (Logs tab)
    except OpsError:
        recent = []
    runs = [{"job": j["job"], "last_run": j["last_run_raw"], "ok": j["ok"]} for j in d["jobs"]]
    return {**d, **usage_part(who, app), "recent": recent, "warnings": warnings_for(d["found"], d["health_tuple"], runs)}


def log_lines(app: str, q: dict, before: datetime | None, n: int = 100) -> list[dict]:
    since = q.get("since") or None
    secs = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}.get(since) if since else None
    rel = q.get("release") or None
    if rel:
        rel = int(rel.lstrip("rR")) if rel.lstrip("rR").isdigit() else None
    return logstore.query_logs(app, n, secs, rel, q.get("level") or None, q.get("grep") or None, q.get("request") or None,
                               q.get("source") if q.get("source") in ("app", "gateway", "all") else "app", False, before)


# --- activity (M2.9): the audit trail and the platform's incidents in one timeline -----------------------------------------
CHIPS: dict[str, dict] = {
    "All": {}, "Deploys": {"action": ["deploy", "rollback", "app"]}, "Access": {"action": ["signin", "user", "secret"]},
    "Data reads": {"action": ["gateway", "data"]}, "Messages": {"action": ["email", "slack"]}, "Webhooks": {"action": ["webhook"]},
    "Failures": {"failures": True}, "Platform": {"platform_only": True},
}


def incident_events(app: str | None = None, limit: int = 100) -> list[dict]:
    """Incidents opening and closing as timeline events (they happened to the platform, not by a person)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT subject, app, opened_at, resolved_at, signals FROM annex_ops.incidents "
                            "WHERE (%s::text IS NULL OR app = %s) ORDER BY opened_at DESC LIMIT %s", (app, app, limit)).fetchall()
    out = []
    for subject, a, opened, resolved, signals in rows:
        det = {"subject": subject, "signals": ", ".join(signals or {})}
        out.append({"ts": opened, "actor": None, "app": a, "action": "incident.opened", "details": det, "kind": "platform"})
        if resolved:
            out.append({"ts": resolved, "actor": None, "app": a, "action": "incident.resolved", "details": {"subject": subject}, "kind": "platform"})
    return out


def activity(limit: int = 100, chip: str = "All", app: str | None = None, actor: str | None = None, since_s: float | None = None,
             before: datetime | None = None) -> tuple[list[dict], bool]:
    """(newest-first events, whether older ones exist). `chip` is a key of CHIPS."""
    spec = CHIPS.get(chip, {})
    events: list[dict] = []
    if not spec.get("platform_only"):
        for r in audit.timeline(app, since_s, spec.get("action"), actor, limit + 1, before, spec.get("failures", False)):
            events.append({**r, "ts": v.to_dt(r["ts"]), "kind": "audit"})
    if (chip in ("All", "Platform", "Failures")) and not actor:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=since_s) if since_s else None
        for ev in incident_events(app):
            if (before is None or ev["ts"] < before) and (cutoff is None or ev["ts"] >= cutoff):
                if chip == "Failures" and ev["action"] != "incident.opened":
                    continue
                events.append(ev)
    events.sort(key=lambda r: r["ts"], reverse=True)
    return events[:limit], len(events) > limit


# --- the platform page (M2.9) --------------------------------------------------------------------------------------
PLATFORM_TOOLS = [("logs", "Dozzle: live logs of every container, the platform's included"), ("metabase", "Metabase: analytics on the curated product data"),
                  ("mail", "Mailpit: the outgoing email catcher (prototype)"), ("gateway", "The data gateway: read-only API over curated product views")]


def platform_view() -> dict:
    """Everything the Platform page shows."""
    states, _ = health_snapshot()
    fd = states.get(health.FRONT_DOOR)
    t = config.tuning()
    open_all = [i for i in incidents.list_incidents(open_only=True, limit=500) if i["subject"].startswith("platform:")]
    return {"front_door": "unknown" if fd is None else "up" if fd["up"] else "down",
            "incidents": [{"subject": i["subject"], "opened_at": i["opened_at"], "what": ", ".join(i["signals"])} for i in open_all
                          if i["subject"] != "platform:front-door"],
            "host": host_latest(), "series": host_series(),
            "ops": threads.fetch(config.OPS_URL),  # annex-ops's own /healthz: threads and queues (D81); the console can't see inside ops
            "tuning": {k: getattr(t, k) for k in ("disk_alert_pct", "disk_clear_pct", "mem_alert_mb", "mem_clear_mb", "backup_max_age_s")},
            "links": [(name, url, what) for name, what in PLATFORM_TOOLS if (url := v.host_url(name))]}
