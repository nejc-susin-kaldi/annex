"""What the console's pages show, read with the console's read-only database role (M2.4).

Almost everything is a function that already exists for `annex status`, `annex usage`, `annex errors` and `annex logs`
(usage.usage_app, incidents.list_incidents, errors_run.list_groups, logstore.query_logs), so a number on a page and the
same number from the CLI come from the same query. What's new here: the apps and their owners (from the release
records, `annex_ops.releases`, read for an app's latest life: removing an app restarts numbering, D65), the status
view (the health checker's results, `annex_ops.health_checks`) and the warnings.
"""
import os
import time
from datetime import datetime, timedelta, timezone

from . import config, console_view as v, db, errors_run, health, host, incidents, logstore, usage
from .errors import OpsError

NO_RECORD = "deployed before releases were recorded"


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
                    "deployed_at": live["deployed_at"] if live else "", "deployed_by": live["deployed_by"] if live else "",
                    "releases": rels})
    return out


def health_states() -> dict[str, dict]:
    """The health checker's newest result per check, keyed `app:<name>` and `platform:front-door`: {"up": bool, "detail": ...}.
    A result older than the checker's freshness limit is left out (it isn't running, or the app is new); {} if the table can't be read."""
    try:
        rows = health.latest()
    except OpsError:
        return {}
    now, out = time.time(), {}
    for key, row in rows.items():
        st = health.state_of(row, now, config.tuning().check_interval_s)
        if st["state"] != "unknown":
            out[key] = {"up": st["state"] == "up", "detail": st.get("detail", "")}
    return out


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


def fleet(who: v.Who) -> dict:
    all_apps = apps()
    mine = [a for a in all_apps if v.can_see_app(who, a["owner"])]
    states = health_states()
    open_all = incidents.list_incidents(open_only=True, limit=500)
    use = {r["app"]: r for r in usage.usage_rows()}
    groups = errors_run.list_groups(None, "active", 1000)
    rows = []
    for a in mine:
        inc = [i for i in open_all if i["app"] == a["app"]]
        health = app_health(a["app"], a["manifest"], states, inc)
        u = use.get(a["app"])
        runs = incidents.last_job_runs(a["app"])
        flags = ([u["signal"]] if u and u["signal"] else []) + (["incident"] if inc else [])
        rows.append({"app": a["app"], "owner": a["owner"], "health": health[0], "users_7d": u["users_7d"] if u else None,
                     "trend": trend_for(u) if u else "", "open_errors": sum(1 for g in groups if g["app"] == a["app"]),
                     "last_deploy": (f"r{a['live']} {a['deployed_at']}" if a["live"] else NO_RECORD), "flags": flags,
                     "warnings": len(warnings_for(a, health, runs))})
    plat = []
    if who.admin:
        plat = [{"subject": i["subject"], "opened_at": i["opened_at"], "what": ", ".join(i["signals"])}
                for i in open_all if i["subject"].startswith("platform:")]
    return {"rows": rows, "platform": plat, "host": host_numbers() if who.admin else None}


def host_numbers() -> dict:
    pct = mem = None
    try:
        pct, _ = host.disk_usage(os.statvfs("/"))
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = host.mem_available_mb(f.read())
    except OSError:
        pass
    return {"disk_used_pct": pct, "mem_available_mb": mem}


def app_detail(who: v.Who, app: str) -> dict | None:
    """Everything on an app's page, or None if there is no such app."""
    found = next((a for a in apps() if a["app"] == app), None)
    if found is None:
        return None
    states = health_states()
    inc = incidents.list_incidents(open_only=True, app=app)
    health = app_health(app, found["manifest"], states, inc)
    g = states.get(f"app:{app}")
    runs = incidents.last_job_runs(app)
    try:
        use = usage.usage_app(app, who.admin)
    except OpsError:
        use = None
    if use and use.get("app"):
        use["app"]["trend"] = usage.trend(use["app"]["users_7d"], use["app"]["users_prev_7d"])
    return {"app": app, "owner": found["owner"], "live": found["live"], "manifest": found["manifest"],
            "health": {"state": health[0], "detail": health[1] or (g or {}).get("detail", "")}, "incidents": inc,
            "jobs": [{**j, "last_run": j["last_run"].replace("T", " ")} for j in runs], "usage": use,
            "errors": errors_run.list_groups(app, None, 100), "releases": found["releases"],
            "warnings": warnings_for(found, health, runs)}


def log_lines(app: str, q: dict, before: datetime | None, n: int = 100) -> list[dict]:
    since = q.get("since") or None
    secs = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}.get(since) if since else None
    rel = q.get("release") or None
    if rel:
        rel = int(rel.lstrip("rR")) if rel.lstrip("rR").isdigit() else None
    return logstore.query_logs(app, n, secs, rel, q.get("level") or None, q.get("grep") or None, q.get("request") or None,
                               q.get("source") if q.get("source") in ("app", "gateway", "all") else "app", False, before)
