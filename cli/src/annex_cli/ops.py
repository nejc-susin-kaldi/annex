"""Annex operations. Shared by the CLI and the MCP server.

Each function returns plain data (dicts, strings) and raises AnnexError or
ManifestError with a message that says what to do next.
"""
from __future__ import annotations

import getpass
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import logcheck, logview
from . import manifest as mf
from .remote import REPO, AnnexError, Remote

TEMPLATES = REPO / "templates"
RUNTIME = REPO / "runtime"  # the one source of annex_runtime.py/.js; scripts/sync-runtime.sh copies it
CONTRACT = Path(__file__).with_name("contract.md")
OFELIA = "annex-ofelia-1"
OPS_CONTAINER = "annex-ops-1"
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache"}

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


BAD_NAME = ("{what} {name!r} is not valid: use 2-32 lowercase letters, digits and '-', starting with a letter{extra}")


def _name(app: str, what: str = "App name") -> str:
    """An app name that is safe to put in a shell command. Every public function and every helper that builds a
    command from a name goes through here before it talks to the VM (D79)."""
    if not isinstance(app, str) or not mf.NAME_RE.fullmatch(app):  # fullmatch: '$' would let a trailing newline through
        raise AnnexError(BAD_NAME.format(what=what, name=app, extra=""))
    if app in mf.RESERVED_NAMES:
        raise AnnexError(f"{app!r} is reserved by the platform. Use a name that is not one of "
                         f"{', '.join(sorted(mf.RESERVED_NAMES))}")
    return app


def _job(job: str) -> str:
    if not isinstance(job, str) or not mf.NAME_RE.fullmatch(job):
        raise AnnexError(BAD_NAME.format(what="Job name", name=job, extra=""))
    return job


def container(app: str) -> str:
    return f"annex-app-{_name(app)}"


def project(app: str) -> str:
    return f"annex-app-{_name(app)}"


# Per-app networks (D88): an app is on its own internal network and nothing else apps are on; the platform
# containers it needs join that network at runtime (annex_vm.py networks-sync), not through this compose file.
def app_network(app: str) -> str:
    return f"annex-app-{_name(app)}"


def egress_network(app: str) -> str:
    """Only for an app that declares egress: a non-internal network with only that app on it."""
    return f"annex-egress-{_name(app)}"


def _compose_networks(app: str, egress: bool) -> tuple[list[str], dict]:
    """(the service's networks, the file's top-level networks): both external, created by networks-sync."""
    nets = {"app": {"external": True, "name": app_network(app)}}
    if egress:
        nets["egress"] = {"external": True, "name": egress_network(app)}
    return list(nets), nets


# --- contract and init -----------------------------------------------------
# The contract is one file (for people: docs/app-contract.md), served to agents in parts: the core every app needs,
# and topics fetched only when an app uses them, so a simple dashboard doesn't load the webhook rules (D74).
CONTRACT_TOPICS = {
    "notify": ("Email and Slack", "sending email or Slack messages (notify:)"),
    "webhooks": ("Webhooks", "receiving calls from Trello, Stripe, GitHub, Slack (webhooks:)"),
    "logging": ("Logging", "the log format in detail, request ids, error grouping, browser errors, searching logs"),
    "stack": ("Bring your own stack", "any language or framework without a template (Next.js, Streamlit, Django, Go...)"),
}


def contract_version() -> int:
    """`ANNEX_CONTRACT_VERSION: n` at the top of contract.md (C4); 0 if the line is missing."""
    m = re.search(r"^ANNEX_CONTRACT_VERSION:\s*(\d+)\s*$", CONTRACT.read_text(), re.M)
    return int(m.group(1)) if m else 0


