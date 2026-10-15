"""Who hears about an incident, and what the message says. Pure functions: no I/O, so they're unit-tested."""
from datetime import datetime

# the phrase for an app's headline, by the signal that opened the incident (a stopped container is down to users)
PHRASE = {"down": "is down", "stopped": "is down", "crash-loop": "is crash-looping", "unhealthy": "is unhealthy",
          "oom": "ran out of memory", "errors": "is returning server errors"}
WORD = {"down": "down", "stopped": "down", "crash-loop": "crash-looping", "unhealthy": "unhealthy",
        "oom": "out of memory", "errors": "returning errors", "job-failed": "failing"}
EXPLAIN = {"down": "the health check is failing", "stopped": "the container is not running",
           "crash-loop": "the app keeps exiting with an error", "unhealthy": "the container's health check fails",
           "oom": "the app used more memory than its limit", "errors": "a spike of server errors (5xx)",
           "job-failed": "a scheduled job exited with an error"}


def parse_subject(subject: str) -> tuple[str, str, str | None]:
    """(kind, app or service, job): app:x -> (app, x, None), job:x/j -> (job, x, j), platform:s -> (platform, s, None)."""
    kind, _, rest = subject.partition(":")
    if kind == "job":
        app, _, job = rest.partition("/")
        return "job", app, job
    return kind, rest, None


def duration(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m} min" if m else f"{h} h"


def headline(subject: str, first_signal: str) -> str:
    """The sentence after "[Annex] ", e.g. "client-lookup is down" or "platform: gateway stopped"."""
    kind, name, job = parse_subject(subject)
    if kind == "job":
        return f'{name}: job "{job}" failed'
    if kind == "platform":
        if name == "front-door":
            return "platform: the front door isn't answering"
        phrase = {"stopped": "stopped", "down": "is down"}.get(first_signal, PHRASE.get(first_signal, "has a problem"))
        return f"platform: {name} {phrase}"
    return f"{name} {PHRASE.get(first_signal, 'has a problem')}"


def subject_line(kind: str, subject: str, signals: dict, duration_s: float = 0) -> str:
    first = next(iter(signals), "down")
    k, name, job = parse_subject(subject)
    if kind == "open":
        return f"[Annex] {headline(subject, first)}"
    if kind == "remind":
        return f"[Annex] reminder: {headline(subject, first)}"
    # resolved
    if k == "job":
        return f'[Annex] resolved: {name} job "{job}" is passing again'
    word = WORD.get(first, "affected")
    who = f"platform {name}" if k == "platform" else name
    return f"[Annex] resolved: {who} is back ({word} {duration(duration_s)})"


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def body(kind: str, subject: str, signals: dict, opened_at: float, duration_s: float, notes: list[str],
         status_url: str, app_url: str = "") -> str:
    """The message text under the title (the title already says what happened). `app_url` isn't repeated here:
    the notice template adds it as the button."""
    k, name, job = parse_subject(subject)
    if kind == "resolve":
        lines = [f"The problem lasted {duration(duration_s)} (since {clock(opened_at)})."]
    elif kind == "remind":
        lines = [f"Still open since {clock(opened_at)} ({duration(duration_s)})."]
    else:
        lines = [f"Since {clock(opened_at)}."]
    if kind != "resolve" and signals:
        lines += ["", "What Annex sees:"]
        lines += [f"- {n}: {s.get('detail') or EXPLAIN.get(n, '')}" for n, s in signals.items()]
    if notes:
        lines += [""] + notes
    lines += [""]
    if status_url:
        lines.append(f"Status page: {status_url}")
    if k in ("app", "job"):
        lines.append(f"Logs: annex logs {name}")
    elif k == "platform" and name != "front-door":
        lines.append(f"Logs: ssh annex docker logs annex-{name}-1 (or Dozzle, for admins)")
    return "\n".join(lines).strip()


def recipients(kind: str, owner: str | None, alerts_to: str | None, users: dict, slack_connected: bool,
               platform_channel: str) -> tuple[list[str], list[str], list[str]]:
    """(email addresses, Slack channels, notes) for an incident.

    App and job problems go to the owner plus the logins and channels in `alerts-to`, and to every admin.
    Platform problems go to admins only. Slack: the alerts channel gets everything, and the app's own channels
    their app's alerts, but only when Slack is connected. An owner or login missing from the directory is
    skipped with a note, so admins at least hear about it."""
    emails: list[str] = []
    channels: list[str] = []
    notes: list[str] = []

    def add_login(login: str) -> None:
        user = users.get(login)
        if user and user.get("email"):
            if user["email"] not in emails:
                emails.append(user["email"])
        else:
            notes.append(f"Note: '{login}' isn't in the user directory, so they were not emailed. "
                         "Fix the owner in annex.yaml or add the user.")

    if kind != "platform":
        entries = [e.strip() for e in (alerts_to or "owner").split(",") if e.strip()] or ["owner"]
        for entry in entries:
            if entry == "owner":
                add_login(owner) if owner else notes.append("Note: this app has no owner label, so only admins were emailed.")
            elif entry.startswith("#"):
                if slack_connected and entry not in channels:
                    channels.append(entry)
            else:
                add_login(entry)
    for login, user in users.items():
        if "admins" in (user.get("groups") or []) and user.get("email") and user["email"] not in emails:
            emails.append(user["email"])
    if slack_connected and platform_channel and platform_channel not in channels:
        channels.insert(0, platform_channel)
    return emails, channels, notes
