"""The console's rules and pages (M2.4): who sees what, and server-rendered HTML. Pure: no database, no network.

Every page is read-only and says so, with the CLI command that would change what it shows. Everything that comes
from an app (a log line, an error message, a manifest value) is escaped. The identity comes from `Remote-User` and
`Remote-Groups`, which can only be set by Traefik's forward-auth: the console isn't on any network apps are on (D69).
"""
import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Ljubljana")


# --- who sees what ------------------------------------------------------------------------------------
@dataclass
class Who:
    login: str
    groups: list[str]

    @property
    def admin(self) -> bool:
        return "admins" in self.groups


def parse_who(headers) -> Who | None:
    """The signed-in user from the forward-auth headers, or None (no `Remote-User`: not through the front door)."""
    login = (headers.get("remote-user") or "").strip()
    if not login:
        return None
    return Who(login, [g.strip() for g in (headers.get("remote-groups") or "").split(",") if g.strip()])


def can_see_app(who: Who, owner: str | None) -> bool:
    """An app's page: its owner and admins. (`owner` in annex.yaml is a claim, not a verified identity: D58.)"""
    return who.admin or (bool(owner) and owner == who.login)


# --- html helpers --------------------------------------------------------------------------------------
class Raw(str):
    """Markup that is already safe."""


def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def cell(value) -> str:
    return value if isinstance(value, Raw) else e(value)


def code(text: str) -> Raw:
    return Raw(f"<code>{e(text)}</code>")


def link(href: str, text) -> Raw:
    return Raw(f'<a href="{e(href)}">{e(text)}</a>')


def when(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else ""


def table(headers: list[str], rows: list[list], empty: str = "none") -> Raw:
    if not rows:
        return Raw(f'<p class="muted">{e(empty)}</p>')
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in rows)
    return Raw(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def badge(state: str) -> Raw:
    cls = {"up": "good", "ok": "good", "live": "good", "active": "bad", "down": "bad", "failed": "bad", "error": "bad",
           "quiet": "muted", "unknown": "warn", "warn": "warn", "none": "muted"}.get(state, "muted")
    return Raw(f'<span class="badge {cls}">{e(state)}</span>')


def section(title: str, body, hint: str = "") -> Raw:
    return Raw(f'<section><h2>{e(title)}</h2>{body if isinstance(body, Raw) else e(body)}'
               + (f'<p class="muted hint">{hint}</p>' if hint else "") + "</section>")


STYLE = """
:root{--bg:#fff;--fg:#1f2933;--muted:#667;--line:#e4e7eb;--card:#f7f8fa;--good:#1a7f4b;--bad:#b42318;--warn:#a15c07;--link:#0b5cad}
@media (prefers-color-scheme: dark){:root{--bg:#14171a;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2b3138;--card:#1c2127;--good:#4cc38a;--bad:#ff8a80;--warn:#e5b567;--link:#7db7ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
header{border-bottom:1px solid var(--line);padding:.6rem 1rem;display:flex;gap:1.2rem;flex-wrap:wrap;align-items:center}
header b{font-size:1.05rem}header .who{margin-left:auto;color:var(--muted)}
main{max-width:70rem;margin:0 auto;padding:1rem}h1{font-size:1.4rem;margin:.2rem 0 .8rem}h2{font-size:1.1rem;margin:1.6rem 0 .5rem;border-bottom:1px solid var(--line);padding-bottom:.2rem}
a{color:var(--link)}code,pre{background:var(--card);border-radius:4px;font:13px ui-monospace,Menlo,monospace}code{padding:.05rem .3rem}pre{padding:.6rem;overflow:auto;white-space:pre-wrap;word-break:break-word}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:.35rem .6rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;white-space:nowrap}
.muted{color:var(--muted)}.hint{font-size:13px}.badge{padding:.05rem .45rem;border-radius:999px;border:1px solid currentColor;font-size:13px}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.box{background:var(--card);border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}
form.filters{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0}form.filters input,form.filters select{padding:.3rem .4rem;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}
.log{font:13px ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}.log .err{color:var(--bad)}.log .wrn{color:var(--warn)}
footer{max-width:70rem;margin:2rem auto;padding:0 1rem;color:var(--muted);font-size:13px}
"""


def page(title: str, who: Who, body: str, active: str = "") -> str:
    nav = "".join(f'<a href="{href}"{" style=\"font-weight:700\"" if name == active else ""}>{label}</a>'
                  for name, href, label in (("fleet", "/", "Apps"), ("status", "/status", "Status")))
    role = "admin" if who.admin else "signed in"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{e(title)} · Annex console</title><style>{STYLE}</style></head><body>"
            f'<header><b>Annex console</b>{nav}<span class="who">{e(who.login)} ({role})</span></header>'
            f"<main>{body}</main>"
            '<footer>Read-only: nothing here changes anything. Times are Europe/Ljubljana. An app\'s owner is what its '
            "annex.yaml says (a claim, not a verified identity yet), so who sees an app rests on that.</footer></body></html>")