def _checkout(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def cli_version() -> dict:
    """What this CLI is, for the VM to judge (D103): the commit of the checkout it runs from ("unknown" when that
    isn't a repository), whether the tree has uncommitted changes, and the contract version it ships."""
    commit = _checkout("rev-parse", "HEAD") or "unknown"
    dirty = commit != "unknown" and bool(_checkout("status", "--porcelain"))
    return {"commit": commit, "dirty": dirty, "contract_version": contract_version()}


def cli_label(cli: dict | None) -> str | None:
    """A release's `cli` (commit, dirty) as one short text: 3f2a9c1d4b7e, +dirty when it had uncommitted changes."""
    if not isinstance(cli, dict) or not cli.get("commit"):
        return None
    return cli["commit"][:12] + ("+dirty" if cli.get("dirty") else "")


def runtime_version(text: str | None) -> int:
    """The `__version__` of a copy of annex_runtime.py / .js; 0 for a copy from before versions (C4)."""
    m = re.search(r"^(?:export const )?__version__\s*=\s*(\d+)", text or "", re.M)
    return int(m.group(1)) if m else 0


def runtime_message(name: str, mine: int, current: int, same_bytes: bool) -> str | None:
    """The deploy warning for one runtime copy, or None when it is the current one. `same_bytes` is whether the
    uploaded file equals the platform's (a copy of the current version that was edited still gets a note)."""
    if same_bytes:
        return None
    mv = f"v{mine} (before versions)" if mine == 0 else f"v{mine}"
    if mine < current:
        return (f"{name}: your runtime is {mv}, the current one is v{current}, so some helpers may be missing or "
                f"outdated. Replace it with the copy from a new `annex init` folder (templates are kept up to date)")
    if mine > current:
        return f"{name}: your runtime is {mv}, newer than the platform's v{current}; check you are using the right Annex"
    return (f"{name}: your runtime is v{mine}, the current version, but the file differs from the platform's copy. "
            f"Unless you changed it on purpose, replace it with the copy from a new `annex init` folder")


def runtime_versions(folder: Path | None) -> dict[str, int]:
    """The runtime version of each helper the app folder carries, e.g. {"annex_runtime.py": 1}."""
    out = {}
    for name in ("annex_runtime.py", "annex_runtime.js"):
        if folder and (folder / name).is_file():
            out[name] = runtime_version((folder / name).read_text(errors="replace"))
    return out


def _contract_sections() -> tuple[str, list[tuple[str, str]]]:
    """(intro, [(heading, text)]) split on the '## ' headings."""
    parts = CONTRACT.read_text().split("\n## ")
    return parts[0], [(p.split("\n", 1)[0].strip(), "## " + p) for p in parts[1:]]


def contract(topic: str | None = None) -> str:
    """The core of the contract (no topic), one topic, or "all"."""
    intro, sections = _contract_sections()
    if topic == "all":
        return CONTRACT.read_text()
    if topic:
        if topic not in CONTRACT_TOPICS:
            raise AnnexError(f"No contract topic {topic!r}. Topics: {', '.join(CONTRACT_TOPICS)}, or all")
        heading = CONTRACT_TOPICS[topic][0]
        return next(text for h, text in sections if h == heading).rstrip() + "\n"
    topic_headings = {h for h, _ in CONTRACT_TOPICS.values()}
    core = [text.rstrip() for h, text in sections if h not in topic_headings]
    more = "\n".join(f"- `annex contract {t}` (MCP: `annex_contract(topic=\"{t}\")`): {what}"
                      for t, (_, what) in CONTRACT_TOPICS.items())
    return "\n\n".join([intro.rstrip(), *core, "## More, only when the app needs it\n" + more]) + "\n"


def templates() -> list[str]:
    return sorted(p.name for p in TEMPLATES.iterdir() if (p / "annex.yaml").exists())


def init(template: str, name: str, dest: Path | None = None, owner: str | None = None) -> Path:
    if template not in templates():
        raise AnnexError(f"Unknown template {template!r}. Available: {', '.join(templates())}")
    if not mf.NAME_RE.match(name) or name in mf.RESERVED_NAMES:
        raise AnnexError(f"{name!r} is not a valid app name: use 2-32 lowercase letters, digits and '-', "
                         f"starting with a letter; not one of {', '.join(sorted(mf.RESERVED_NAMES))}")
    dest = (dest or Path.cwd() / name).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise AnnexError(f"{dest} exists and is not empty. Pick another folder or name")
    shutil.copytree(TEMPLATES / template, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*SKIP))
    for f in dest.rglob("*"):
        if f.is_file():
            try:
                text = f.read_text()
            except UnicodeDecodeError:
                continue
            new = text.replace("__APP_NAME__", name).replace("__OWNER__", owner or "CHANGE-ME")
            if new != text:
                f.write_text(new)
    return dest


