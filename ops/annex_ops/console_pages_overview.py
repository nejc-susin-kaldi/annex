"""The overview, the apps list and the status page (M2.7). Pure: a page is a function of the data `console_data` read."""
from urllib.parse import urlencode

from . import console_view as v
from .console_view import Raw, e

FILTERS = ["All", "Needs attention", "Dormant", "Graduating", "Mine"]


def event_text(ev: dict) -> Raw:
    """One timeline event as a line: who did what, to which app. Platform events (incidents) have no person."""
    app = v.link(f"/apps/{ev['app']}", ev["app"]) if ev.get("app") else ""
    d = ev.get("details") or {}
    if ev.get("kind") == "platform":
        what = f"{d.get('subject', '')}" + (f": {d['signals']}" if d.get("signals") else "")
        return v.raw(v.state("bad" if ev["action"] == "incident.opened" else "ok", ev["action"].replace("incident.", "incident ")), " ", what)
    actor = v.link("/audit?" + urlencode({"actor": ev["actor"]}), ev["actor"]) if ev.get("actor") else "-"
    return v.raw(actor, " ", ev["action"], " ", app)


def signal_chips(r: dict) -> Raw:
    out = []
    if r["incident"]:
        out.append(v.chip("incident", "bad"))
    if r["failing_jobs"]:
        out.append(v.chip("job failing", "bad"))
    if r["failed_release"]:
        out.append(v.chip("release failed", "warn"))
    if r["signal"] == "dormant":
        out.append(v.chip("dormant", "warn"))
    elif r["signal"] == "graduation":
        out.append(v.chip("graduation candidate", "ok"))
    if r["warnings"]:
        out.append(v.chip(f"{len(r['warnings'])} warning{'s' if len(r['warnings']) != 1 else ''}", "warn"))
    return Raw(" ".join(out))


def apps_table(rows: list[dict], now=None) -> Raw:
    trs = []
    for r in rows:
        users = "" if r["users_7d"] is None else v.raw(f"{r['users_7d']} {r['trend']} ", v.sparkline(r["spark"], "users per day, last 14 days"))
        deploy = v.raw(f"r{r['live']} · ", v.rel_time(r["deployed_at"], now)) if r["live"] and r["deployed_at"] else Raw('<span class="muted">no release record</span>')
        health = v.raw(v.state(r["health"]), Raw(f'<small>{e(r["health_detail"])}</small>') if r["health_detail"] else "")
        trs.append([v.raw(v.link(f"/apps/{r['app']}", r["app"]), Raw(f'<small>{e(r["owner"] or "unknown owner")}</small>')), health, users,
                    r["open_errors"] or "", deploy, signal_chips(r)])
    return v.table(["App", "Health", "Users 7d", "Errors", "Last deploy", "Signal"], trs, "no apps")


def _attention(ov: dict, now_text: str = "") -> Raw:
    items = ov["attention"]
    if not items:
        return Raw('<div class="attn ok"><h2>All clear</h2><p class="muted" style="margin:0">Nothing needs attention. Health last checked '
                   f'{cell_time(ov)}.</p></div>')
    worst = "bad" if any(i["sev"] == 0 for i in items) else "warn"
    lis = "".join(f'<li>{v.state("bad" if i["sev"] == 0 else "warn", "")}<a href="{e(i["href"])}">{e(i["text"])}</a></li>' for i in items)
    return Raw(f'<div class="attn {worst}"><h2>Needs attention: {len(items)}</h2><ul>{lis}</ul></div>')


def cell_time(ov: dict) -> str:
    return str(v.rel_time(ov["checked_at"])) if ov.get("checked_at") else "never"


def _kpis(who: v.Who, k: dict) -> Raw:
    tiles = [v.kpi("Apps up", f"{k['apps_up']} / {k['apps']}"),
             v.kpi("People, 7 days", k["people_7d"], f"{'+' if k['people_change'] > 0 else ''}{k['people_change']} on the week before" if k["people_change"] else "same as the week before"),
             v.kpi("Open error groups", k["open_errors"]),
             v.kpi("Deploys, 7 days", k["deploys_7d"], f"{k['deploys_failed_7d']} failed" if k["deploys_failed_7d"] else "none failed")]
    h = k["host"]
    if who.admin:
        if h and not h["stale"]:
            tiles.append(v.kpi("Disk used", f"{h['disk_used_pct']:.0f} %" if h["disk_used_pct"] is not None else "?",
                               f"{h['mem_available_mb']:.0f} MB memory free" if h["mem_available_mb"] is not None else ""))
        else:
            tiles.append(v.kpi("Disk used", "?", "not sampled recently"))
    return v.kpis(*tiles)


def _platform_card(ov: dict) -> Raw:
    h = ov["host"]
    lines = [f'<p>Front door {v.state(ov["front_door"])}</p>']
    if ov["platform"]:
        lines.append("<ul>" + "".join(f'<li>{v.state("bad", "")}{e(p["subject"].removeprefix("platform:"))}: {e(p["what"])}</li>' for p in ov["platform"]) + "</ul>")
    else:
        lines.append('<p class="muted">No open platform incidents.</p>')
    if h and not h["stale"]:
        lines.append(f'<p>Disk used: {"?" if h["disk_used_pct"] is None else format(h["disk_used_pct"], ".0f") + " %"} · '
                     f'Memory available: {"?" if h["mem_available_mb"] is None else format(h["mem_available_mb"], ".0f") + " MB"}</p>')
    else:
        lines.append('<p class="muted">Host numbers: not sampled recently.</p>')
    lines.append('<p><a href="/platform">Platform details</a></p>')
    return v.card("Platform", Raw("".join(lines)))