def readonly_note(*commands: str) -> Raw:
    cmds = " ".join(f"<code>{e(c)}</code>" for c in commands)
    return Raw(f'<p class="box muted">Read-only. To change something use the CLI, e.g. {cmds}</p>')


# --- pages --------------------------------------------------------------------------------------------------
def forbidden(who: Who, what: str = "this page") -> str:
    return page("Not allowed", who, f"<h1>Not allowed</h1><p>You can't see {e(what)}: it is for its owner and the admins. "
                "<a href=\"/status\">Status</a> shows whether everything is up.</p>")


def not_found(who: Who, what: str) -> str:
    return page("Not found", who, f"<h1>Not found</h1><p>{e(what)}. <a href=\"/\">Back to the apps</a>.</p>")


def status_page(who: Who, apps: list[dict], platform: list[dict]) -> str:
    """Everyone signed in sees this: which apps and platform parts are up. Names and up/down only, like the discovery page."""
    bad = [a for a in apps + platform if a["state"] in ("down", "error")]
    head = ('<p class="box good"><b>Everything is up.</b></p>' if not bad else
            f'<p class="box bad"><b>{len(bad)} thing{"s" if len(bad) != 1 else ""} down.</b> The owners and admins have been told.</p>')
    body = (f"<h1>Status</h1>{head}"
            + section("Apps", table(["App", "State", "Detail"], [[a["app"], badge(a["state"]), a.get("detail", "")] for a in apps], "no apps"))
            + section("Platform", table(["Part", "State", "Detail"], [[p["name"], badge(p["state"]), p.get("detail", "")] for p in platform], "nothing to show")))
    return page("Status", who, body, "status")


def fleet_page(who: Who, rows: list[dict], platform_incidents: list[dict], host: dict | None) -> str:
    if not rows and not who.admin:
        body = ("<h1>Apps</h1><p>You don't own any apps, so there is nothing to show here. "
                '<a href="/status">Status</a> shows whether everything is up.</p>')
        return page("Apps", who, body, "fleet")
    trs = []
    for r in rows:
        flags = " ".join(str(badge(f)) for f in r["flags"])
        trs.append([link(f"/apps/{r['app']}", r["app"]), r["owner"] or "unknown", badge(r["health"]),
                    f"{r['users_7d']} {r['trend']}" if r["users_7d"] is not None else "", r["open_errors"] or "",
                    r["last_deploy"], Raw(flags), r["warnings"] or ""])
    body = ["<h1>" + ("All apps" if who.admin else "Your apps") + "</h1>",
            table(["App", "Owner", "Health", "Users 7d", "Open errors", "Last deploy", "Flags", "Warnings"], trs, "no apps")]
    if who.admin:
        body.append(section("Platform", table(["Subject", "Since", "What"], [[i["subject"], i["opened_at"], i["what"]] for i in platform_incidents],
                                             "no open platform incidents")))
        if host:
            body.append(section("Host", table(["Disk used", "Memory available"],
                                              [[f"{host['disk_used_pct']:.0f} %" if host.get("disk_used_pct") is not None else "?",
                                                f"{host['mem_available_mb']:.0f} MB" if host.get("mem_available_mb") is not None else "?"]]),
                                "The newest backup's age is an alert (platform:backup) when it is overdue; it isn't measured here."))
    body.append(readonly_note("annex list", "annex status <app>"))
    return page("Apps", who, "".join(body), "fleet")


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


