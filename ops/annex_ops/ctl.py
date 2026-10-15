"""Admin commands, run with: docker exec annex-ops-1 python -m annex_ops.ctl <command> [--json]

Used by the annex CLI over SSH. Never prints tokens, hashes or message bodies (none are stored).
"""
import argparse
import json
import sys

from . import config, db, sends
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="annex_ops.ctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sends", help="recent send attempts, newest first")
    s.add_argument("--app")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    sub.add_parser("grants", help="what each app may do").add_argument("--json", action="store_true")
    sub.add_parser("info", help="how email and Slack are wired").add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "sends":
            data, cols = sends.list_sends(a.app, a.limit), ["ts", "app", "channel", "recipients", "subject", "status", "error"]
        elif a.cmd == "grants":
            data, cols = grants(), ["app", "email", "slack_channels", "updated_at"]
        else:
            data, cols = info(), []
    except OpsError as e:
        print(json.dumps({"error": e.message}) if a.json else f"error: {e.message}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(data))
    elif a.cmd == "info":
        print("\n".join(f"{k}: {v}" for k, v in data.items()))
    else:
        print(table(data, cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
