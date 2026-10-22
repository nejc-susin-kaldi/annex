"""An app's pages (M2.8): Summary, Logs, Errors, Releases, Usage, Config, each its own URL under /apps/<app>. Pure."""
import json
from datetime import datetime, timezone

from . import console_view as v
from .console_view import Raw, e

TABS = [("summary", "", "Summary"), ("logs", "/logs", "Logs"), ("errors", "/errors", "Errors"), ("releases", "/releases", "Releases"),
        ("usage", "/usage", "Usage"), ("config", "/config", "Config")]


def header(who: v.Who, d: dict, active: str) -> str:
    """The same top of every tab: name, health, owner, live release, type and memory, and the tab bar."""
    app, m = d["app"], d.get("manifest") or {}
    health = d["health"]
    bits = [f"owner {e(d['owner'] or 'unknown')}", f"live r{d['live']}" if d["live"] else "no release recorded"]
    if m:
        bits += [e(m.get("type") or ""), f"{m.get('memory_mb', 256)} MB"]
    url = v.host_url(app)
    if url:
        bits.append(f'<a href="{e(url)}">open the app</a>')
    n = d.get("active_errors")
    tabs = "".join(f'<a href="/apps/{e(app)}{path}"' + (' aria-current="page"' if key == active else "") + f">{label}"
                   + (f'<span class="n">{n}</span>' if key == "errors" and n else "") + "</a>" for key, path, label in TABS)
    return (f'<h1>{e(app)} {v.state(health["state"])}</h1>'
            f'<p class="muted">{" · ".join(bits)}</p>'
            + (f'<p class="muted hint">{e(health["detail"])}</p>' if health.get("detail") else "")
            + f'<nav class="tabs" aria-label="{e(app)} sections">{tabs}</nav>')


def shell(who: v.Who, d: dict, active: str, body: str, title: str) -> str:
    return v.page(f"{d['app']} · {title}", who, header(who, d, active) + body, "apps", [("/apps", "Apps"), (f"/apps/{d['app']}", d["app"])] if active != "summary" else [("/apps", "Apps")])


# --- summary --------------------------------------------------------------------------------------------------
def _usage_tiles(d: dict) -> Raw:
    u, s = d["usage"], d["series"]
    a = (u or {}).get("app")
    if not a:
        return v.kpis(v.kpi("Users, 7 days", "-", "no usage recorded yet"), v.kpi("Requests, 7 days", "-"), v.kpi("Server errors, 7 days", "-"), v.kpi("p95", "-"))
    return v.kpis(v.kpi("Users, 7 days", a["users_7d"], "same as the week before" if a["trend"] == "=" else f"{a['trend']} on the week before", v.sparkline(s["users"][-14:], "users per day")),
                  v.kpi("Requests, 7 days", a["requests_7d"], "", v.sparkline(s["requests"][-14:], "requests per day")),
                  v.kpi("Server errors, 7 days", a["errors_7d"], "", v.sparkline(s["errors"][-14:], "server errors per day")),
                  v.kpi("Response time p95", f"{a['p95_7d']} ms" if a["p95_7d"] is not None else "-", "7 days"))


def _attention(items: list[dict]) -> Raw:
    if not items:
        return Raw('<div class="attn ok"><h2>Nothing needs attention</h2></div>')
    worst = "bad" if any(i["sev"] == 0 for i in items) else "warn"
    lis = "".join(f'<li>{v.state("bad" if i["sev"] == 0 else "warn", "")}<a href="{e(i["href"])}">{e(i["text"])}</a></li>' for i in items)
    return Raw(f'<div class="attn {worst}"><h2>Needs attention: {len(items)}</h2><ul>{lis}</ul></div>')


def release_state(r: dict) -> Raw:
    return v.state("live" if r["live"] else "failed" if r["result"] == "failed" else "superseded", "live" if r["live"] else r["result"] if r["result"] == "failed" else "superseded")