def config_rows(m: dict) -> list[list]:
    j = lambda xs: ", ".join(map(str, xs)) if xs else "none"  # noqa: E731
    return [["type", m.get("type")], ["description", m.get("description") or ""], ["port / health check", f"{m.get('port')} {m.get('healthcheck') or ''}".strip()],
            ["storage", j([k for k in ("postgres", "files") if m.get(k)])], ["gateway views", j(m.get("views"))],
            ["schedule", j([f"{x['name']} ({x['cron']})" for x in m.get("schedule") or []])],
            ["memory", f"{m.get('memory_mb', 256)} MB"], ["email", "allowed" if m.get("notify_email") else "no"],
            ["Slack channels", j(m.get("notify_slack"))], ["alerts to", j(m.get("alerts_to") or ["owner"])],
            ["webhooks", j([f"{w['name']} ({w['verify']})" for w in m.get("webhooks") or []])],
            ["env (set in annex.yaml)", j([f"{k}={v}" for k, v in sorted((m.get("env") or {}).items())])],
            ["secrets (names only)", j(m.get("secrets"))], ["injected variables (names only)", j(injected_names(m))]]


def app_page(who: Who, d: dict) -> str:
    app = d["app"]
    o = [f"<h1>{e(app)}</h1>",
         f'<p class="muted">Owner: {e(d["owner"] or "unknown")} · live release: {e("r%s" % d["live"] if d["live"] else "none recorded")} · '
         f'<a href="/apps/{e(app)}/logs">Logs</a></p>']
    health = d["health"]
    inc = d["incidents"]
    status = [f"<p>Health check: {badge(health['state'])} {e(health.get('detail', ''))}</p>"]
    status.append(table(["Opened", "Subject", "Signals"], [[i["opened_at"], i["subject"], ", ".join(f"{k}: {v}" if v else k for k, v in i["signals"].items())] for i in inc],
                        "no open incident"))
    if d["jobs"]:
        status.append(table(["Job", "Last run", "Result", "Took", "Error"], [[j["job"], j["last_run"], badge("ok" if j["ok"] else "failed"),
                            f"{j['duration_ms']} ms" if j["duration_ms"] is not None else "", j["error"] or ""] for j in d["jobs"]]))
    o.append(section("Status", Raw("".join(map(str, status))), f"CLI: {e('annex status ' + app)}"))
    if d["warnings"]:
        o.append(section("Warnings", Raw("<ul>" + "".join(f"<li>{e(w)}</li>" for w in d["warnings"]) + "</ul>")))
    rel_rows = []
    for r in d["releases"]:
        what = link(f"/apps/{app}/logs?release={r['release']}", "logs")
        rel_rows.append([f"r{r['release']}", badge("live" if r["live"] else r["result"]), r["deployed_at"], r["deployed_by"] or "", r["via"] or "",
                         r["git"] or "", r["note"] or "", what])
    o.append(section("Releases", table(["Release", "Result", "When", "By", "Via", "Git", "Note", ""], rel_rows, "no release history (deployed before releases were recorded)"),
                     f"CLI: {e('annex releases ' + app)}, {e('annex rollback ' + app)}"))
    u = d["usage"]
    if u and u.get("app"):
        a = u["app"]
        us = [f"<p>{a['users_7d']} user(s) in the last 7 days ({e(a['trend'])}), {a['users_30d']} in 30 days, {a['requests_7d']} requests, "
              f"{a['errors_7d']} server errors, p95 {a['p95_7d'] if a['p95_7d'] is not None else '-'} ms. Signal: {e(a['signal'] or 'none')}.</p>",
              f"<p class=muted>Last 4 complete weeks (users): {' → '.join('-' if a[k] is None else str(a[k]) for k in ('week1', 'week2', 'week3', 'week4'))}</p>"]
        us.append(str(table(["Day", "Users", "Requests", "Errors", "p95 ms"], [[x["day"], x["users"], x["requests"], x["errors"], x["p95_ms"] if x["p95_ms"] is not None else ""] for x in u["days"][:14]],
                            "no daily numbers yet")))
        if who.admin and u.get("by_user") is not None:
            us.append("<p class=box>Per person (last 30 days). <b>Personal data: for admins, don't share it.</b></p>")
            us.append(str(table(["Login", "Requests"], [[x["login"], x["requests"]] for x in u["by_user"]], "nobody")))
        o.append(section("Usage", Raw("".join(us)), f"CLI: {e('annex usage ' + app)}"))
    else:
        o.append(section("Usage", Raw("<p class=muted>No usage recorded yet.</p>")))
    errs = d["errors"]
    o.append(section("Errors", table(["State", "Kind", "What", "Seen", "Last", "Release", "User", "Request"],
                                     [[badge(g["state"]), g["kind"], g["title"], g["count"], g["last_seen"][5:16].replace("T", " ") + "Z",
                                       f"r{g['first_release']}" if g["first_release"] == g["last_release"] else f"r{g['first_release']}–r{g['last_release']}",
                                       g["user"] or "", link(f"/apps/{app}/logs?request={g['request_id']}", g["request_id"]) if g["request_id"] else ""] for g in errs],
                                     "no errors seen"), f"A new kind of error emails the owner once. CLI: {e('annex errors ' + app)}"))
    o.append(section("Config", table(["", ""], [[a, b] for a, b in config_rows(d["manifest"])] if d["manifest"] else [],
                                     "no manifest recorded"), "The live release's annex.yaml as deployed. Secret and token values are never shown (they aren't here)."))
    o.append(readonly_note(f"annex deploy <folder>", f"annex rollback {app}", f"annex run {app} <job>"))
    return page(app, who, "".join(map(str, o)), "fleet")


