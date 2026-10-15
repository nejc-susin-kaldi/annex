"""Admin commands, run with: docker exec annex-ops-1 python -m annex_ops.ctl <command> [--json]

Used by the annex CLI over SSH. Never prints tokens, hashes or message bodies (none are stored).
"""
import argparse
import json
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db, digest, directory, hooks_store, incidents, sends, usage
from .errors import OpsError


def grants() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT app, email, slack_channels, updated_at FROM annex_ops.app_grants "
                            "ORDER BY app").fetchall()
    return [{"app": r[0], "email": r[1], "slack_channels": list(r[2]),
             "updated_at": r[3].isoformat(timespec="seconds")} for r in rows]


def info() -> dict:
    return {"email": f"smtp {config.SMTP_HOST}:{config.SMTP_PORT}", "slack_connected": config.slack_connected(),
            "domains": config.EMAIL_DOMAINS}


def table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    cell = lambda v: ", ".join(map(str, v)) if isinstance(v, list) else "" if v is None else str(v)  # noqa: E731
    widths = {c: max(len(c), *(len(cell(r[c])) for r in rows)) for c in cols}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in cols)]
    lines += ["  ".join(cell(r[c]).ljust(widths[c]) for c in cols) for r in rows]
    return "\n".join(lines)


def parse_since(text: str) -> float:
    """"30m", "1h", "2d" -> seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", (text or "").strip())
    if not m:
        raise OpsError(400, f"--since {text!r} is not a duration. Use e.g. 30m, 1h or 2d")
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def hooks_action(a) -> dict | list:
    if a.action == "show":
        if not (a.target or "").isdigit():
            raise OpsError(400, "usage: annex hooks show <id>")
        return hooks_store.show_event(int(a.target))
    if a.action == "replay":
        if not a.target:
            raise OpsError(400, "usage: annex hooks replay <app> --id N | --failed | --since 1h")
        ids = hooks_store.replay(a.target, a.id, a.failed, parse_since(a.since) if a.since else None)
        return {"app": a.target, "replayed": ids}
    return hooks_store.list_events(a.app or a.target, a.status, a.limit)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="annex_ops.ctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sends", help="recent send attempts, newest first")
    s.add_argument("--app")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    sub.add_parser("grants", help="what each app may do").add_argument("--json", action="store_true")
    sub.add_parser("info", help="how email and Slack are wired").add_argument("--json", action="store_true")
    s = sub.add_parser("app-status", help="health check, open incident and last job runs of an app")
    s.add_argument("app")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("incidents", help="incidents, newest first")
    s.add_argument("--open", action="store_true")
    s.add_argument("--app")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("maintenance", help="hold alerts for a subject (app:<name>, job:<app>/<job>, platform:<service>)")
    s.add_argument("subject")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--minutes", type=int)
    g.add_argument("--end", action="store_true")
    s.add_argument("--json", action="store_true")
    sub.add_parser("users", help="logins in the user directory").add_argument("--json", action="store_true")
    s = sub.add_parser("usage", help="usage per app (totals; --by-user adds logins, one app only)")
    s.add_argument("--app")
    s.add_argument("--by-user", action="store_true", help="per-login request counts for the last 30 days (admins only)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("hooks", help="incoming webhook events: list (metadata only), show <id> (body: admins), replay <app>")
    s.add_argument("action", nargs="?", default="list", choices=["list", "show", "replay"])
    s.add_argument("target", nargs="?", help="show: the event id; replay: the app")
    s.add_argument("--app")
    s.add_argument("--status", choices=["pending", "delivered", "failed", "rejected"])
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--id", type=int, help="replay: this event")
    s.add_argument("--failed", action="store_true", help="replay: every failed event of the app")
    s.add_argument("--since", help="replay: events received within this long, e.g. 30m, 1h, 2d")
    s.add_argument("--json", action="store_true")
    sub.add_parser("rollup", help="run the usage rollup now").add_argument("--json", action="store_true")
    s = sub.add_parser("digest", help="preview the weekly digest, or send it with --send")
    s.add_argument("--send", action="store_true")
    s.add_argument("--to", help="only this login's digest")
    s.add_argument("--dormant-days", type=int)
    s.add_argument("--min-age-days", type=int)
    s.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "sends":
            data, cols = sends.list_sends(a.app, a.limit), ["ts", "app", "channel", "recipients", "subject", "status", "error"]
        elif a.cmd == "grants":
            data, cols = grants(), ["app", "email", "slack_channels", "updated_at"]
        elif a.cmd == "app-status":
            data, cols = incidents.app_status(a.app), []
        elif a.cmd == "incidents":
            data = incidents.list_incidents(a.open, a.app, a.limit)
            cols = ["id", "subject", "opened_at", "resolved_at", "reminders", "signals"]
        elif a.cmd == "maintenance":
            if not re.match(r"^(app:[a-z][a-z0-9-]*|job:[a-z][a-z0-9-]*/[a-z][a-z0-9-]*|platform:[a-z][a-z0-9_-]*)$", a.subject):
                raise OpsError(400, f"{a.subject!r} is not a subject. Use app:<name>, job:<app>/<job> or platform:<service>")
            data, cols = (incidents.end_maintenance(a.subject) if a.end
                          else incidents.set_maintenance(a.subject, a.minutes)), []
        elif a.cmd == "usage":
            if a.by_user and not a.app:
                raise OpsError(400, "--by-user needs an app: annex usage <app> --by-user")
            data, cols = (usage.usage_app(a.app, a.by_user) if a.app else usage.usage_table()), []
        elif a.cmd == "hooks":
            data, cols = hooks_action(a), []
        elif a.cmd == "rollup":
            data, cols = usage.rollup(), []
        elif a.cmd == "digest":
            now = datetime.now(ZoneInfo(config.USAGE_TZ))
            fn = digest.send if a.send else digest.plan
            data, cols = fn(now, a.to, a.dormant_days, a.min_age_days), []
        elif a.cmd == "users":
            data, cols = sorted(directory.users()), []
        else:
            data, cols = info(), []
    except OpsError as e:
        print(json.dumps({"error": e.message}) if a.json else f"error: {e.message}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(data, default=str))
    elif isinstance(data, dict):
        print("\n".join(f"{k}: {v}" for k, v in data.items()))
    elif a.cmd == "users":
        print("\n".join(data))
    else:
        print(table(data, cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