def summary_page(who: v.Who, d: dict, attention: list[dict], now: datetime | None = None) -> str:
    app = d["app"]
    body = [str(_usage_tiles(d)), str(_attention(attention))]
    if d["jobs"]:
        body.append(str(v.section("Jobs", v.table(["Job", "Last run", "Result", "Took", "Error"],
                    [[v.link(f"/apps/{app}/logs?source=all&grep={j['job']}", j["job"]), v.rel_time(j.get("last_run_raw") or j["last_run"].replace(" ", "T"), now),
                      v.state("ok" if j["ok"] else "failed"), f"{j['duration_ms']} ms" if j["duration_ms"] is not None else "", j["error"] or ""] for j in d["jobs"]]))))
    rel = [[f"r{r['release']}", release_state(r), v.rel_time(r["deployed_at_raw"], now), r["deployed_by"] or "", r["note"] or ""] for r in d["releases"][:3]]
    recent = ""
    if d["recent"]:
        recent = "".join(log_line(r) for r in d["recent"]) + f'<p><a href="/apps/{e(app)}/logs?level=warn&since=7d' + (f'&release={d["live"]}' if d["live"] else '') + '">More warnings and errors</a></p>'
    else:
        recent = '<p class="muted">No warnings or errors in the last 7 days.</p>'
    body.append('<div class="cols" style="grid-template-columns:repeat(auto-fit,minmax(20rem,1fr))">'
                + str(v.card("Latest releases", Raw(str(v.table(["Release", "Result", "When", "By", "Note"], rel, "no release history (deployed before releases were recorded)"))
                                                    + f'<p><a href="/apps/{e(app)}/releases">All releases</a></p>')))
                + str(v.card(f"Latest warnings and errors of r{d['live']}" if d["live"] else "Latest warnings and errors", Raw(recent))) + "</div>")
    body.append(str(v.cli_block(f"annex status {app}", f"annex run {app} <job>", f"annex rollback {app}")))
    return shell(who, d, "summary", "".join(body), "Summary")


# --- logs -----------------------------------------------------------------------------------------------------
def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def log_line(r: dict) -> str:
    cls = " err" if r["level"] == "error" else " wrn" if r["level"] == "warn" else ""
    fields = r.get("fields") or {}
    extra = " ".join(f"{k}={x if isinstance(x, str) and x.replace('.', '').replace('_', '').replace('-', '').isalnum() else _j(x)}"
                     for k, x in fields.items() if k != "stack")
    tag = f"[{r['container']}] " if r["container"] == "gateway" or r["container"].startswith("job:") else ""
    rel = f"r{r['release']} " if r["release"] is not None else ""
    rid = f" request_id={r['request_id']}" if r["request_id"] else ""
    text = f"{r['ts'][:23]}Z {rel}{tag}{(r['level'] or '').ljust(5)} {r['msg']}{rid}{('  ' + extra) if extra else ''}"
    return f'<div class="log{cls}">{e(text)}</div>'


def logs_page(who: v.Who, d: dict, rows: list[dict], f: dict, older: str | None) -> str:
    app = d["app"]
    sel = lambda cur, opts: "".join(f'<option value="{o}"{" selected" if o == cur else ""}>{o or "any"}</option>' for o in opts)  # noqa: E731
    form = (f'<form class="filters" method="get" action="/apps/{e(app)}/logs">'
            f'<select name="level" aria-label="level">{sel(f.get("level") or "", ["", "debug", "info", "warn", "error"])}</select>'
            f'<input name="release" placeholder="release, e.g. r4" value="{e(f.get("release") or "")}" size="12" aria-label="release">'
            f'<input name="grep" placeholder="text" value="{e(f.get("grep") or "")}" aria-label="text">'
            f'<input name="request" placeholder="request id" value="{e(f.get("request") or "")}" size="18" aria-label="request id">'
            f'<select name="since" aria-label="age">{sel(f.get("since") or "", ["", "1h", "6h", "1d", "7d"])}</select>'
            f'<select name="source" aria-label="source">{sel(f.get("source") or "app", ["app", "gateway", "all"])}</select><button>Search</button></form>')
    body = ['<p class="muted hint">Stored for 14 days, also after a release was rolled back or a container replaced.</p>', form]
    body.append("".join(log_line(r) for r in rows) if rows else '<p class="muted">no lines match</p>')
    if older:
        body.append(f'<p><a href="{e(older)}">Older lines</a></p>')
    cmd = "annex logs " + app + "".join(f" --{k} {x}" for k, x in f.items() if x and k not in ("source",))
    body.append(str(v.cli_block(cmd)))
    return shell(who, d, "logs", "".join(body), "Logs")


