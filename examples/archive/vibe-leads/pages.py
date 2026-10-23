"""HTML for the lead form and the team page."""
from __future__ import annotations

from html import escape

from annex_runtime import User
from db import Member
from leads import CHANNELS, NET_WORTH, SERVICES

CSS = """
:root {
  --bg: #f6f5f2; --card: #ffffff; --text: #1d1d1b; --muted: #6b6a66; --line: #e3e1db;
  --accent: #2f5d50; --accent-text: #ffffff; --accent-soft: #e5efeb; --error: #b42318;
  --error-soft: #fdecea; --ok-soft: #e7f4ec; --ok: #1e6b3c; --input: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161716; --card: #1f201f; --text: #ecebe7; --muted: #a09f9a; --line: #353633;
    --accent: #7fb8a5; --accent-text: #0f1a16; --accent-soft: #24332d; --error: #f0857a;
    --error-soft: #3a1f1c; --ok-soft: #1c3326; --ok: #8fd3a9; --input: #181918;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { border-bottom: 1px solid var(--line); background: var(--card); }
.bar { max-width: 46rem; margin: 0 auto; padding: .75rem 1rem; display: flex; gap: 1.25rem;
  align-items: center; }
.brand { font-weight: 650; margin-right: auto; }
nav a { color: var(--muted); text-decoration: none; padding: .25rem 0; }
nav a.on { color: var(--text); border-bottom: 2px solid var(--accent); }
.who { color: var(--muted); font-size: .85rem; }
main { max-width: 46rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.lede { color: var(--muted); margin: 0 0 1.5rem; }
fieldset { border: 1px solid var(--line); border-radius: 10px; background: var(--card);
  padding: 1.25rem; margin: 0 0 1rem; }
legend { font-weight: 650; padding: 0 .4rem; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.full { grid-column: 1 / -1; }
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
.f { display: block; font-weight: 550; font-size: .9rem; margin-bottom: .3rem; }
.opt { color: var(--muted); font-weight: 400; }
input[type=text], input[type=email], input[type=tel], input[type=date], select, textarea {
  width: 100%; padding: .55rem .65rem; border: 1px solid var(--line); border-radius: 7px;
  background: var(--input); color: var(--text); font: inherit; }
textarea { min-height: 6rem; resize: vertical; }
input:focus, select:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.chips { display: flex; flex-wrap: wrap; gap: .4rem; }
.chips label { display: inline-flex; align-items: center; gap: .35rem; border: 1px solid var(--line);
  border-radius: 999px; padding: .3rem .75rem; cursor: pointer; font-size: .9rem; }
.chips input { accent-color: var(--accent); margin: 0; }
.chips label:has(input:checked) { background: var(--accent-soft); border-color: var(--accent); }
.chips label:has(input:disabled) { opacity: .5; cursor: not-allowed; }
.err { color: var(--error); font-size: .85rem; margin-top: .3rem; }
.hint { color: var(--muted); font-size: .85rem; margin-top: .3rem; }
.banner { border-radius: 10px; padding: .85rem 1rem; margin: 0 0 1.25rem; }
.banner.ok { background: var(--ok-soft); color: var(--ok); }
.banner.bad { background: var(--error-soft); color: var(--error); }
.banner a { color: inherit; }
.banner.test { background: var(--accent-soft); color: var(--text); }
.preview { margin: 0 0 1.5rem; }
.preview .banner { margin-bottom: .5rem; }
.msg { border: 1px solid var(--line); border-radius: 10px; background: var(--card); padding: 1rem 1.25rem; }
.msg pre { margin: .75rem 0 0; white-space: pre-wrap; font: inherit; }
button { font: inherit; cursor: pointer; border-radius: 8px; }
.primary { background: var(--accent); color: var(--accent-text); border: 0; padding: .7rem 1.4rem;
  font-weight: 600; }
.link { background: none; border: 0; color: var(--error); padding: 0; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { text-align: left; padding: .55rem .4rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 550; font-size: .82rem; }
.wrap { overflow-x: auto; }
code { font-size: .85em; }
"""


