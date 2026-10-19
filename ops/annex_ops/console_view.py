"""The console's rules and building blocks (M2.4, redone in M2.7): who sees what, and the HTML pieces every page is made of.
Pure: no database, no network, no clock of its own (`rel_time` takes `now`).

The pages themselves are in `console_pages_overview.py` (overview, apps, status), `console_pages_app.py` (an app's tabs) and
`console_pages_admin.py` (activity, platform). Every page is read-only and says so once, in a "From the CLI" block that names
the commands which would change what it shows. Everything that comes from an app (a log line, an error message, a manifest
value) is escaped. The identity comes from `Remote-User` and `Remote-Groups`, which can only be set by Traefik's
forward-auth: the console isn't on any network apps are on (D69). No JavaScript: the CSP forbids it, so charts are inline
SVG, filters are links and forms, and tabs are separate URLs.
"""
import html
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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


def raw(*parts) -> Raw:
    """Join pieces (Raw as they are, anything else escaped) into markup."""
    return Raw("".join(cell(p) for p in parts))


def code(text: str) -> Raw:
    return Raw(f"<code>{e(text)}</code>")


def link(href: str, text) -> Raw:
    return Raw(f'<a href="{e(href)}">{cell(text)}</a>')


def host_url(name: str) -> str | None:
    """The address of `name` on this server (an app, or a platform part such as `logs`), built the way Traefik's hosts are
    (`ANNEX_HOST_PATTERN`, `ANNEX_PORT_SUFFIX`); None if the console wasn't given the pattern."""
    pattern = os.environ.get("ANNEX_HOST_PATTERN", "")
    if "{name}" not in pattern or not name.replace("-", "").isalnum():
        return None
    return f"https://{pattern.replace('{name}', name)}{os.environ.get('ANNEX_PORT_SUFFIX', '')}"


def when(dt: datetime | None) -> str:
    """Absolute local time, minutes."""
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else ""