# --- errors ---------------------------------------------------------------------------------------------------
def errors_page(who: v.Who, d: dict, state: str = "active", now: datetime | None = None) -> str:
    app, groups = d["app"], d["errors"]
    state = state if state in ("active", "quiet", "all") else "active"
    shown = [g for g in groups if state == "all" or g["state"] == state]
    counts = {"active": sum(1 for g in groups if g["state"] == "active"), "quiet": sum(1 for g in groups if g["state"] == "quiet"), "all": len(groups)}
    bar = v.chips([(k, f"/apps/{app}/errors?s={k}", counts[k]) for k in ("active", "quiet", "all")], state)
    rows = [[v.state(g["state"]), g["kind"], g["title"], g["count"], v.rel_time(g["first_seen"], now), v.rel_time(g["last_seen"], now),
             f"r{g['first_release']}" if g["first_release"] == g["last_release"] else f"r{g['first_release']}–r{g['last_release']}", g["user"] or "",
             v.link(f"/apps/{app}/logs?request={g['request_id']}", g["request_id"]) if g["request_id"] else ""] for g in shown]
    body = [str(bar), str(v.table(["State", "Kind", "What", "Seen", "First", "Last", "Release", "User", "Request"], rows,
                                  "no errors seen" if not groups else "no errors in this state")),
            '<p class="muted hint">A new kind of error emails the owner once. Quiet means not seen lately, or fixed by a newer release.</p>',
            str(v.cli_block(f"annex errors {app}"))]
    return shell(who, d, "errors", "".join(body), "Errors")


# --- releases ---------------------------------------------------------------------------------------------------
RELEASES_SHOWN = 25


def releases_page(who: v.Who, d: dict, now: datetime | None = None, show_all: bool = False) -> str:
    app = d["app"]
    shown = d["releases"] if show_all else d["releases"][:RELEASES_SHOWN]
    rows = [[f"r{r['release']}", release_state(r), v.rel_time(r["deployed_at_raw"], now), r["deployed_by"] or "", r["via"] or "", r["git"] or "", r.get("cli") or "", r["note"] or "",
             v.link(f"/apps/{app}/logs?release={r['release']}", "logs")] for r in shown]
    more = (f'<p class="muted">Showing the newest {RELEASES_SHOWN} of {len(d["releases"])}. <a href="/apps/{e(app)}/releases?all=1">Show all</a></p>'
            if len(shown) < len(d["releases"]) else "")
    body = [str(v.table(["Release", "Result", "When", "By", "Via", "Git", "CLI", "Note", ""], rows, "no release history (deployed before releases were recorded)")), more,
            str(v.cli_block(f"annex releases {app}", f"annex rollback {app}", f"annex deploy <folder>"))]
    return shell(who, d, "releases", "".join(body), "Releases")


# --- usage ------------------------------------------------------------------------------------------------------
def usage_page(who: v.Who, d: dict) -> str:
    app, u, s = d["app"], d["usage"], d["series"]
    a = (u or {}).get("app")
    if not a:
        return shell(who, d, "usage", '<p class="muted">No usage recorded yet.</p>' + str(v.cli_block(f"annex usage {app}")), "Usage")
    weeks = [a[k] for k in ("week1", "week2", "week3", "week4")]
    body = [f"<p>{a['users_7d']} user(s) in the last 7 days ({e(a['trend'])}), {a['users_30d']} in 30 days, {a['requests_7d']} requests, "
            f"{a['errors_7d']} server errors, p95 {a['p95_7d'] if a['p95_7d'] is not None else '-'} ms. Signal: {e(a['signal'] or 'none')}.</p>",
            str(v.section("Users per day, last 30 days", v.bars(s["users"], s["days"], unit="users"))),
            str(v.section("Requests per day, server errors in red", v.bars(s["requests"], s["days"], s["errors"], unit="requests"))),
            str(v.section("Users per week, last 4 complete weeks", v.bars([x or 0 for x in weeks], ["4 weeks ago", "3 weeks ago", "2 weeks ago", "last week"], unit="users", h=90))),
            '<details class="more"><summary>The numbers behind the charts</summary>'
            + str(v.table(["Day", "Users", "Requests", "Errors", "p95 ms"], [[x["day"], x["users"], x["requests"], x["errors"], x["p95_ms"] if x["p95_ms"] is not None else ""]
                                                                           for x in u["days"]], "no daily numbers yet")) + "</details>"]
    if who.admin and u.get("by_user") is not None:
        body.append('<section><h2>Per person, last 30 days</h2><p class="box">Personal data: for admins, don\'t share it.</p>'
                    + str(v.table(["Login", "Requests"], [[x["login"], x["requests"]] for x in u["by_user"]], "nobody")) + "</section>")
    body.append(str(v.cli_block(f"annex usage {app}")))
    return shell(who, d, "usage", "".join(body), "Usage")


