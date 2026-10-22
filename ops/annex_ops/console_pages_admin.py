"""Activity and Platform (M2.9), for admins. Pure."""
import json
from datetime import datetime
from urllib.parse import urlencode

from . import console_view as v
from .console_pages_overview import event_text
from .console_view import Raw, e

SINCE = {"1h": 3600, "1d": 86400, "7d": 604800, "30d": 2592000, "365d": 31536000}
PAGE = 100


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def detail(d: dict) -> Raw:
    """The details of an event on one line; a long one folds (the whole text is one click away)."""
    text = " ".join(f"{k}={_j(x) if not isinstance(x, str) else x}" for k, x in (d or {}).items())
    if len(text) <= 110:
        return Raw(e(text))
    return Raw(f'<details class="more"><summary>{e(text[:110])}…</summary>{e(text)}</details>')


def _qs(q: dict, **over) -> str:
    merged = {**q, **over}
    return urlencode({k: x for k, x in merged.items() if x})


def activity_page(who: v.Who, events: list[dict], more: bool, q: dict, chips: list[str], now: datetime | None = None) -> str:
    chip = q.get("chip") or "All"
    bar = v.chips([(c, "/audit?" + _qs({**q, "before": ""}, chip=c if c != "All" else ""), None) for c in chips], chip if chip in chips else "All")
    sel = lambda cur, opts: "".join(f'<option value="{o}"{" selected" if o == cur else ""}>{o or "any time"}</option>' for o in opts)  # noqa: E731
    form = (f'<form class="filters" method="get" action="/audit">'
            + (f'<input type="hidden" name="chip" value="{e(chip)}">' if chip != "All" else "")
            + f'<input name="app" placeholder="app" value="{e(q.get("app") or "")}" size="14" aria-label="app">'
            f'<input name="actor" placeholder="actor" value="{e(q.get("actor") or "")}" size="16" aria-label="actor">'
            f'<select name="since" aria-label="time range">{sel(q.get("since") or "", ["", "1h", "1d", "7d", "30d", "365d"])}</select><button>Show</button></form>')
    rows = []
    for ev in events:
        failed = ev["action"].endswith(("failed", "rejected"))
        if ev.get("kind") == "platform":
            rows.append([v.rel_time(ev["ts"], now), "platform", v.link(f"/apps/{ev['app']}", ev["app"]) if ev.get("app") else "", v.state("bad" if ev["action"] == "incident.opened" else "ok", ev["action"]),
                         detail(ev["details"])])
            continue
        actor = v.link("/audit?" + _qs({**q, "before": ""}, actor=ev["actor"]), ev["actor"]) if ev.get("actor") else ""
        app = v.link(f"/apps/{ev['app']}", ev["app"]) if ev.get("app") else ""
        rows.append([v.rel_time(ev["ts"], now), actor, app, v.state("failed", ev["action"]) if failed else ev["action"], detail(ev["details"])])
    older = ""
    if more and events:
        older = f'<p><a href="/audit?{e(_qs(q, before=events[-1]["ts"].isoformat()))}">Older</a></p>'
    body = ["<h1>Activity</h1>",
            '<p class="muted">Who did what, to which app, which data it read, what was sent, and what happened to the platform. Kept a year. For admins: it holds logins and the '
            "recipients of emails. A CLI actor is user@host as the CLI reports it (not verified yet, D58); gateway reads are summed per app, view and hour.</p>",
            str(bar), form, str(v.table(["When", "Actor", "App", "Action", "Details"], rows, "nothing matches")), older,
            str(v.cli_block("annex audit --since 7d", "annex audit --app <app> --action deploy"))]
    return v.page("Activity", who, "".join(body), "activity")


def _age(sec: float) -> str:
    """"5 min", "31 h", "4 d": hours up to three days, because a backup 1 d old and one 47 h old are not the same news."""
    return f"{int(sec // 3600)} h" if 86400 <= sec < 3 * 86400 else v.ago_text(sec).replace(" ago", "")