def layout(title: str, user: User, active: str, body: str) -> str:
    def tab(href: str, key: str, label: str) -> str:
        return f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · vibe-leads</title><style>{CSS}</style></head><body>
<header><div class="bar"><span class="brand">vibe-leads</span>
<nav>{tab("./", "lead", "New lead")} &nbsp; {tab("team", "team", "Team")}</nav>
<span class="who">{escape(user.name)}</span></div></header>
<main>{body}</main></body></html>"""


# --- small field helpers ---------------------------------------------------

def _err(errors: dict, key: str) -> str:
    return f'<div class="err">{escape(errors[key])}</div>' if key in errors else ""


def _text(name: str, label: str, values: dict, errors: dict, *, kind: str = "text",
          optional: bool = False, cls: str = "", hint: str = "") -> str:
    opt = ' <span class="opt">(optional)</span>' if optional else ""
    hint_html = f'<div class="hint">{escape(hint)}</div>' if hint else ""
    return f"""<div class="{cls}"><label class="f" for="{name}">{label}{opt}</label>
<input type="{kind}" id="{name}" name="{name}" value="{escape(values.get(name, ''))}">
{hint_html}{_err(errors, name)}</div>"""


def _select(name: str, label: str, options: list[tuple[str, str]], values: dict, errors: dict,
            *, cls: str = "", placeholder: str = "Choose…") -> str:
    current = str(values.get(name, ""))
    opts = "".join(
        f'<option value="{escape(v)}"{" selected" if v == current else ""}>{escape(t)}</option>'
        for v, t in options)
    return f"""<div class="{cls}"><label class="f" for="{name}">{label}</label>
<select id="{name}" name="{name}"><option value="">{placeholder}</option>{opts}</select>
{_err(errors, name)}</div>"""


def _chips(name: str, label: str, options: list[str], values: dict, errors: dict, *,
           multi: bool = False, cls: str = "full", disabled: set[str] = frozenset(),
           labels: dict[str, str] | None = None, hint: str = "") -> str:
    kind = "checkbox" if multi else "radio"
    chosen = values.get(name, [] if multi else "")
    chosen = set(chosen) if multi else {chosen}
    items = "".join(
        f'<label><input type="{kind}" name="{name}" value="{escape(o)}"'
        f'{" checked" if o in chosen and o not in disabled else ""}'
        f'{" disabled" if o in disabled else ""}>{escape((labels or {}).get(o, o))}</label>'
        for o in options)
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return f"""<div class="{cls}"><span class="f">{label}</span><div class="chips">{items}</div>{hint_html}{_err(errors, name)}</div>"""


# --- pages -----------------------------------------------------------------

def lead_form(user: User, members: list[Member], values: dict, errors: dict, *,
              channels_ready: dict[str, bool], sent: str = "", failure: str = "",
              preview: tuple[str, str, str] | None = None) -> str:
    test_mode = not any(channels_ready.values())
    banner = ""
    if test_mode:
        banner = ('<div class="banner test"><strong>Test mode.</strong> Slack and email aren\'t set up '
                  'yet, so nothing is sent. Fill in the form to preview the message the teammate '
                  'would get.</div>')
    if preview:
        to, subject, body = preview
        banner = f"""<div class="preview"><div class="banner test"><strong>Preview only, nothing was
sent.</strong> This is what {escape(to)} would get. Your answers are still below if you want to change
anything.</div><div class="msg"><div class="opt">To: {escape(to)}<br>Subject: {escape(subject)}</div>
<pre>{escape(body)}</pre></div></div>"""
    if sent:
        banner = f'<div class="banner ok">✓ {escape(sent)}</div>'
    if failure:
        banner = f'<div class="banner bad">{escape(failure)} Nothing was sent; your answers are still below.</div>'
    elif errors:
        banner = '<div class="banner bad">A few things need fixing before this can be sent. See the notes below.</div>'

    if not members:
        return layout("New lead", user, "lead", f"""{banner}