# --- config -----------------------------------------------------------------------------------------------------
def injected_names(m: dict) -> list[str]:
    """The names (never values) of the variables Annex puts in the app's environment, from its manifest."""
    names = {"ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL"}
    if m.get("type") in ("web", "web+job"):
        names.add("PORT")
    if m.get("postgres"):
        names.add("DATABASE_URL")
    if m.get("files"):
        names.add("DATA_DIR")
    if m.get("views"):
        names |= {"ANNEX_GATEWAY_URL", "ANNEX_GATEWAY_TOKEN"}
    if m.get("notify_email") or m.get("notify_slack"):
        names |= {"ANNEX_OPS_URL", "ANNEX_OPS_TOKEN"}
    if m.get("webhooks"):
        names |= {"ANNEX_HOOKS_URL", "ANNEX_HOOK_TOKEN"}
    names |= set(m.get("secrets") or [])
    return sorted(names)


def config_groups(m: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    j = lambda xs: ", ".join(map(str, xs)) if xs else "none"  # noqa: E731
    return [
        ("Runtime", [("type", m.get("type") or ""), ("description", m.get("description") or ""),
                     ("port / health check", f"{m.get('port')} {m.get('healthcheck') or ''}".strip()), ("memory", f"{m.get('memory_mb', 256)} MB"),
                     ("schedule", j([f"{x['name']} ({x['cron']})" for x in m.get("schedule") or []]))]),
        ("Storage", [("Postgres", "yes" if m.get("postgres") else "no"), ("files", "yes" if m.get("files") else "no")]),
        ("Data access", [("gateway views", j(m.get("views")))]),
        ("Notifications", [("email", "allowed" if m.get("notify_email") else "no"), ("Slack channels", j(m.get("notify_slack"))),
                           ("alerts to", j(m.get("alerts_to") or ["owner"]))]),
        ("Webhooks", [("incoming", j([f"{w['name']} ({w['verify']})" for w in m.get("webhooks") or []]))]),
        ("Secrets and environment", [("env (set in annex.yaml)", j([f"{k}={x}" for k, x in sorted((m.get("env") or {}).items())])),
                                     ("secrets (names only)", j(m.get("secrets"))), ("injected variables (names only)", j(injected_names(m)))]),
    ]


def config_page(who: v.Who, d: dict) -> str:
    m = d["manifest"]
    if not m:
        body = '<p class="muted">no manifest recorded</p>'
    else:
        body = "".join(f"<section><h2>{e(t)}</h2><dl class=\"kv\">" + "".join(f"<dt>{e(k)}</dt><dd>{e(x)}</dd>" for k, x in rows) + "</dl></section>"
                       for t, rows in config_groups(m))
        live = next((r for r in d.get("releases") or [] if r.get("live")), None)
        if live:
            body += ('<section><h2>Release</h2><dl class="kv">' + f"<dt>live release</dt><dd>r{e(live['release'])}</dd>"
                     + f"<dt>made with CLI commit</dt><dd>{e(live.get('cli') or 'not recorded (before the CLI reported it)')}</dd></dl></section>")
        body += '<p class="muted hint">The live release\'s annex.yaml as deployed. Secret and token values are never shown (they aren\'t here).</p>'
    body += str(v.cli_block(f"annex status {d['app']}", f"annex secret set {d['app']} <NAME>"))
    return shell(who, d, "config", body, "Config")