def ops_threads(ops: dict | None) -> Raw:
    """annex-ops's threads and buffers from its /healthz (`ops`, as threads.fetch returns it): one row per thread, the
    unhealthy ones first, and the queue depths with what was lost."""
    if not ops:
        return Raw("")
    if "unreachable" in ops:
        return v.section("annex-ops", Raw(f'<p>{v.state("unreachable", "annex-ops did not answer")} <span class="muted">{e(ops["unreachable"])}</span></p>'))
    rows = sorted(ops.get("threads", []), key=lambda t: (t["state"] == "ok", t["name"]))
    table = [[t["name"], v.state(t["state"]), v.ago_text(t["age_s"]) if t["age_s"] >= 60 else f'{t["age_s"]:.0f} s ago',
              f'{t["period_s"]:.0f} s'] for t in rows]
    queues = [[name, ", ".join(f"{k} {n}" for k, n in q.items())] for name, q in sorted((ops.get("queues") or {}).items())]
    lost = sum(n for q in (ops.get("queues") or {}).values() for k, n in q.items() if k in ("lost", "dropped") and isinstance(n, int))
    note = f"{len(rows)} threads" + (f", {lost} log or usage rows dropped since ops started" if lost else "")
    html = str(v.table(["Thread", "State", "Last progress", "Expected every"], table, "no threads reported")) \
        + str(v.table(["Buffer", "Now"], queues, "none"))
    return v.section("annex-ops", Raw(html), note + ". A thread silent for three times its period, or one that ended, turns the state red and ops' /healthz to 503.")


def platform_page(who: v.Who, p: dict, now: float | None = None) -> str:
    """p: front_door, incidents, host (latest sample or None), series, tuning (thresholds), links [(name, url, what)],
    ops (annex-ops's /healthz, optional)."""
    import time
    now = now or time.time()
    t, h = p["tuning"], p["host"]
    rows = [[v.link("/status", "front door"), v.state(p["front_door"]), ""]]
    rows += [[i["subject"].removeprefix("platform:"), v.state("down"), v.raw(i["what"], " since ", v.rel_time(i["opened_at"]))] for i in p["incidents"]]
    body = ["<h1>Platform</h1>", str(v.section("Health", v.table(["Part", "State", "Detail"], rows, "nothing to show"))),
            str(ops_threads(p.get("ops")))]
    if h is None:
        host_html = '<p class="muted">No host samples yet. annex-ops stores one every 5 minutes.</p>'
    else:
        stale = ' <span class="chip warn">not sampled recently</span>' if h["stale"] else ""
        disk, mem, backup = h["disk_used_pct"], h["mem_available_mb"], h["backup_at"]
        disk_state = "bad" if disk is not None and disk >= t["disk_alert_pct"] else "warn" if disk is not None and disk >= t["disk_clear_pct"] else "ok"
        mem_state = "bad" if mem is not None and mem < t["mem_alert_mb"] else "warn" if mem is not None and mem < t["mem_clear_mb"] else "ok"
        if backup is None:
            backup_cell = '<span class="muted">unknown</span>'
        else:
            age = now - backup
            backup_cell = f'{v.state("ok" if age <= t["backup_max_age_s"] else "bad", _age(age) + " old")} <span class="muted">(alert after {_age(t["backup_max_age_s"])})</span>'
        rows = [["Disk used", Raw(str(v.meter(disk, t["disk_clear_pct"], t["disk_alert_pct"], "disk used")) + f' {"?" if disk is None else format(disk, ".0f")} %'
                                   f' <span class="muted">(alert at {t["disk_alert_pct"]:.0f} %{"" if h["disk_free_gb"] is None else f", {h["disk_free_gb"]:.1f} GB free"})</span>'),
                 v.sparkline(p["series"]["disk"], "disk used per hour, last 7 days", 120, 24)],
                ["Memory available", Raw(f'{v.state(mem_state, "?" if mem is None else format(mem, ".0f") + " MB")} <span class="muted">(alert below {t["mem_alert_mb"]:.0f} MB)</span>'),
                 v.sparkline(p["series"]["mem"], "memory available per hour, last 7 days", 120, 24)],
                ["Newest backup", Raw(backup_cell), ""]]
        host_html = str(v.table(["", "Now", "Last 7 days"], rows)) + f'<p class="muted hint">Sampled {_age(now - h["ts"])} ago.{stale}</p>'
    body.append(str(v.section("The server", Raw(host_html))))
    links = [[v.link(url, name), what] for name, url, what in p["links"]]
    body.append(str(v.section("Tools", v.table(["Tool", "What it is for"], links, "none"))))
    body.append(str(v.cli_block("annex list", "annex audit --since 1d")))
    return v.page("Platform", who, "".join(body), "platform")