def to_dt(value) -> datetime | None:
    """A datetime, an ISO string (with `Z` or an offset) or epoch seconds, as an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def ago_text(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def rel_time(value, now: datetime | None = None) -> Raw:
    """"3 d ago" with the absolute local time in the title (hover); older than 60 days it is the date itself."""
    dt = to_dt(value)
    if dt is None:
        return Raw("")
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    text = ago_text(secs) if secs < 60 * 86400 else dt.astimezone(TZ).strftime("%Y-%m-%d")
    return Raw(f'<time datetime="{e(dt.isoformat())}" title="{e(when(dt))}">{e(text)}</time>')


# --- states: one scheme everywhere ---------------------------------------------------------------------
STATES = {"up": "ok", "ok": "ok", "live": "ok", "good": "ok",
          "unknown": "warn", "warn": "warn", "warning": "warn", "dormant": "warn",
          "down": "bad", "failed": "bad", "error": "bad", "active": "bad", "bad": "bad", "rejected": "bad",
          "quiet": "off", "none": "off", "off": "off", "superseded": "off", "graduation": "off"}


def state_class(word: str) -> str:
    return STATES.get(word, "off")


def state(word: str, label: str | None = None) -> Raw:
    """A dot and a word: ok (green), warn (amber), bad (red) or off (grey). The word is what the data says."""
    return Raw(f'<span class="st {state_class(word)}"><i aria-hidden="true"></i>{e(label if label is not None else word)}</span>')


def chip(text, kind: str = "off") -> Raw:
    return Raw(f'<span class="chip {e(kind)}">{e(text)}</span>')


def table(headers: list[str], rows: list[list], empty: str = "none", cls: str = "") -> Raw:
    """A table that stacks into labelled rows on a narrow screen (each cell carries its column's name)."""
    if not rows:
        return Raw(f'<p class="muted">{e(empty)}</p>')
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f'<td data-label="{e(h)}">{cell(c)}</td>' for h, c in zip(headers, r)) + "</tr>" for r in rows)
    return Raw(f'<div class="scroll"><table class="stack {e(cls)}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def section(title: str, body, hint: str = "", extra: str = "") -> Raw:
    return Raw(f'<section><h2>{e(title)}{extra}</h2>{cell(body)}' + (f'<p class="muted hint">{cell(hint)}</p>' if hint else "") + "</section>")


def card(title: str, body, cls: str = "") -> Raw:
    return Raw(f'<section class="card {e(cls)}"><h2>{e(title)}</h2>{cell(body)}</section>')


def kpi(label: str, value, note="", chart=None) -> Raw:
    """One number with a label; `chart` is a sparkline."""
    return Raw(f'<div class="kpi"><div class="k">{e(label)}</div><div class="n">{cell(value)}</div>'
               f'<div class="note muted">{cell(note)}{cell(chart) if chart else ""}</div></div>')


def kpis(*tiles: Raw) -> Raw:
    return Raw('<div class="kpis">' + "".join(tiles) + "</div>")


def chips(items: list[tuple[str, str, int | None]], current: str) -> Raw:
    """Filter links: (key, href, count). The current one is marked."""
    out = []
    for key, href, n in items:
        label = e(key) + (f' <span class="muted">{n}</span>' if n is not None else "")
        out.append(f'<a class="fc{" on" if key == current else ""}" href="{e(href)}"' + (' aria-current="true"' if key == current else "") + f">{label}</a>")
    return Raw('<nav class="filters-row" aria-label="Filter">' + "".join(out) + "</nav>")


def cli_block(*commands: str) -> Raw:
    """The one place a page says what changes things: nothing in the console does."""
    cmds = "".join(f"<div><code>{e(c)}</code></div>" for c in commands)
    return Raw(f'<details class="cli"><summary>From the CLI. This console is read-only.</summary>{cmds}</details>')


# --- charts: inline SVG, no script ---------------------------------------------------------------------
def _nums(values) -> list[float]:
    return [float(x or 0) for x in values]


def sparkline(values: list, label: str = "per day", w: int = 84, h: int = 22) -> Raw:
    """A line of the last values (oldest first). Flat when there is nothing to draw."""
    vs = _nums(values)
    desc = f"{label}: " + ", ".join(str(int(x)) if x == int(x) else str(x) for x in vs) if vs else f"{label}: no data"
    if len(vs) < 2:
        pts = f"0,{h - 2} {w},{h - 2}"
    else:
        top = max(vs) or 1.0
        step = w / (len(vs) - 1)
        pts = " ".join(f"{i * step:.1f},{h - 2 - (v / top) * (h - 4):.1f}" for i, v in enumerate(vs))
    return Raw(f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{e(desc)}"><title>{e(desc)}</title>'
               f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def bars(values: list, labels: list[str], marks: list | None = None, unit: str = "", w: int = 640, h: int = 120) -> Raw:
    """A bar chart (oldest first), each bar with its label and value on hover; `marks` (for example errors) are drawn in red on top."""
    vs = _nums(values)
    if not vs:
        return Raw('<p class="muted">no data yet</p>')
    ms = _nums(marks) if marks else [0.0] * len(vs)
    top = max(vs + ms) or 1.0
    pad_l, pad_b = 34, 16
    cw, ch = w - pad_l, h - pad_b
    bw = cw / len(vs)
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{e(unit)} per day, {len(vs)} days" preserveAspectRatio="none">',
           f'<text x="0" y="10" class="ax">{int(top)}</text><text x="0" y="{ch}" class="ax">0</text>',
           f'<line x1="{pad_l}" y1="{ch}" x2="{w}" y2="{ch}" class="axis"/>']
    for i, (v, m) in enumerate(zip(vs, ms)):
        x = pad_l + i * bw
        bh = (v / top) * (ch - 4)
        tip = f"{labels[i] if i < len(labels) else ''}: {int(v)} {unit}" + (f", {int(m)} errors" if marks else "")
        out.append(f'<g><title>{e(tip.strip())}</title><rect x="{x + 1:.1f}" y="{ch - bh:.1f}" width="{max(bw - 2, 1):.1f}" height="{max(bh, 0):.1f}" class="bar"/>')
        if m:
            mh = (m / top) * (ch - 4)
            out.append(f'<rect x="{x + 1:.1f}" y="{ch - mh:.1f}" width="{max(bw - 2, 1):.1f}" height="{max(mh, 0):.1f}" class="bar bad"/>')
        out.append("</g>")
    if labels:
        out.append(f'<text x="{pad_l}" y="{h - 3}" class="ax">{e(labels[0])}</text><text x="{w}" y="{h - 3}" class="ax" text-anchor="end">{e(labels[-1])}</text>')
    out.append("</svg>")
    return Raw("".join(out))


def meter(pct: float | None, warn_at: float, bad_at: float, label: str) -> Raw:
    """A horizontal bar for a percentage (disk used)."""
    if pct is None:
        return Raw('<span class="muted">not sampled</span>')
    kind = "bad" if pct >= bad_at else "warn" if pct >= warn_at else "ok"
    return Raw(f'<div class="meter {kind}" role="img" aria-label="{e(label)}: {pct:.0f} %"><span style="width:{min(max(pct, 0), 100):.0f}%"></span></div>')


# --- the shell -----------------------------------------------------------------------------------------
STYLE = """
:root{--bg:#fff;--fg:#1f2933;--muted:#5b6672;--line:#e4e7eb;--card:#f6f7f9;--ok:#1a7f4b;--warn:#a15c07;--bad:#b42318;--off:#7b8794;--link:#0b5cad;
--bad-bg:#fdecea;--warn-bg:#fff4e0;--ok-bg:#e8f6ee;--r:8px;--s1:.4rem;--s2:.8rem;--s3:1.4rem}
@media (prefers-color-scheme: dark){:root{--bg:#14171a;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2b3138;--card:#1c2127;--ok:#4cc38a;--warn:#e5b567;--bad:#ff8a80;--off:#8a95a1;--link:#7db7ff;
--bad-bg:#3a1f1d;--warn-bg:#33281a;--ok-bg:#16291f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;font-variant-numeric:tabular-nums}
header.top{border-bottom:1px solid var(--line);padding:.5rem 1rem;display:flex;gap:1.2rem;flex-wrap:wrap;align-items:center}
header.top b{font-size:1.05rem}header.top nav{display:flex;gap:.2rem;flex-wrap:wrap}
header.top nav a{padding:.35rem .7rem;border-radius:var(--r);text-decoration:none;color:var(--muted)}
header.top nav a[aria-current]{color:var(--fg);font-weight:600;background:var(--card)}
header.top .who{margin-left:auto;color:var(--muted);font-size:14px}
main{max-width:72rem;margin:0 auto;padding:1rem}h1{font-size:1.45rem;margin:.2rem 0 .6rem;display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}
h2{font-size:1.05rem;margin:0 0 .5rem}section{margin:1.4rem 0}
.crumbs{color:var(--muted);font-size:14px;margin:.2rem 0}.crumbs a{color:var(--muted)}
a{color:var(--link)}code,pre{background:var(--card);border-radius:4px;font:13px ui-monospace,Menlo,monospace}code{padding:.05rem .3rem}pre{padding:.6rem;overflow:auto;white-space:pre-wrap;word-break:break-word}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:.4rem .6rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;white-space:nowrap;font-size:13px}
.muted{color:var(--muted)}time{white-space:nowrap}.hint{font-size:13px}small,.sub{color:var(--muted);font-size:13px;display:block}
.st{display:inline-flex;align-items:center;gap:.4rem;white-space:nowrap}.st i{width:.62rem;height:.62rem;border-radius:50%;background:var(--off);flex:none}
.st.ok i{background:var(--ok)}.st.warn i{background:var(--warn)}.st.bad i{background:var(--bad)}
.chip{padding:.05rem .5rem;border-radius:999px;font-size:12px;background:var(--card);color:var(--muted);white-space:nowrap}
.chip.warn{background:var(--warn-bg);color:var(--warn)}.chip.bad{background:var(--bad-bg);color:var(--bad)}.chip.ok{background:var(--ok-bg);color:var(--ok)}
.card,.box{background:var(--card);border-radius:var(--r);padding:.8rem 1rem;margin:0}.card h2{font-size:.95rem;color:var(--muted);font-weight:600}
.attn{border-radius:var(--r);padding:.7rem 1rem;margin:.6rem 0 1rem;border:1px solid var(--line)}.attn.bad{background:var(--bad-bg);border-color:var(--bad)}.attn.warn{background:var(--warn-bg);border-color:var(--warn)}
.attn.ok{background:var(--ok-bg);border-color:var(--ok)}.attn h2{margin:0 0 .3rem}.attn ul,.card ul{margin:0;padding-left:0;list-style:none}.attn li{margin:.2rem 0}.attn li .st,.card li .st{margin-right:.5rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));gap:.7rem;margin:.6rem 0 1rem}
.kpi{background:var(--card);border-radius:var(--r);padding:.6rem .8rem}.kpi .k{color:var(--muted);font-size:13px}.kpi .n{font-size:1.6rem;font-weight:600;line-height:1.2}.kpi .note{font-size:13px;display:flex;gap:.5rem;align-items:center}
.cols{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:1rem;align-items:start}@media (max-width:56rem){.cols{grid-template-columns:minmax(0,1fr)}}
.spark{color:var(--link);vertical-align:middle}.chart{width:100%;height:auto;max-height:9rem}.chart .bar{fill:var(--link);opacity:.75}.chart .bar.bad{fill:var(--bad);opacity:1}
.chart .ax{fill:var(--muted);font-size:10px}.chart .axis{stroke:var(--line)}
.meter{background:var(--line);border-radius:99px;height:.6rem;overflow:hidden;min-width:6rem}.meter span{display:block;height:100%;background:var(--ok)}.meter.warn span{background:var(--warn)}.meter.bad span{background:var(--bad)}
.tabs{display:flex;gap:.2rem;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:.4rem 0 1rem}.tabs a{padding:.45rem .8rem;text-decoration:none;color:var(--muted);border-bottom:2px solid transparent}
.tabs a[aria-current]{color:var(--fg);font-weight:600;border-bottom-color:var(--link)}.tabs .n{background:var(--card);border-radius:999px;padding:0 .45rem;font-size:12px;margin-left:.25rem}
.filters-row{display:flex;gap:.4rem;flex-wrap:wrap;margin:.4rem 0 .8rem}.fc{padding:.2rem .7rem;border:1px solid var(--line);border-radius:999px;text-decoration:none;color:var(--fg);font-size:14px}.fc.on{background:var(--card);font-weight:600;border-color:var(--muted)}
form.filters{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0}form.filters input,form.filters select{padding:.3rem .4rem;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}
.log{font:13px ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;padding-left:.5rem;border-left:3px solid transparent}.log.err{color:var(--bad);border-left-color:var(--bad)}.log.wrn{color:var(--warn);border-left-color:var(--warn)}
details.cli{margin:1.6rem 0 .4rem;font-size:14px;color:var(--muted)}details.cli summary{cursor:pointer}details.cli div{margin:.25rem 0 0 1rem}
details.more summary{cursor:pointer;color:var(--link)}dl.kv{display:grid;grid-template-columns:minmax(8rem,14rem) 1fr;gap:.25rem .8rem;margin:0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;word-break:break-word}
.feed{list-style:none;margin:0;padding:0}.feed li{padding:.3rem 0;border-bottom:1px solid var(--line);font-size:14px}.feed li:last-child{border:0}
footer{max-width:72rem;margin:2rem auto;padding:0 1rem;color:var(--muted);font-size:13px}
@media (max-width:40rem){table.stack thead{display:none}table.stack tr{display:block;border-bottom:1px solid var(--line);padding:.35rem 0}
table.stack td{display:flex;gap:.6rem;justify-content:space-between;border:0;padding:.15rem .2rem;text-align:right}
table.stack td::before{content:attr(data-label);color:var(--muted);font-size:13px;text-align:left;flex:none}dl.kv{grid-template-columns:1fr}}
"""


def nav_links(who: Who) -> list[tuple[str, str, str]]:
    links = [("overview", "/", "Overview"), ("apps", "/apps", "Apps")]
    if who.admin:
        links += [("activity", "/audit", "Activity"), ("platform", "/platform", "Platform")]
    else:
        links.append(("status", "/status", "Status"))
    return links


def page(title: str, who: Who, body: str, active: str = "", crumbs: list[tuple[str, str]] | None = None) -> str:
    nav = "".join(f'<a href="{href}"' + (' aria-current="page"' if name == active else "") + f">{label}</a>" for name, href, label in nav_links(who))
    role = "admin" if who.admin else "signed in"
    trail = ""
    if crumbs:
        trail = '<p class="crumbs">' + " / ".join(f'<a href="{e(h)}">{e(t)}</a>' for h, t in crumbs) + "</p>"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{e(title)} · Annex console</title><style>{STYLE}</style></head><body>"
            f'<header class="top"><b>Annex console</b><nav aria-label="Console">{nav}</nav><span class="who">{e(who.login)} ({role})</span></header>'
            f"<main>{trail}{body}</main>"
            '<footer>Times are Europe/Ljubljana (log lines are UTC). An app\'s owner is what its annex.yaml says, a claim and not a verified '
            "identity yet, so who sees an app rests on that.</footer></body></html>")


def forbidden(who: Who, what: str = "this page") -> str:
    return page("Not allowed", who, f"<h1>Not allowed</h1><p>You can't see {e(what)}: it is for its owner and the admins. "
                '<a href="/status">Status</a> shows whether everything is up.</p>')


def not_found(who: Who, what: str) -> str:
    return page("Not found", who, f'<h1>Not found</h1><p>{e(what)}. <a href="/apps">Back to the apps</a>.</p>', "apps")