# --- deploy ----------------------------------------------------------------
def _tar(folder: Path) -> bytes:
    buf = io.BytesIO()

    def keep(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if any(part in SKIP for part in Path(ti.name).parts):
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(folder, arcname=".", filter=keep)
    if buf.tell() > 50 * 1024 * 1024:
        raise AnnexError("App folder is over 50 MB compressed. Add build output and data files to .dockerignore "
                         "or move them out of the folder")
    return buf.getvalue()


def _network_args(app: str, *, web: bool, postgres: bool, views: bool, egress: bool) -> list[str]:
    """Arguments for `annex_vm.py networks-sync`: which platform containers join the app's network."""
    return ["networks-sync", app, *(["--web"] if web else []), *(["--postgres"] if postgres else []),
            *(["--views"] if views else []), *(["--egress"] if egress else [])]


def rewire_compose(doc: dict, app: str, egress: bool) -> tuple[dict, dict]:
    """An app's compose file from before D88 (on annex-edge, and annex-appdata with Postgres) moved onto its own
    network, for an app whose source isn't at hand (migrate_networks). Returns (new document, what the app uses:
    the flags for networks-sync), read from the file itself: routed = web, on appdata = Postgres, a gateway URL = views."""
    doc = json.loads(json.dumps(doc))  # a deep copy; the file is plain YAML data
    services = doc.get("services") or {}
    if len(services) != 1:
        raise AnnexError(f"{app}'s compose file has {len(services)} services; an Annex app has exactly one")
    svc = next(iter(services.values()))
    labels = svc.get("labels") or {}
    if not isinstance(labels, dict):  # Annex always writes a mapping; a list means someone edited the file
        raise AnnexError(f"{app}'s compose file has its labels as a list; it wasn't written by Annex. Redeploy it from its source")
    env = svc.get("environment") or {}
    old = svc.get("networks") or []
    old = list(old) if isinstance(old, (list, dict)) else []
    old_names = {(doc.get("networks") or {}).get(k, {}).get("name", k) for k in old}
    uses = {"web": str(labels.get("traefik.enable", "")).lower() == "true",
            "postgres": bool(old_names & {"annex-appdata"}),
            "views": "ANNEX_GATEWAY_URL" in (env if isinstance(env, dict) else {}),
            "egress": egress}
    if uses["web"]:
        labels["traefik.docker.network"] = app_network(app)
        svc["labels"] = labels
    svc["networks"], doc["networks"] = _compose_networks(app, egress)
    return doc, uses


def _record(r: Remote, app: str) -> dict | None:
    res = r.run(f"cat {r.path('deploy', _name(app), 'deploy.json')}", check=False)
    return json.loads(res.stdout) if res.returncode == 0 else None


def _now() -> str:
    # milliseconds: since D92 a deploy is stamped when it begins, often in the same second as the step before it
    # (a `secret set`), and the audit trail orders by this time
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _who() -> str:
    return os.environ.get("ANNEX_USER") or f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"


def _git(folder: Path) -> dict | None:
    """The folder's git commit, if it's in a repository (recorded with the release)."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    sha = git("rev-parse", "--short=12", "HEAD")
    if sha.returncode != 0 or not git("ls-files", "--", ".").stdout.strip():
        return None  # not a repository, or the folder isn't tracked in it
    return {"sha": sha.stdout.strip(), "dirty": bool(git("status", "--porcelain", "--", ".").stdout.strip())}


# --- releases --------------------------------------------------------------
# Each deploy or rollback has one line in /srv/annex/apps/<app>/releases.jsonl: release number, image, manifest,
# who/when/where from, data snapshot and result: "pending" while it runs, then "live", or "failed" when it didn't pass
# the health gate or broke off. Only the VM writes it: the deploy process that holds the app's lock, or the next one
# (or deploy-reconcile) when that process died (C1, D92, D101).
def _releases(r: Remote, app: str) -> list[dict]:
    res = r.run(f"cat {r.path('apps', _name(app), 'releases.jsonl')}", check=False)
    if res.returncode != 0:
        return []
    return [json.loads(line) for line in res.stdout.decode().splitlines() if line.strip()]


def _live(releases: list[dict]) -> dict | None:
    """The release that's running now: the last one that went live."""
    live = [x for x in releases if x["result"] == "live"]
    return live[-1] if live else None


def _images(r: Remote, app: str) -> set[str]:
    return set(r.out(f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' annex-app/{_name(app)}").split())


def _provision_args(app: str, *, postgres: bool, files: bool, views: list[str], secrets: list[str],
                    notify_email: bool = False, notify_slack: list[str] | None = None,
                    webhooks: list[dict] | None = None, port: int | None = None) -> list[str]:
    """Arguments for `annex_vm.py provision`. Records written before M1.2 have no notify keys, and before M1.5
    no webhooks: no grant, no hooks file."""
    return [app, *(["--postgres"] if postgres else []), *(["--files"] if files else []),
            "--views", ",".join(views), "--secrets", ",".join(secrets),
            *(["--notify-email"] if notify_email else []),
            *(["--slack", ",".join(notify_slack)] if notify_slack else []),
            *(["--hooks", json.dumps(webhooks), "--hook-port", str(port or 0)] if webhooks else [])]


def _webhook_urls(r: Remote, app: str, webhooks) -> list[dict]:
    """The public URL of each declared webhook (what the provider is told), for deploy and status output."""
    base = r.settings.get("ANNEX_URL_HOOKS", "")
    return [{"name": w["name"], "verify": w["verify"], "url": f"{base}/h/{app}/{w['name']}"}
            for w in (x if isinstance(x, dict) else x.__dict__ for x in webhooks)]


def _drain(r: Remote, app: str, progress: Progress = _quiet) -> None:
    """Before Docker replaces or removes an app's container, have annex-ops store the lines it hasn't read yet
    (the container's log goes with it). Never blocks the operation: annex-ops being down, slow or confused costs
    a warning and nothing else. Lines written while the old container shuts down can still be missed."""
    why = None
    try:
        res = r.run(f"timeout 10 docker exec {OPS_CONTAINER} python -m annex_ops.ctl logs-drain {container(app)} "
                    f"--timeout 5 --json", check=False, timeout=30)
        if res.returncode != 0:
            why = "annex-ops didn't answer"
        else:
            answer = json.loads(res.stdout.decode().strip().splitlines()[-1])
            if answer.get("timed_out") or not answer.get("stored", True):
                why = "annex-ops was too slow or its database is down"
    except (AnnexError, subprocess.SubprocessError, OSError, ValueError, IndexError):
        why = "annex-ops didn't answer"
    if why:
        progress(f"Warning: couldn't store {container(app)}'s last log lines before replacing it ({why}); "
                 f"the deploy carries on. Check: {r.hint('docker logs ' + OPS_CONTAINER)}")


# Settings a test sets in the CLI's environment for one deploy; the VM honours exactly these (vm/annex_deploy.py).
DEPLOY_OVERRIDES = ("ANNEX_BUILD_MIN_FREE_GB", "ANNEX_IMAGE_MAX_MB", "ANNEX_HEALTH_TIMEOUT")


def deploy(folder: Path, progress: Progress = _quiet, via: str = "cli") -> dict:
    """Validate here (fast, agent-readable errors), then one call: the VM runs the whole deploy (D101) and streams
    its progress; it carries on if this process or its connection goes away."""
    folder = Path(folder).resolve()
    m = mf.load(folder)
    r = Remote()
    progress(f"Validated {m.name} ({m.type}) owned by {m.owner}")

    allow = r.vm("views")["views"]
    bad = [v for v in m.views if v not in allow]
    if bad:
        raise mf.ManifestError(folder / "annex.yaml", [
            f"data.views: {', '.join(bad)} not in the gateway allowlist. Available views: {', '.join(allow)}. "
            "New views are added by the platform team in db/product/api_views.sql"])

    line = {"manifest": m.to_dict(), "deployed_at": _now(), "deployed_by": _who(), "via": via,
            "source": str(folder).replace(str(Path.home()), "~", 1), "git": _git(folder), "snapshot": None,
            "contract_version": contract_version(), "runtime": runtime_versions(folder)}
    overrides = {k: os.environ[k] for k in DEPLOY_OVERRIDES if os.environ.get(k, "").strip()}
    meta = {"line": line, "cli": cli_version(), **({"overrides": overrides} if overrides else {})}
    body = json.dumps(meta, default=str).encode() + b"\n" + _tar(folder)
    res = r.call("deploy", m.name, "--actor", _who(), "--via", via, input=body, progress=progress)
    res.pop("ok", None)
    res["warnings"] = list(res.get("warnings") or []) + _local_warnings(m, folder)
    return res


def _local_warnings(m: mf.Manifest, folder: Path | None = None) -> list[str]:
    """What only this side can tell a successful deploy: the app's runtime copies against this checkout's runtime/,
    and a raised memory limit. The VM adds the rest (reconcile notes, cleanup, alert recipients, Slack, log format)."""
    out = []
    for name in ("annex_runtime.py", "annex_runtime.js"):
        mine, current = (folder / name if folder else None), RUNTIME / name
        if mine and mine.is_file() and current.is_file():
            msg = runtime_message(name, runtime_version(mine.read_text(errors="replace")),
                                  runtime_version(current.read_text()), mine.read_bytes() == current.read_bytes())
            if msg:
                out.append(msg)
    if m.memory_mb > mf.MEMORY_DEFAULT_MB:
        out.append(f"{m.name} runs with {m.memory_mb}m of memory instead of the default {mf.MEMORY_DEFAULT_MB}m. "
                   "That's fine if it needs it; the platform team sees raised limits in annex list")
    return out


def _ops_ctl(r: Remote, *args: str) -> dict | list:
    """Run an annex-ops admin command (`python -m annex_ops.ctl ... --json`) and parse its answer."""
    res = r.run(f"docker exec {OPS_CONTAINER} python -m annex_ops.ctl {shlex.join(args)} --json", check=False)
    try:
        return json.loads(res.stdout.decode().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        detail = (res.stderr or res.stdout).decode(errors="replace").strip()[-300:]
        try:  # ctl reports its own errors (an unknown app, a bad option) as {"error": ...} on stderr
            raise AnnexError(json.loads(detail.splitlines()[-1])["error"]) from None
        except (IndexError, json.JSONDecodeError, KeyError, TypeError):
            pass
        raise AnnexError(f"annex-ops isn't answering ({detail or 'is the container running?'}). "
                         f"Check it with: {r.hint('docker logs ' + OPS_CONTAINER)}") from None


def usage(app: str | None = None, by_user: bool = False) -> dict | list:
    """Usage of every app (totals), or of one app by day. by_user adds per-login request counts: personal data,
    for admins (in the prototype, CLI access is SSH access, which is admin)."""
    if by_user and not app:
        raise AnnexError("--by-user needs an app: annex usage <app> --by-user")
    if app is not None:
        _name(app)
    return _ops_ctl(Remote(), "usage", *(["--app", app] if app else []), *(["--by-user"] if by_user else []))


def errors(app: str | None = None, state: str | None = None) -> list[dict]:
    """The kinds of error seen in the logs (server, browser and jobs), newest first: active, or quiet (a newer
    release is live and it hasn't come back, or it hasn't been seen for a day)."""
    if app is not None:
        _name(app)
    return _ops_ctl(Remote(), "errors", *([app] if app else []), *(["--state", state] if state else []))


def audit(app: str | None = None, since: str | None = None, action: str | None = None, actor: str | None = None,
          limit: int = 100) -> list[dict]:
    """The audit trail, newest first: deploys, rollbacks, restores, secrets set (names only), apps removed, accounts,
    sign-ins, reads of product data (per app, view and hour), emails, Slack posts and webhook events. Admins, by
    convention (in the prototype CLI access is SSH access)."""
    if app is not None:
        _name(app)
    return _ops_ctl(Remote(), "audit", *(["--app", app] if app else []), *(["--since", since] if since else []),
                    *(["--action", action] if action else []), *(["--actor", actor] if actor else []),
                    "--limit", str(max(1, min(int(limit), 2000))))


def hooks(app: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    """Incoming webhook events, newest first: metadata only (never a body)."""
    if app is not None:
        _name(app)
    return _ops_ctl(Remote(), "hooks", "list", *(["--app", app] if app else []), *(["--status", status] if status else []),
                    "--limit", str(max(1, min(int(limit), 500))))


def hook_show(event_id: int) -> dict:
    """One event with its headers and body (truncated to 64 KB). Human-only: bodies can hold personal data."""
    return _ops_ctl(Remote(), "hooks", "show", str(int(event_id)))


def hooks_replay(app: str, event_id: int | None = None, failed: bool = False, since: str | None = None) -> dict:
    """Queue copies of stored events for delivery again (the app sees the same X-Annex-Hook-Event). Human-only."""
    _name(app)
    if sum(x is not None and x is not False for x in (event_id, failed or None, since)) != 1:
        raise AnnexError("Choose what to replay: --id N, --failed, or --since 1h")
    return _ops_ctl(Remote(), "hooks", "replay", app, *(["--id", str(event_id)] if event_id is not None else []),
                    *(["--failed"] if failed else []), *(["--since", since] if since else []))


def sends(app: str | None = None, limit: int = 50) -> list[dict]:
    """Email and Slack send attempts, newest first (every attempt, rejections too; never a body)."""
    if app is not None:
        _name(app)
    return _ops_ctl(Remote(), "sends", *(["--app", app] if app else []), "--limit", str(max(1, min(int(limit), 500))))


def releases(app: str) -> list[dict]:
    """An app's releases, newest first, with which can be rolled back to (and with data)."""
    _name(app)
    r = Remote()
    rels = _releases(r, app)
    if not rels:
        if _record(r, app):
            raise AnnexError(f"{app} has no release history yet: it was deployed before releases were recorded. "
                             f"Its next deploy becomes r1")
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    live = _live(rels)
    images = _images(r, app)
    data_from = {s["from_release"] for s in r.vm("snapshots", app)["snapshots"]}
    out = []
    for x in reversed(rels):
        current = live is not None and x["release"] == live["release"]
        note = x.get("error", "")
        if x.get("rolled_back_to"):
            note = f"rolled back to r{x['rolled_back_to']}: {note}"
        if x.get("rollback"):
            rb = x["rollback"]
            note = f"rollback from r{rb['from']} to r{rb['to']}" + (" with data" if rb["with_data"] else "")
        if x["result"] == "pending":
            note = "in progress, or its deploy died (the next deploy or rollback reconciles it)"
        elif x.get("interrupted_at") and x["result"] == "live":
            note = f"interrupted at {x['interrupted_at']}, recorded live by reconcile (it was healthy)"
        git = x.get("git") or {}
        out.append({
            "release": x["release"], "current": current, "result": x["result"],
            "deployed_at": x["deployed_at"], "deployed_by": x["deployed_by"], "via": x["via"],
            "git": (git.get("sha", "") + ("+dirty" if git.get("dirty") else "")) or None,
            "cli": cli_label(x.get("cli")),
            "image": x["image"], "image_kept": x["image"] in images,
            "can_roll_back_to": x["result"] == "live" and not current and x["image"] in images,
            "data_snapshot": x["release"] in data_from, "note": note,
        })
    return out


def rollback(app: str, to: int | None = None, with_data: bool = False, progress: Progress = _quiet,
             via: str = "cli") -> dict:
    """Switch an app back to an earlier release (default: the one before the current), re-applying that release's
    manifest. with_data also restores the database and /data snapshot from that release's time, losing writes since
    (human only). One call: the VM resolves the target under the app's lock and runs the switch (D101)."""
    _name(app)
    meta = {"line": {"deployed_at": _now(), "deployed_by": _who(), "via": via}}
    return Remote().call("rollback", app, "--actor", _who(), "--via", via,
                         *(["--to", str(int(to))] if to is not None else []), *(["--with-data"] if with_data else []),
                         input=json.dumps(meta) + "\n", progress=progress)


# --- inspect ---------------------------------------------------------------
def _containers(r: Remote, app: str | None = None) -> list[dict]:
    flt = f"label=com.kaldi.annex.app={_name(app)}" if app is not None else "label=com.kaldi.annex.app"
    raw = r.out(f"ids=$(docker ps -aq --filter {flt}); [ -n \"$ids\" ] && docker inspect $ids || echo []")
    return json.loads(raw)


def _summary(c: dict, r: Remote) -> dict:
    lb = c["Config"]["Labels"]
    st = c["State"]
    app = lb["com.kaldi.annex.app"]
    return {
        "name": app,
        "owner": lb.get("com.kaldi.annex.owner"),
        "type": lb.get("com.kaldi.annex.type"),
        "state": st["Status"],
        "health": (st.get("Health") or {}).get("Status", "n/a"),
        "url": lb.get("homepage.href"),
        "started_at": st.get("StartedAt", "")[:19],
        "restarts": c.get("RestartCount", 0),
        "memory": lb.get("com.kaldi.annex.memory", f"{mf.MEMORY_DEFAULT_MB}m"),
    }


def list_apps() -> list[dict]:
    r = Remote()
    return sorted((_summary(c, r) for c in _containers(r)), key=lambda a: a["name"])


def status(app: str) -> dict:
    _name(app)
    r = Remote()
    cs = _containers(r, app)
    record = _record(r, app)
    if not cs and not record:
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    info = _summary(cs[0], r) if cs else {"name": app, "state": "missing"}
    if record:
        info["release"] = f"r{record['release']}" if record.get("release") else "none (deployed before releases)"
        info.update({k: record[k] for k in ("image", "deployed_at", "postgres", "files", "views", "secrets", "schedule")})
        info["memory"] = f"{record.get('memory_mb', mf.MEMORY_DEFAULT_MB)}m"
        notify = (["email"] if record.get("notify_email") else []) + \
                 [f"Slack {c}" for c in record.get("notify_slack", [])]
        info["notify"] = ", ".join(notify) or "none"
        info["alerts_to"] = ", ".join(record.get("alerts_to", ["owner"]))
        if record.get("webhooks"):
            info["webhooks"] = _webhook_urls(r, app, record["webhooks"])
    cli = cli_label((_live(_releases(r, app)) or {}).get("cli"))
    if cli:
        info["cli"] = cli  # the commit of the CLI that made the live release
    deploy_line = _deploy_state(r, app)
    if deploy_line:
        info["deploy"] = deploy_line
    try:
        health = _ops_ctl(r, "app-status", app)
    except AnnexError:
        health = {"health_check": {"state": "unknown", "detail": "annex-ops can't be reached"}, "incident": None, "jobs": []}
    info["usage"] = health.get("usage")
    info["health_check"] = health["health_check"]
    info["incident"] = health["incident"]
    runs = {j["job"]: j for j in health["jobs"]}
    info["jobs"] = [{"job": j["name"], "cron": j["cron"], **{k: v for k, v in runs.get(j["name"], {}).items() if k != "job"}}
                    for j in (record or {}).get("schedule", [])]
    info["recent_logs"] = r.out(f"docker logs --tail 15 {container(app)} 2>&1", check=False).splitlines()
    if record and not record.get("url") and record.get("release"):
        # a job-only app has no web start-up to judge at deploy time: its first stored lines (from a scheduled run)
        try:
            first = _stored_logs(r, app, 50, release=record["release"], oldest=True)
        except AnnexError:
            first = []
        if (w := logcheck.rows_warning(first)):
            info["warnings"] = [w]
    return info


def deploy_state_text(state: dict, app: str) -> str | None:
    """`annex status`'s deploy line from `annex_vm.py deploy-state` (pure): the run going on now, or the last one."""
    cur = state.get("current") or {}
    if not cur:
        return None
    kind = cur.get("kind") or "deploy"
    rel = f"r{cur['release']} " if cur.get("release") else ""
    who = f"by {cur.get('actor') or 'unknown'}, started {cur.get('started') or 'at an unknown time'}"
    if state.get("running"):
        return (f"{kind} of {rel}running on the VM at step {cur.get('step') or 'unknown'} ({who}; pid {cur.get('pid')}). "
                f"Its outcome: annex releases {app}")
    return f"none running; the last was a {kind} of {rel}({who}): {cur.get('result') or 'ended without a result'}"


def _deploy_state(r: Remote, app: str) -> str | None:
    try:
        return deploy_state_text(r.vm("deploy-state", app), app)
    except AnnexError:  # a platform from before D101 has no deploy-state
        return None


def _stored_logs(r: Remote, app: str, tail: int = 100, since: str | None = None, release: int | str | None = None,
                 level: str | None = None, grep: str | None = None, request: str | None = None, source: str = "app",
                 oldest: bool = False) -> list[dict]:
    """An app's log lines as annex-ops stored them (14 days, whatever happened to the container), oldest first."""
    try:
        rel = logview.release_number(None if release is None else str(release))
    except ValueError as e:
        raise AnnexError(str(e)) from None
    args = ["logs", app, "-n", str(max(1, min(int(tail), 5000))), "--source", source,
            *(["--since", since] if since else []), *(["--release", str(rel)] if rel is not None else []),
            *(["--level", level] if level else []), *(["--grep", grep] if grep else []),
            *(["--request", request] if request else []), *(["--oldest"] if oldest else [])]
    return _ops_ctl(r, *args)


def stored_logs(app: str, tail: int = 100, since: str | None = None, release: int | str | None = None,
                level: str | None = None, grep: str | None = None, request: str | None = None,
                source: str = "app") -> list[dict]:
    """`annex logs --json`: the matching stored lines, oldest first."""
    _name(app)
    r = Remote()
    rows = _stored_logs(r, app, tail, since, release, level, grep, request, source)
    if not rows and not _containers(r, app) and not _record(r, app):
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    return rows


def logs(app: str, tail: int = 100, since: str | None = None, release: int | str | None = None,
         level: str | None = None, grep: str | None = None, request: str | None = None, source: str = "app") -> str:
    """An app's logs from annex-ops' store: the live container's lines and those of containers that are gone (a
    rolled-back release, last week). Filter by release (r4), level (this and above), text, request id and age.
    If annex-ops can't be reached, the live container's own log is shown instead, unfiltered."""
    _name(app)
    r = Remote()
    try:
        rows = _stored_logs(r, app, tail, since, release, level, grep, request, source)
    except AnnexError as e:
        if not str(e).startswith("annex-ops isn't answering"):
            raise
        if any((release, level, grep, request)) or source != "app":
            raise AnnexError(f"{e}\nThe stored logs need annex-ops, so --release, --level, --grep, --request and --source "
                             f"can't be used until it is back") from None
        if not _containers(r, app):
            raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list") from None
        s = f" --since {shlex.quote(since)}" if since else ""
        return ("(annex-ops isn't answering: this is the live container's own log, not the stored one)\n"
                + r.out(f"docker logs --timestamps --tail {int(tail)}{s} {container(app)} 2>&1"))
    if not rows and not _containers(r, app) and not _record(r, app):
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    return logview.render_all(rows)


def follow_logs(app: str, tail: int = 100) -> int:
    _name(app)
    return Remote().stream(f"docker logs --timestamps --tail {int(tail)} -f {container(app)}")


def run_job(app: str, job: str) -> dict:
    _name(app)
    _job(job)
    r = Remote()
    record = _record(r, app)
    if not record:
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    jobs = {j["name"]: j for j in record.get("schedule", [])}
    if job not in jobs:
        raise AnnexError(f"{app} has no job {job!r}. Jobs: {', '.join(jobs) or 'none'} (schedule in annex.yaml)")
    t0 = time.time()
    res = r.run(f"docker exec {container(app)} {shlex.join(jobs[job]['command'])} 2>&1", check=False)
    return {"app": app, "job": job, "exit_code": res.returncode, "seconds": round(time.time() - t0, 1),
            "output": res.stdout.decode(errors="replace")[-8000:]}


# --- human-only ------------------------------------------------------------
def secret_set(app: str, pairs: list[str]) -> dict:
    _name(app)
    for p in pairs:
        k, sep, _ = p.partition("=")
        if not sep or not mf.KEY_RE.fullmatch(k):
            raise AnnexError(f"{k!r}: use KEY=VALUE with an UPPER_SNAKE_CASE key")
    r = Remote()
    res = r.vm("secret-set", app, "--actor", _who(), input="\n".join(pairs) + "\n")
    record = _record(r, app)
    if record:
        undeclared = [k for k in res["set"] if k not in record.get("secrets", [])]
        r.vm("provision", *_provision_args(
            app, postgres=record["postgres"], files=record["files"], views=record["views"], secrets=record["secrets"],
            notify_email=record.get("notify_email", False), notify_slack=record.get("notify_slack", []),
            webhooks=record.get("webhooks", []), port=record.get("port")))
        _drain(r, app)
        r.run(f"cd {r.path('deploy', app)} && docker compose up -d 2>&1")
        res["applied"] = "restarted with new values"
        if undeclared:
            res["warning"] = (f"{', '.join(undeclared)} not declared under secrets: in annex.yaml, "
                              "so not injected. Add them there and deploy again")
    else:
        res["applied"] = "stored; injected on the next deploy"
    return res


def remove(app: str, keep_data: bool = False) -> dict:
    _name(app)
    r = Remote()
    record = _record(r, app)
    if not record and not _containers(r, app):
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    d = r.path("deploy", app)
    _drain(r, app)
    r.run(f"if [ -f {d}/compose.yaml ]; then cd {d} && docker compose down --remove-orphans 2>&1; "
          f"else docker rm -f {container(app)} >/dev/null 2>&1 || true; fi; "
          f"docker image ls -q annex-app/{app} | xargs -r docker rmi -f >/dev/null 2>&1 || true")
    res = r.vm("deprovision", app, "--actor", _who(), *(["--keep-data"] if keep_data else []))
    if record and record.get("schedule"):
        r.run(f"docker restart {OFELIA} >/dev/null")
    return res


def migrate_networks(app: str, egress: bool = False, progress: Progress = _quiet) -> dict:
    """Move an app deployed before per-app networks (D88) onto its own network without its source (human only):
    rewrite the networks of its compose file on the VM (the old file is kept as compose.yaml.pre-d88), record egress
    in deploy.json, connect the platform containers it uses and recreate the container. An app whose source is at hand
    should be redeployed instead. egress=True keeps internet access (the old shared network had it)."""
    _name(app)
    r = Remote()
    d = r.path("deploy", app)
    res = r.run(f"cat {d}/compose.yaml", check=False)
    if res.returncode != 0:
        raise AnnexError(f"{app} has no compose file on the VM ({r.data}/deploy/{app}/compose.yaml). See: annex list")
    doc, uses = rewire_compose(yaml.safe_load(res.stdout.decode()) or {}, app, egress)
    record = _record(r, app)
    progress(f"Connecting the platform to {app_network(app)}")
    sync = r.vm(*_network_args(app, **uses))
    if record is not None:
        record["egress"] = ["*"] if egress else []  # "*": hosts unknown (migrated); networks-sync only asks "any?"
        r.run(f"cat > {d}/deploy.json", input=json.dumps(record, indent=2))
    r.run(f"[ -f {d}/compose.yaml.pre-d88 ] || cp {d}/compose.yaml {d}/compose.yaml.pre-d88")
    r.run(f"cat > {d}/compose.yaml", input=yaml.safe_dump(doc, sort_keys=False))
    _drain(r, app, progress)
    progress(f"Recreating {container(app)} on its own network")
    r.run(f"cd {d} && docker compose up -d --remove-orphans 2>&1")
    nets = r.out(f"docker inspect -f '{{{{range $k, $v := .NetworkSettings.Networks}}}}{{{{$k}}}} {{{{end}}}}' {container(app)}",
                 check=False).split()
    return {"app": app, "uses": uses, "networks": sorted(nets), "platform": sync.get("platform", []),
            "backup": f"{r.data}/deploy/{app}/compose.yaml.pre-d88"}


def users(action: str, login: str | None = None, **opts) -> dict:
    """Annex's own user accounts (human only, not over MCP; D57). action: list, add, set, remove or reset.
    Options are passed on as flags: name="x" -> --name x, print_password=True -> --print-password."""
    args = [f"users-{action}", *([login] if login else []), *(["--actor", _who()] if action not in ("list", "sync") else [])]
    for key, value in opts.items():
        if value is None or value is False:
            continue
        flag = "--" + key.replace("_", "-")
        args += [flag] if value is True else [flag, str(value)]
    return Remote().vm(*args)
