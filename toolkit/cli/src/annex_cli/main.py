"""annex command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import ops
from .manifest import ManifestError
from .remote import AnnexError


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c) or "")) for r in rows)) for c in cols}
    line = lambda vals: "  ".join(str(v or "").ljust(widths[c]) for c, v in zip(cols, vals))  # noqa: E731
    return "\n".join([line([c.upper() for c in cols])] + [line([r.get(c) for c in cols]) for r in rows])


def _health_line(h: dict) -> str:
    if h["state"] == "none":
        return "no check (job-only app; its jobs are watched instead)"
    when = f", checked {h['checked_at'][11:19]} UTC" if h.get("checked_at") else ""
    return f"{h['state']}{when}" + (f" ({h['detail']})" if h.get("detail") else "")


def _incident_line(i: dict | None) -> str:
    if not i:
        return "none"
    return f"OPEN since {i['opened_at'].replace('T', ' ')}: " + "; ".join(f"{k}: {v}" for k, v in i["signals"].items())


def _job_line(j: dict) -> str:
    if not j.get("last_run"):
        return f"{j['cron']}; no run seen yet"
    state = "FAILING" if j.get("failing") else ("ok" if j["ok"] else "failed")
    return (f"{j['cron']}; last run {j['last_run'].replace('T', ' ')} {state}, {j['duration_ms']} ms"
            + (f" ({j['error']})" if j.get("error") else ""))


def secret_pairs(args: list[str], from_file: str | None, stdin) -> tuple[list[str], list[str]]:
    """(KEY=VALUE pairs, warnings) for `secret set`. A bare KEY takes its value from --from-file or stdin, so it
    stays out of shell history and the process list; KEY=VALUE still works but warns (D85)."""
    bare = [x for x in args if "=" not in x]
    pairs = [x for x in args if "=" in x]
    warnings = []
    if pairs:
        warnings.append(f"{', '.join(p.partition('=')[0] for p in pairs)}: the value is now in your shell history and the process "
                        "list. Prefer `annex secret set <app> KEY` and type or pipe the value on stdin (or use --from-file PATH)")
    if from_file and not bare:
        raise AnnexError("--from-file needs a bare KEY to put the file's content in: annex secret set <app> KEY --from-file PATH")
    if len(bare) > 1:
        raise AnnexError(f"{', '.join(bare)}: only one KEY at a time can take its value from stdin or --from-file; "
                         "run the command once per key")
    if bare:
        if from_file:
            try:
                value = Path(from_file).read_text()
            except OSError as e:
                raise AnnexError(f"cannot read {from_file}: {e.strerror or e}") from e
        elif stdin.isatty():
            import getpass
            value = getpass.getpass(f"Value for {bare[0]} (hidden): ")
        else:
            value = stdin.read()
        value = value[:-1] if value.endswith("\n") else value
        if value == "" or "\n" in value or "\r" in value:
            raise AnnexError(f"the value for {bare[0]} must be one non-empty line (got "
                             f"{'nothing' if value == '' else 'several lines'}). Pipe it in, e.g. "
                             f"`printf %s \"$VALUE\" | annex secret set <app> {bare[0]}`")
        pairs.append(f"{bare[0]}={value}")
    return pairs, warnings


def _users(a: argparse.Namespace, emit) -> None:
    c = a.users_cmd
    if c == "list":
        res = ops.users("list")
        if emit:
            return emit(res["users"])
        rows = [{**u, "groups": ",".join(u["groups"]), "disabled": "disabled" if u["disabled"] else ""}
                for u in res["users"]]
        return print(_table(rows, ["login", "name", "email", "groups", "disabled"]))
    if c == "devices":
        res = ops.users("devices", a.login)
        if emit:
            return emit(res["devices"])
        if not res["devices"]:
            return print("no devices on the tailnet" + (f" for {a.login}" if a.login else "")
                         + ". Send a key with: annex users device-invite <login>")
        rows = [{**d, "online": "online" if d["online"] else "", "last_seen": d["last_seen"] or "",
                 "expires": d["expires"] or "never", "tags": ",".join(d["tags"])} for d in res["devices"]]
        return print(_table(rows, ["id", "login", "device", "ip", "online", "last_seen", "expires", "tags"]))
    if c == "remove" and not a.yes:
        if input(f"Remove the account {a.login}? They can't sign in any more. Type the login to confirm: ") != a.login:
            sys.exit("Aborted.")
    opts = {k: getattr(a, k) for k in ("name", "email", "groups", "print_password", "demo", "disable", "enable",
                                       "hours", "print_key", "device") if hasattr(a, k)}
    res = ops.users(c, a.login, **opts)
    if emit:
        return emit(res)
    if c == "add":
        u = res["user"]
        print(f"added {u['login']} ({u['name']}, {u['email']}), groups: {', '.join(u['groups'])}")
        if res.get("password"):
            print(f"password (shown once; give it to them privately): {res['password']}")
        else:
            print(f"invitation sent to {res['invited']}: the link lets them set their own password (valid 24 hours)")
        if res.get("demo_password_stored"):
            print("the password is also in secrets/users.txt (scripts/show-users.sh); a push with --extras creates their Metabase account from it")
        else:
            print("Metabase has its own accounts (D9): add one there too if they need it.")
    elif c == "set":
        u = res["user"]
        print(f"{u['login']}: {u['name']}, {u['email']}, groups: {', '.join(u['groups'])}"
              + (", DISABLED" if u["disabled"] else ""))
    elif c == "remove":
        print(f"removed {res['removed']}. {res['note']}")
        if res.get("devices_removed"):
            print(f"their devices left the tailnet: {', '.join(res['devices_removed'])}")
    elif c == "device-invite":
        until = f" (works once, until {res['expires']})" if res.get("expires") else " (works once)"
        if res.get("key"):
            print(f"one-time key for one of {res['login']}'s devices{until}; give it to them privately:\n  {res['key']}\n"
                  f"they connect with: tailscale login --login-server {res['server']} --authkey <the key>")
        else:
            print(f"a one-time key for one device was emailed to {res['sent_to']}{until}, with how to connect")
    elif c == "device-remove":
        if res["removed"]:
            print(f"{res['login']}: removed " + ", ".join(f"{d['device']} ({d['id']})" for d in res["removed"]))
        else:
            print(f"{res['login']} had no devices on the tailnet")
    elif c == "reset":
        if res.get("password"):
            print(f"new password for {res['login']} (shown once): {res['password']}"
                  + ("\n(the demo password list, scripts/show-users.sh, is updated too)" if res.get("demo_password_updated") else ""))
        else:
            print(f"a reset link was emailed to {res['reset_sent_to']} (valid 24 hours)")
    if res.get("warning"):
        print(f"warning: {res['warning']}")


def _release(v: str) -> int:
    try:
        return int(v.lower().removeprefix("r"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a release; use e.g. r3") from None


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="annex", description="Deploy small internal tools to Annex.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("init", help="copy a template into a new folder")
    s.add_argument("template", help="python-web, python-job, ... (see toolkit/templates/)")
    s.add_argument("name")
    s.add_argument("--dir", type=Path, help="target folder (default ./<name>)")
    s.add_argument("--owner", default=os.environ.get("ANNEX_OWNER"), help="builder login (default $ANNEX_OWNER)")
    s = sub.add_parser("deploy", help="validate, provision, build, run and register an app")
    s.add_argument("path", nargs="?", default=".", type=Path)
    sub.add_parser("list", help="list deployed apps")
    s = sub.add_parser("status", help="show an app's state")
    s.add_argument("app")
    s = sub.add_parser("logs", help="show an app's logs (stored for 14 days: they outlive the container)")
    s.add_argument("app")
    s.add_argument("-n", "--tail", type=int, default=100, help="the last N matching lines")
    s.add_argument("-f", "--follow", action="store_true", help="follow the live container (no filters)")
    s.add_argument("--since", help="e.g. 10m, 2h, 7d")
    s.add_argument("--release", help="one release's lines, e.g. r4: also after it was rolled back")
    s.add_argument("--level", choices=["debug", "info", "warn", "error"], help="this level and above")
    s.add_argument("--grep", help="lines containing this text (message and fields, any case)")
    s.add_argument("--request", help="the lines of one request id")
    s.add_argument("--source", choices=["app", "gateway", "all"], default="app",
                   help="app: its own lines and its jobs' output (default); gateway: its data reads; all")
    s = sub.add_parser("errors", help="the kinds of error seen in logs (server, browser, jobs): active, or quiet since a fix")
    s.add_argument("app", nargs="?", help="one app (default: all)")
    s.add_argument("--state", choices=["active", "quiet"])
    s = sub.add_parser("audit", help="the audit trail: who deployed, what data was read, what was sent, who changed a secret or an account (admins)")
    s.add_argument("--app")
    s.add_argument("--since", help="e.g. 30m, 2h, 7d")
    s.add_argument("--action", help="deploy, rollback, secret, app, user, signin, gateway, email, slack, webhook or data (a prefix of the action)")
    s.add_argument("--actor")
    s.add_argument("-n", "--limit", type=int, default=100)
    s = sub.add_parser("usage", help="who uses what: users, requests, errors and response times per app")
    s.add_argument("app", nargs="?", help="one app, by day (default: every app)")
    s.add_argument("--by-user", action="store_true", help="per-person request counts (admins only; needs an app)")
    s = sub.add_parser("hooks", help="incoming webhooks: list an app's events, show one, replay (human only)")
    s.add_argument("args", nargs="+", metavar="app | show ID | replay APP",
                   help="annex hooks <app>; annex hooks show <id>; annex hooks replay <app> --id N|--failed|--since 1h")
    s.add_argument("--status", choices=["pending", "delivered", "failed", "rejected"])
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("--id", type=int, help="replay: this event")
    s.add_argument("--failed", action="store_true", help="replay: every failed event")
    s.add_argument("--since", help="replay: events received within this long (30m, 1h, 2d)")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation (replay)")
    s = sub.add_parser("sends", help="email and Slack send log (every attempt, no message bodies)")
    s.add_argument("app", nargs="?", help="only this app (default: all)")
    s.add_argument("-n", "--limit", type=int, default=50)
    s = sub.add_parser("releases", help="list an app's releases")
    s.add_argument("app")
    s = sub.add_parser("rollback", help="switch an app back to an earlier release")
    s.add_argument("app")
    s.add_argument("--to", type=_release, metavar="rN", help="release to switch to (default: the one before the current)")
    s.add_argument("--with-data", action="store_true",
                   help="also restore the database and /data from that release's time; loses writes since (human only)")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation (--with-data)")
    s = sub.add_parser("run", help="run one of an app's jobs now")
    s.add_argument("app")
    s.add_argument("job")
    s = sub.add_parser("secret", help="manage app secrets (human only)")
    ss = s.add_subparsers(dest="secret_cmd", required=True)
    s = ss.add_parser("set", help="store a secret for an app on the VM: KEY reads the value from stdin (or --from-file); "
                                  "KEY=VALUE works but leaves the value in shell history")
    s.add_argument("app")
    s.add_argument("pairs", nargs="+", metavar="KEY", help="KEY (value on stdin) or KEY=VALUE (warns)")
    s.add_argument("--from-file", metavar="PATH", help="read the value of a bare KEY from this file instead of stdin")
    s = sub.add_parser("remove", help="remove an app (human only)")
    s.add_argument("app")
    s.add_argument("--keep-data", action="store_true", help="keep its database, files and secrets")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s = sub.add_parser("users", help="Annex's own user accounts: list, add, set, remove, reset, and their devices on the tailnet (human only)")
    us = s.add_subparsers(dest="users_cmd", required=True)
    us.add_parser("list", help="every account, with groups and whether it's disabled")
    s = us.add_parser("add", help="create an account and email the person an invitation to set a password")
    s.add_argument("login")
    s.add_argument("--name", required=True, help="display name, e.g. 'Ana Kovač'")
    s.add_argument("--email", required=True)
    s.add_argument("--groups", default="users", help="comma-separated: users, builders, admins (default users)")
    s.add_argument("--print-password", action="store_true",
                   help="print a random password once instead of emailing an invitation")
    s.add_argument("--demo", action="store_true",
                   help="a demo account: also keep the password in secrets/users.txt for the test suites and the Metabase "
                        "setup (implies --print-password)")
    s = us.add_parser("set", help="change name, email or groups, or disable/enable an account")
    s.add_argument("login")
    s.add_argument("--name")
    s.add_argument("--email")
    s.add_argument("--groups", help="replaces the groups, comma-separated")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--disable", action="store_true", help="the person can't sign in any more (reversible)")
    g.add_argument("--enable", action="store_true")
    s = us.add_parser("remove", help="delete an account")
    s.add_argument("login")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s = us.add_parser("reset", help="email the person a link to set a new password (or --print-password)")
    s.add_argument("login")
    s.add_argument("--print-password", action="store_true")
    s.add_argument("--demo", action="store_true", help="print a new password and keep it in secrets/users.txt (adds the line if missing)")
    # employee devices on the tailnet (ANNEX_ACCESS=headscale, D120)
    s = us.add_parser("devices", help="devices on the tailnet: whose, online, last seen (ANNEX_ACCESS=headscale)")
    s.add_argument("login", nargs="?", help="only this person's")
    s = us.add_parser("device-invite", help="email the person a one-time key that connects one device to the tailnet")
    s.add_argument("login")
    s.add_argument("--hours", type=int, default=24, help="how long the key works, 1 to 168 (default 24)")
    s.add_argument("--print-key", action="store_true", help="print the key once instead of emailing it")
    s = us.add_parser("device-remove", help="take a device off the tailnet, or all of the person's devices")
    s.add_argument("login")
    s.add_argument("--device", type=int, help="the device's id from `annex users devices` (default: all of theirs)")
    s = sub.add_parser("connect", help="point this CLI and the MCP server at your Annex instance (human only), or show where they point")
    s.add_argument("ssh_host", nargs="?", help="the SSH alias of the Annex server (a Host in ~/.ssh/config), e.g. annex-kaldi")
    s.add_argument("--data-dir", default="/srv/annex", help="Annex's directory on the server (default /srv/annex)")
    s = sub.add_parser("contract", help="print the app contract: the core, or one topic (for agents)")
    s.add_argument("topic", nargs="?", help="notify, webhooks, logging, stack, or all")

    a = p.parse_args(argv)
    emit = (lambda obj: print(json.dumps(obj, indent=2, default=str))) if a.json else None
    progress = (lambda msg: print(f"==> {msg}", file=sys.stderr))
    try:
        if a.cmd == "connect":
            res = ops.connect(a.ssh_host, a.data_dir) if a.ssh_host else ops.connection()
            if emit:
                emit(res)
            elif a.ssh_host:
                print(f"Annex now points at {res['home']} (ssh {res['ssh_host']}, {res['data_dir']}). The annex CLI "
                      "and the annex MCP server use it from now on, until you run annex connect again.")
                for n in res["notes"]:
                    print(f"note: {n}")
            elif res["connected"]:
                print(f"Points at {res['home']} (ssh {res['ssh_host']}, {res['data_dir']}), since {res['connected_at']}.")
            else:
                print("Not pointed at an Annex instance yet: annex connect <ssh-host>")
            if not emit and res.get("annex_env", os.environ.get("ANNEX_ENV")):
                print(f"note: ANNEX_ENV={os.environ.get('ANNEX_ENV')} is set in this shell: commands here use that lane "
                      "of this checkout instead")
        elif a.cmd == "contract":
            print(ops.contract(a.topic), end="")
        elif a.cmd == "init":
            dest = ops.init(a.template, a.name, a.dir, a.owner)
            emit({"path": str(dest)}) if emit else print(
                f"Created {dest} from {a.template}. Edit the code, then: annex deploy {dest}")
        elif a.cmd == "deploy":
            res = ops.deploy(a.path, progress)
            emit(res) if emit else print(
                f"{res['name']} r{res['release']} is live" + (f" at {res['url']}" if res["url"] else " (no web page)")
                + f"\nimage {res['image']}\ninjected env: {', '.join(res['injected_env'])}"
                + "".join(f"\nwebhook {w['name']} ({w['verify']}): {w['url']}" for w in res.get("webhooks", []))
                + "".join(f"\nwarning: {w}" for w in res.get("warnings", [])))
        elif a.cmd == "list":
            apps = ops.list_apps()
            emit(apps) if emit else print(_table(apps, ["name", "owner", "type", "state", "health", "memory", "url"]))
        elif a.cmd == "status":
            st = ops.status(a.app)
            if emit:
                emit(st)
            else:
                logs = st.pop("recent_logs")
                jobs = st.pop("jobs", [])
                if "warnings" in st:
                    st["warnings"] = "; ".join(st["warnings"])
                for k, v in st.items():
                    if k == "schedule":
                        v = ", ".join(j["name"] + " (" + j["cron"] + ")" for j in v)
                    elif k == "health_check":
                        v = _health_line(v)
                    elif k == "incident":
                        v = _incident_line(v)
                    elif k == "webhooks":
                        v = "; ".join(f"{w['name']} ({w['verify']}) {w['url']}" for w in v)
                    elif k == "usage":
                        v = "not recorded" if not v else f"{v['users_7d']} user(s) in the last 7 days ({v['trend']}), {v['requests_7d']} requests"
                    print(f"{k:12} {v}")
                for j in jobs:
                    print(f"{'job ' + j['job']:12} {_job_line(j)}")
                print("recent logs:\n  " + "\n  ".join(logs))
        elif a.cmd == "logs":
            filters = dict(release=a.release, level=a.level, grep=a.grep, request=a.request)
            if a.follow:
                if any(filters.values()) or a.since or a.source != "app":
                    raise AnnexError("-f follows the live container's own log and can't be combined with --since, "
                                         "--release, --level, --grep, --request or --source. Drop -f to search the stored logs")
                sys.exit(ops.follow_logs(a.app, a.tail))
            if emit:
                emit(ops.stored_logs(a.app, a.tail, a.since, source=a.source, **filters))
            else:
                print(ops.logs(a.app, a.tail, a.since, source=a.source, **filters), end="")
        elif a.cmd == "errors":
            res = ops.errors(a.app, a.state)
            if emit:
                emit(res)
            else:
                for g in res:
                    g["seen"] = f"{g['count']}x, last {g['last_seen'][5:16].replace('T', ' ')}"
                    g["release"] = (f"r{g['first_release']}" if g["first_release"] == g["last_release"]
                                    else f"r{g['first_release']}-r{g['last_release']}")
                    g["count"] = str(g["count"])
                print(_table(res, ["app", "state", "kind", "title", "seen", "release", "user"]))
                for g in res[:3]:
                    print(f"  e.g. {g['sample']}" + (f"   (annex logs {g['app']} --request {g['request_id']})" if g["request_id"] else ""))
        elif a.cmd == "audit":
            res = ops.audit(a.app, a.since, a.action, a.actor, a.limit)
            if emit:
                emit(res)
            else:
                for r in res:
                    r["when"] = r["ts"][:19].replace("T", " ")
                    r["what"] = " ".join(f"{k}={v if isinstance(v, str) else json.dumps(v)}" for k, v in (r["details"] or {}).items())
                print(_table(res, ["when", "actor", "app", "action", "what"]))
                print("Admins only: the trail holds logins and the recipients of emails. Times are UTC.")
        elif a.cmd == "usage":
            res = ops.usage(a.app, a.by_user)
            if emit:
                emit(res)
            elif not a.app:
                for x in res:
                    x["users 7d"], x["requests 7d"] = str(x["users_7d"]), str(x["requests_7d"])
                    x["errors"] = f"{x['error_rate_7d'] * 100:.1f}%"
                    x["p95"] = f"{x['p95_7d']} ms" if x["p95_7d"] is not None else "-"
                print(_table(res, ["app", "users 7d", "trend", "requests 7d", "errors", "p95", "signal"]))
                print("Last 7 days. Per app by day: annex usage <app>")
            else:
                app = res["app"]
                if app:
                    print(f"{a.app}: {app['users_7d']} user(s) in the last 7 days ({app['trend']}), {app['users_30d']} in 30 days, "
                          f"{app['requests_7d']} requests, signal: {app['signal'] or 'none'}")
                elif res["removed"]:
                    print(f"{a.app} has been removed; its history is kept.")
                for d in res["days"]:
                    d["users"], d["requests"], d["errors"] = str(d["users"]), str(d["requests"]), str(d["errors"])
                    d["p95"] = f"{d['p95_ms']} ms" if d["p95_ms"] is not None else "-"
                    d["day"] = d["day"] + (" (synthetic)" if d["synthetic"] else "")
                print(_table(res["days"], ["day", "users", "requests", "errors", "p95"]))
                if a.by_user:
                    print("\nPer-person usage is personal data: for admins only, don't share it. Last 30 days:")
                    print(_table([{"login": u["login"], "requests": str(u["requests"])} for u in res["by_user"]], ["login", "requests"]))
        elif a.cmd == "hooks":
            first = a.args[0]
            if first == "show":
                if len(a.args) != 2 or not a.args[1].isdigit():
                    sys.exit("error: usage: annex hooks show <id>")
                ev = ops.hook_show(int(a.args[1]))
                if emit:
                    emit(ev)
                else:
                    print(f"event {ev['id']}: {ev['app']}/{ev['hook']} {ev['status']}, {ev['attempts']} attempt(s), "
                          f"received {ev['received_at'].replace('T', ' ')}" + (f", replay of {ev['replay_of']}" if ev["replay_of"] else "")
                          + (f"\nlast error: {ev['last_error']}" if ev["last_error"] else ""))
                    print(f"headers: {json.dumps(ev['headers'])}\nThe body can hold personal data: for admins only.\n"
                          f"body ({ev['body_bytes']} bytes{', truncated' if ev['truncated'] else ''}):\n{ev['body']}")
            elif first == "replay":
                if len(a.args) != 2:
                    sys.exit("error: usage: annex hooks replay <app> --id N | --failed | --since 1h")
                app = a.args[1]
                if not a.yes and input(f"The app will process the replayed events again. Replay for {app}? "
                                       "Type the app name to confirm: ") != app:
                    sys.exit("Aborted.")
                res = ops.hooks_replay(app, a.id, a.failed, a.since)
                emit(res) if emit else print(f"queued {len(res['replayed'])} event(s) for {app} (ids {', '.join(map(str, res['replayed']))}); "
                                             f"watch them with: annex hooks {app}")
            else:
                app = a.args[1] if first == "list" and len(a.args) > 1 else first
                rows = ops.hooks(app, a.status, a.limit)
                if emit:
                    emit(rows)
                else:
                    for x in rows:
                        x["time"] = x["received_at"][:19].replace("T", " ")
                        x["id"], x["tries"] = str(x["id"]), str(x["attempts"])
                        x["error"] = (x["last_error"] or "")[:70]
                        x["replay"] = f"of {x['replay_of']}" if x["replay_of"] else ""
                    print(_table(rows, ["id", "time", "hook", "status", "tries", "replay", "error"]))
                    print(f"Bodies are not shown here. One event: annex hooks show <id>. Again: annex hooks replay {app} --id <id>")
        elif a.cmd == "sends":
            rows = ops.sends(a.app, a.limit)
            if emit:
                emit(rows)
            else:
                for x in rows:
                    x["time"] = x["ts"][:19].replace("T", " ")
                    x["to"] = ", ".join(x["recipients"])
                    x["error"] = x["error"] if not x["error"] or len(x["error"]) <= 70 else x["error"][:69] + "…"
                print(_table(rows, ["time", "app", "channel", "to", "subject", "status", "error"] if not a.app
                             else ["time", "channel", "to", "subject", "status", "error"]))
        elif a.cmd == "releases":
            rels = ops.releases(a.app)
            if emit:
                emit(rels)
            else:
                for x in rels:
                    x["r"] = f"r{x['release']}" + (" *" if x["current"] else "")
                    x["when"] = x["deployed_at"][:16].replace("T", " ")
                    x["by"] = f"{x['deployed_by']} ({x['via']})"
                    x["note"] = x["note"] if len(x["note"]) <= 70 else x["note"][:69] + "…"
                    x["rollback"] = ("yes" + (", with data" if x["data_snapshot"] else "")) if x["can_roll_back_to"] else ""
                print(_table(rels, ["r", "result", "when", "by", "git", "rollback", "note"]))
                print("* = live now. Roll back with: annex rollback <app> [--to rN] [--with-data]")
        elif a.cmd == "rollback":
            if a.with_data and not a.yes:
                if input(f"Restore {a.app}'s database and /data from that release's time? Writes since then are "
                         "lost (a snapshot of the current data is taken first). Type the app name to confirm: ") != a.app:
                    sys.exit("Aborted.")
            res = ops.rollback(a.app, a.to, a.with_data, progress)
            emit(res) if emit else print(
                f"{a.app} rolled back from r{res['rolled_back_from']} to r{res['rolled_back_to']} "
                f"(now live as r{res['release']}, {res['image']})"
                + (", data restored" if res["with_data"] else "") + (f" at {res['url']}" if res["url"] else "")
                + "".join(f"\nwarning: {w}" for w in res.get("warnings", [])))
        elif a.cmd == "run":
            res = ops.run_job(a.app, a.job)
            emit(res) if emit else print(f"{res['output']}--\n{a.app}/{a.job} exited {res['exit_code']} in {res['seconds']} s")
            sys.exit(0 if res["exit_code"] == 0 else 1)
        elif a.cmd == "secret":
            pairs, warnings = secret_pairs(a.pairs, a.from_file, sys.stdin)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            res = ops.secret_set(a.app, pairs)
            emit(res) if emit else print(f"set {', '.join(res['set'])} for {a.app}: {res['applied']}"
                                         + (f"\nwarning: {res['warning']}" if res.get("warning") else ""))
        elif a.cmd == "users":
            _users(a, emit)
        elif a.cmd == "remove":
            if not a.yes:
                what = "container and image" if a.keep_data else "container, image, database, files and secrets"
                if input(f"Remove {a.app} ({what})? Type the app name to confirm: ") != a.app:
                    sys.exit("Aborted.")
            res = ops.remove(a.app, a.keep_data)
            emit(res) if emit else print(f"removed {a.app}: {', '.join(res['removed'])}")
    except ManifestError as e:
        sys.exit(f"error: {e}")
    except AnnexError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