def _feed_card(ov: dict) -> Raw:
    if not ov["feed"]:
        return v.card("Latest activity", Raw('<p class="muted">nothing yet</p>'))
    lis = "".join(f"<li>{v.rel_time(ev['ts'])} {event_text(ev)}</li>" for ev in ov["feed"])
    return v.card("Latest activity", Raw(f'<ul class="feed">{lis}</ul><p><a href="/audit">All activity</a></p>'))


def overview_page(who: v.Who, ov: dict, shown: int = 6) -> str:
    rows = ov["rows"]
    if not rows and not who.admin:
        return v.page("Overview", who, "<h1>Overview</h1><p>You don't own any apps, so there is nothing to show here. "
                      '<a href="/status">Status</a> shows whether everything is up.</p>', "overview")
    table = apps_table(rows[:shown])
    more = f'<p><a href="/apps">All {len(rows)} apps</a></p>' if len(rows) > shown else '<p><a href="/apps">Apps list</a></p>'
    left = v.card("Apps", Raw(str(table) + more + '<p class="muted hint">Most urgent first.</p>'))
    body = [f"<h1>{'Overview' if who.admin else 'Your apps'}</h1>", str(_attention(ov)), str(_kpis(who, ov["kpis"]))]
    if who.admin:
        body.append(f'<div class="cols"><div>{left}</div><div style="display:grid;gap:1rem">{_platform_card(ov)}{_feed_card(ov)}</div></div>')
    else:
        body.append(str(left))
    body.append(str(v.cli_block("annex list", "annex status <app>")))
    return v.page("Overview", who, "".join(body), "overview")


def filter_rows(rows: list[dict], who: v.Who, f: str, q: str) -> list[dict]:
    from .console_data import needs_attention
    out = rows
    if f == "Needs attention":
        out = [r for r in out if needs_attention(r)]
    elif f == "Dormant":
        out = [r for r in out if r["signal"] == "dormant"]
    elif f == "Graduating":
        out = [r for r in out if r["signal"] == "graduation"]
    elif f == "Mine":
        out = [r for r in out if r["owner"] == who.login]
    q = (q or "").strip().lower()
    if q:
        out = [r for r in out if q in r["app"].lower() or q in (r["owner"] or "").lower()]
    return out


def apps_page(who: v.Who, ov: dict, f: str = "All", q: str = "") -> str:
    rows = ov["rows"]
    if not rows and not who.admin:
        return v.page("Apps", who, "<h1>Apps</h1><p>You don't own any apps, so there is nothing to show here. "
                      '<a href="/status">Status</a> shows whether everything is up.</p>', "apps")
    f = f if f in FILTERS else "All"
    from .console_data import needs_attention
    counts = {"All": len(rows), "Needs attention": sum(1 for r in rows if needs_attention(r)), "Dormant": sum(1 for r in rows if r["signal"] == "dormant"),
              "Graduating": sum(1 for r in rows if r["signal"] == "graduation"), "Mine": sum(1 for r in rows if r["owner"] == who.login)}
    keys = [k for k in FILTERS if k != "Mine" or (who.admin and counts["Mine"])]
    qs = lambda k: "/apps?" + urlencode({k2: v2 for k2, v2 in (("f", k if k != "All" else ""), ("q", q)) if v2})  # noqa: E731
    bar = v.chips([(k, qs(k), counts[k]) for k in keys], f)
    search = (f'<form class="filters" method="get" action="/apps"><input name="q" placeholder="app or owner" value="{e(q)}" size="18">'
              + (f'<input type="hidden" name="f" value="{e(f)}">' if f != "All" else "") + "<button>Search</button></form>")
    shown = filter_rows(rows, who, f, q)
    body = [f"<h1>{'All apps' if who.admin else 'Your apps'}</h1>", str(bar), search, str(apps_table(shown)) if shown else '<p class="muted">no apps match</p>',
            str(v.cli_block("annex list", "annex status <app>", "annex usage"))]
    return v.page("Apps", who, "".join(body), "apps")


def status_page(who: v.Who, apps: list[dict], platform: list[dict], stale: bool = False) -> str:
    """Everyone signed in sees this: which apps and platform parts are up. Names and up/down only, like the discovery page."""
    bad = [a for a in apps + platform if a["state"] in ("down", "error")]
    if stale:
        head = '<p class="attn warn"><b>The health checker isn\'t answering</b>, so what is shown may be out of date. The platform team has been told.</p>'
    elif bad:
        head = f'<p class="attn bad"><b>{len(bad)} thing{"s" if len(bad) != 1 else ""} down.</b> The owners and admins have been told.</p>'
    else:
        head = '<p class="attn ok"><b>Everything is up.</b></p>'
    body = (f"<h1>Status</h1>{head}"
            + str(v.section("Apps", v.table(["App", "State", "Detail"], [[a["app"], v.state(a["state"]), a.get("detail", "")] for a in apps], "no apps")))
            + str(v.section("Platform", v.table(["Part", "State", "Detail"], [[p["name"], v.state(p["state"]), p.get("detail", "")] for p in platform], "nothing to show"))))
    return v.page("Status", who, body, "status" if not who.admin else "overview")