def log_line(r: dict) -> str:
    cls = " err" if r["level"] == "error" else " wrn" if r["level"] == "warn" else ""
    fields = r.get("fields") or {}
    extra = " ".join(f"{k}={v if isinstance(v, str) and v.replace('.', '').replace('_', '').replace('-', '').isalnum() else _j(v)}"
                     for k, v in fields.items() if k != "stack")
    tag = f"[{r['container']}] " if r["container"] == "gateway" or r["container"].startswith("job:") else ""
    rel = f"r{r['release']} " if r["release"] is not None else ""
    rid = f" request_id={r['request_id']}" if r["request_id"] else ""
    text = f"{r['ts'][:23]}Z {rel}{tag}{(r['level'] or '').ljust(5)} {r['msg']}{rid}{('  ' + extra) if extra else ''}"
    return f'<div class="log{cls}">{e(text)}</div>'


def _j(v) -> str:
    import json
    return json.dumps(v, ensure_ascii=False, default=str)


def logs_page(who: Who, app: str, rows: list[dict], f: dict, older: str | None) -> str:
    sel = lambda cur, opts: "".join(f'<option value="{o}"{" selected" if o == cur else ""}>{o or "any"}</option>' for o in opts)  # noqa: E731
    form = (f'<form class="filters" method="get" action="/apps/{e(app)}/logs">'
            f'<select name="level">{sel(f.get("level") or "", ["", "debug", "info", "warn", "error"])}</select>'
            f'<input name="release" placeholder="release, e.g. r4" value="{e(f.get("release") or "")}" size="12">'
            f'<input name="grep" placeholder="text" value="{e(f.get("grep") or "")}">'
            f'<input name="request" placeholder="request id" value="{e(f.get("request") or "")}" size="18">'
            f'<select name="since">{sel(f.get("since") or "", ["", "1h", "6h", "1d", "7d"])}</select>'
            f'<select name="source">{sel(f.get("source") or "app", ["app", "gateway", "all"])}</select><button>Search</button></form>')
    body = [f'<h1>{e(app)} · logs</h1><p class="muted"><a href="/apps/{e(app)}">Back to {e(app)}</a> · stored for 14 days, also after a release was rolled back or a container replaced</p>', form]
    body.append("".join(log_line(r) for r in rows) if rows else '<p class="muted">no lines match</p>')
    if older:
        body.append(f'<p><a href="{e(older)}">Older lines</a></p>')
    cmd = "annex logs " + app + "".join(f" --{k} {v}" for k, v in f.items() if v and k not in ("source",) and not (k == "since" and not v))
    body.append(readonly_note(cmd))
    return page(f"{app} logs", who, "".join(body), "fleet")