<h1>New lead</h1><p class="lede">Before you can hand out leads, add the people who can follow them up.</p>
<p><a href="team">Add teammates →</a></p>""")

    disabled = {c for c, ok in channels_ready.items() if not ok}
    channel_hint = ""
    if disabled:
        names = " and ".join(CHANNELS[c] for c in sorted(disabled))
        channel_hint = f"{names} isn't set up on this server yet. Ask the app owner to configure it."
    values = dict(values)
    if "channel" not in values:
        values["channel"] = next((c for c in CHANNELS if channels_ready[c]), "")

    people = [(str(m.id), f"{m.name}" + ("" if m.slack_id else " (no Slack ID)")) for m in members]

    return layout("New lead", user, "lead", f"""{banner}
<h1>New lead</h1>
<p class="lede">The basics we need to start a conversation with a potential client. Pick who should
follow up, and they'll get a summary with a request to send a quote within 5 working days.</p>
<form method="post" action="./" novalidate>
<fieldset><legend>The client</legend><div class="grid">
{_text("client_name", "Name", values, errors, cls="full")}
{_text("email", "Email", values, errors, kind="email", hint="An email, a phone number, or both.")}
{_text("phone", "Phone", values, errors, kind="tel")}
</div></fieldset>

<fieldset><legend>What they're looking for</legend><div class="grid">
{_chips("net_worth", "Approximate net worth", NET_WORTH, values, errors)}
{_chips("services", "Services they might be interested in", SERVICES, values, errors, multi=True,
        hint="Pick all that apply.")}
<div class="full"><label class="f" for="note">Note <span class="opt">(optional)</span></label>
<textarea id="note" name="note" placeholder="Anything useful for the first call: context, concerns, who else is involved…">{escape(values.get("note", ""))}</textarea></div>
</div></fieldset>

<fieldset><legend>Hand it over</legend><div class="grid">
{_select("assignee_id", "Who follows up", people, values, errors, placeholder="Choose a teammate…",
          cls="full")}
{_chips("channel", "Notify them via", list(CHANNELS), values, errors, disabled=disabled,
        labels=CHANNELS, hint=escape(channel_hint))}
</div></fieldset>

<button class="primary" type="submit">{"Preview lead" if test_mode else "Send lead"}</button>
</form>""")


def team_page(user: User, members: list[Member], values: dict, errors: dict, notice: str = "") -> str:
    banner = f'<div class="banner ok">{escape(notice)}</div>' if notice else ""
    if errors:
        banner = '<div class="banner bad">Couldn\'t add that person. See the notes below.</div>'

    if members:
        rows = "".join(f"""<tr><td>{escape(m.name)}</td><td>{escape(m.email)}</td>
<td>{f"<code>{escape(m.slack_id)}</code>" if m.slack_id else '<span class="opt">none</span>'}</td>
<td class="opt">{escape(m.added_by)}</td>
<td><form method="post" action="team/{m.id}/remove"
 data-confirm="Remove {escape(m.name)} from the team?" onsubmit="return confirm(this.dataset.confirm)">
<button class="link" type="submit">Remove</button></form></td></tr>""" for m in members)
        table = f"""<div class="wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Slack ID</th>
<th>Added by</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"""
    else:
        table = '<p class="opt">Nobody yet. Add the first person below.</p>'

    return layout("Team", user, "team", f"""{banner}
<h1>Team</h1>
<p class="lede">The people new leads can be assigned to.</p>
<fieldset><legend>People</legend>{table}</fieldset>
<form method="post" action="team" novalidate>
<fieldset><legend>Add someone</legend><div class="grid">
{_text("name", "Name", values, errors)}
{_text("email", "Email", values, errors, kind="email")}
{_text("slack_id", "Slack member ID", values, errors, optional=True, cls="full",
       hint="Used to @mention them in Slack, e.g. U04ABCD1234. In Slack: their profile → ⋮ → Copy member ID.")}
</div><p style="margin:1rem 0 0"><button class="primary" type="submit">Add to team</button></p>
</fieldset></form>""")
