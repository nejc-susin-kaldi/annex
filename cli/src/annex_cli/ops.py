"""Annex operations. Shared by the CLI and the MCP server.

Each function returns plain data (dicts, strings) and raises AnnexError or
ManifestError with a message that says what to do next.
"""
from __future__ import annotations

import getpass
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable

import yaml

from . import logcheck, logview
from . import manifest as mf
from .remote import REPO, AnnexError, Remote

TEMPLATES = REPO / "templates"
RUNTIME = REPO / "runtime"  # the one source of annex_runtime.py/.js; scripts/sync-runtime.sh copies it
CONTRACT = Path(__file__).with_name("contract.md")
APP_UID = 10001
TRAEFIK = "annex-traefik-1"
OFELIA = "annex-ofelia-1"
GATEWAY_URL = "http://gateway:3000"
OPS_URL = "http://ops:8000"
OPS_CONTAINER = "annex-ops-1"
HEALTH_TIMEOUT = int(os.environ.get("ANNEX_HEALTH_TIMEOUT", "60"))
BUILD_MIN_FREE_GB = 3  # D95: a build is refused with less than this free on the data disk
IMAGE_MAX_MB = 2048  # D95: a built image above this is refused and removed
APP_PIDS_LIMIT = 256  # D95: processes an app container may have (a fork bomb stays inside it)
KEEP_RELEASES = 3  # images and data snapshots kept per app
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


def _tile_labels(m: mf.Manifest, fields: list[tuple[str, str, str | None]]) -> dict[str, str]:
    """Homepage's customapi widget on the app's discovery tile: annex-ops answers /tiles/<app> with totals and
    status (never logins), and Homepage calls it from its own server. (label, field, extra field shown beside it)"""
    labels = {"homepage.widget.type": "customapi", "homepage.widget.url": f"http://ops:8000/tiles/{m.name}",
              "homepage.widget.refreshInterval": "60000"}
    for i, (label, field, extra) in enumerate(fields):
        labels[f"homepage.widget.mappings[{i}].label"] = label
        labels[f"homepage.widget.mappings[{i}].field"] = field
        if extra:
            labels[f"homepage.widget.mappings[{i}].additionalField.field"] = extra
            labels[f"homepage.widget.mappings[{i}].additionalField.color"] = "adaptive"
    return labels


def compose_for(m: mf.Manifest, r: Remote, image: str, release: int) -> dict:
    data_dir = r.data
    url = r.url_for(m.name)
    # LOG_LEVEL has a default that env: may override (the Logging section of the contract)
    env = {"ANNEX_APP_NAME": m.name, "ANNEX_BASE_URL": url, "LOG_LEVEL": "info", **m.env}
    labels = {
        "com.kaldi.annex": "true",
        "com.kaldi.annex.app": m.name,
        "com.kaldi.annex.owner": m.owner,
        "com.kaldi.annex.type": m.type,
        "com.kaldi.annex.release": str(release),
        "com.kaldi.annex.memory": f"{m.memory_mb}m",
        "com.kaldi.annex.alerts-to": ",".join(m.alerts_to),
        "homepage.group": "Tools",
        "homepage.name": m.name,
        "homepage.description": f"{m.description} · owner: {m.owner}" if m.description else f"owner: {m.owner}",
        "homepage.icon": "mdi-toolbox-outline",
    }
    svc: dict = {
        # built by _build before the switch; a rollback reuses an older image
        "image": image,
        "pull_policy": "never",
        "container_name": container(m.name),
        "restart": "unless-stopped",
        "user": f"{APP_UID}:{APP_UID}",
        "read_only": True,
        "tmpfs": ["/tmp:size=64m,mode=1777"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": f"{m.memory_mb}m",
        "cpus": 0.5,
        "pids_limit": APP_PIDS_LIMIT,
        "env_file": [f"{data_dir}/secrets/apps/{m.name}/runtime.env"],
        "environment": env,
        "labels": labels,
    }
    if m.is_web:
        env["PORT"] = str(m.port)
        labels.update({
            "traefik.enable": "true",
            # Traefik reaches the app on its own network; the provider default (annex-edge) is for gateway and hooks
            "traefik.docker.network": app_network(m.name),
            f"traefik.http.routers.app-{m.name}.rule": f"Host(`{r.host_for(m.name)}`)",
            f"traefik.http.services.app-{m.name}.loadbalancer.server.port": str(m.port),
            "homepage.href": url,
            # annex-ops excludes the health path from usage counts
            "com.kaldi.annex.healthcheck": m.healthcheck,
        })
        labels.update(_tile_labels(m, [("Users 7d", "users_7d", "trend"), ("Status", "status", None)]))
    else:
        labels["traefik.enable"] = "false"  # an image label can't turn routing on (D83)
        # job-only apps idle between runs; jobs are executed inside this container
        svc["command"] = ["sleep", "infinity"]
        labels.update(_tile_labels(m, [("Last run", "last_run", None), ("Status", "status", None)]))
    if m.files:
        env["DATA_DIR"] = "/data"
        svc["volumes"] = [f"{data_dir}/apps/{m.name}/data:/data"]
    if m.views:
        env["ANNEX_GATEWAY_URL"] = GATEWAY_URL
    if m.notify_email or m.notify_slack:
        env["ANNEX_OPS_URL"] = OPS_URL  # the token is in the env file
    if m.webhooks:
        env["ANNEX_HOOKS_URL"] = f"{r.settings.get('ANNEX_URL_HOOKS', '')}/h/{m.name}"  # the token is in the env file
    labels["ofelia.enabled"] = "true" if m.schedule else "false"  # an image label can't turn the scheduler on (D83)
    if m.schedule:
        for j in m.schedule:
            key = f"ofelia.job-exec.{m.name}-{j.name}"
            labels[f"{key}.schedule"] = mf.ofelia_schedule(j.cron)
            labels[f"{key}.command"] = shlex.join(j.command)
            labels[f"{key}.no-overlap"] = "true"
            labels[f"{key}.user"] = str(APP_UID)  # Ofelia's job-exec defaults to root
    svc["networks"], networks = _compose_networks(m.name, bool(m.egress))
    return {
        "name": project(m.name),
        "services": {m.name: svc},
        "networks": networks,
    }


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
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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
# who/when/where from, data snapshot and result: "pending" while it runs (written by annex_vm.py deploy-begin, which
# also takes the app's deploy lock), then "live", or "failed" when it didn't pass the health gate or broke off
# (rewritten by deploy-end, or by deploy-reconcile when the deploy died; C1, D92).
def _releases(r: Remote, app: str) -> list[dict]:
    res = r.run(f"cat {r.path('apps', _name(app), 'releases.jsonl')}", check=False)
    if res.returncode != 0:
        return []
    return [json.loads(line) for line in res.stdout.decode().splitlines() if line.strip()]


class _Lock:
    """One deploy or rollback's hold on its app (C1, D92): annex_vm.py deploy-begin takes the app's lock, settles a
    release an earlier run left pending, numbers this release and records it as pending; step() says where it is (for
    whoever finds the lock held, and for reconcile); end() rewrites the pending line with the outcome (the VM also
    copies it to annex_ops.releases) and releases the lock. Used as a context manager: leaving it without end(), by
    an exception or Ctrl-C, records the release as failed at the step it reached."""

    def __init__(self, r: Remote, app: str, line: dict, record: dict | None, progress: Progress = _quiet):
        self.r, self.app, self.ended, self.at = r, _name(app), False, "begin"
        stale = os.environ.get("ANNEX_DEPLOY_LOCK_STALE")  # seconds; operations.md: only when the holder is dead
        res = r.vm("deploy-begin", app, "--actor", _who(), *(["--stale-after", str(int(stale))] if stale else []),
                   input=json.dumps({"line": line, "record": record}, default=str))
        self.n, self.image, self.token = res["release"], res["image"], res["token"]
        self.line = {**line, "release": self.n, "image": self.image}
        self.notes = [x for x in [res.get("took_over")] if x] + [x["message"] for x in res.get("reconciled", [])]
        self.notes += res.get("warnings", [])
        for note in self.notes:
            progress(note)

    def step(self, name: str) -> None:
        self.at = self.r.step = name
        self.r.vm("deploy-step", self.app, self.token, name)

    def end(self, rel: dict, timeout: int = 900) -> dict:
        res = self.r.vm("deploy-end", self.app, self.token, str(self.n), rel["result"],
                        input=json.dumps({**self.line, **rel}, default=str), timeout=timeout)
        self.ended, self.r.step = True, None
        return res

    def __enter__(self) -> _Lock:
        return self

    def __exit__(self, kind, exc, tb) -> bool:
        if not self.ended:
            why = (str(exc).strip().splitlines() or [kind.__name__])[0] if exc else "ended without an outcome"
            try:  # best effort: if the VM can't be reached, the next deploy reconciles the pending line
                self.end({"result": "failed", "error": f"broke off at {self.at}: {why}"[:300],
                          "interrupted_at": self.at}, timeout=60)
            except (AnnexError, subprocess.SubprocessError, OSError):
                pass
        return False


def _live(releases: list[dict]) -> dict | None:
    """The release that's running now: the last one that went live."""
    live = [x for x in releases if x["result"] == "live"]
    return live[-1] if live else None


def _images(r: Remote, app: str) -> set[str]:
    return set(r.out(f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' annex-app/{_name(app)}").split())


# Label prefixes only Annex may set: Traefik routes, Ofelia jobs, Homepage tiles, backup hooks, Annex's own markers (D83).
PLATFORM_LABEL_PREFIXES = ("traefik.", "ofelia.", "homepage.", "docker-volume-backup.", "com.kaldi.annex")


def platform_label_offenders(labels: dict | None, app: str) -> list[str]:
    """The keys of an image's labels that a Dockerfile must not set, sorted. `docker build` is given exactly
    com.kaldi.annex=true and com.kaldi.annex.app=<app>, which land on the image too: those two are fine."""
    allowed = {"com.kaldi.annex": "true", "com.kaldi.annex.app": app}
    return sorted(k for k, v in (labels or {}).items()
                  if k.lower().startswith(PLATFORM_LABEL_PREFIXES) and allowed.get(k) != v)


def build_space_verdict(free_bytes: int, min_gb: float) -> str | None:
    """None if the disk has room for a build, else the refusal (pure, D95)."""
    if free_bytes >= min_gb * 1024 ** 3:
        return None
    return (f"The VM has only {free_bytes / 1024 ** 3:.1f} GB free on its data disk and a build needs at least "
            f"{min_gb:g} GB, so nothing was built and the running version is unchanged. Tell the Annex owner: "
            "'docker system df' on the VM shows what takes the space, 'docker image prune -a' and "
            "'docker builder prune' free it")


def image_size_verdict(size_bytes: int, max_mb: float, app: str) -> str | None:
    """None if the image is small enough, else the refusal (pure, D95)."""
    if size_bytes <= max_mb * 1024 ** 2:
        return None
    return (f"The image of {app} is {size_bytes / 1024 ** 2:.0f} MB, above the limit of {max_mb:g} MB, so it was "
            "removed and the running version is unchanged. Make it smaller: a slim base image, no build tools or "
            "caches in the final stage ('pip install --no-cache-dir'), and no data files in the source folder "
            "(the Dockerfile's COPY takes everything in it)")


def _limit(r: Remote, key: str, default: float) -> float:
    """A build limit: the CLI's environment first (tests), then the platform's .env (the VM owner), then the default."""
    raw = os.environ.get(key) or r.settings.get(key) or ""
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise AnnexError(f"{key}={raw!r} is not a number. Set it to a plain number in the platform's .env "
                         "(scripts/push.sh applies it) or unset it to use the default")
    return value


def _build(r: Remote, m: mf.Manifest, image: str) -> None:
    free = r.out(f"df -PB1 {shlex.quote(r.data)} | awk 'NR==2 {{print $4}}'", check=False).strip()
    if free.isdigit():  # a df that can't answer must not block a deploy
        refusal = build_space_verdict(int(free), _limit(r, "ANNEX_BUILD_MIN_FREE_GB", BUILD_MIN_FREE_GB))
        if refusal:
            raise AnnexError(refusal)
    d = r.path("deploy", _name(m.name))
    build = r.run(f"docker build --progress=plain --label com.kaldi.annex=true --label com.kaldi.annex.app={m.name} "
                  f"-t {image} {d}/src 2>&1", check=False)
    if build.returncode != 0:
        tail = "\n".join(build.stdout.decode(errors="replace").splitlines()[-40:])
        raise AnnexError(f"Docker build failed for {m.name}. Fix the Dockerfile or code and deploy again. "
                         f"The running version is unchanged.\n{tail}")
    size = r.out(f"docker image inspect -f '{{{{.Size}}}}' {image}", check=False).strip()
    refusal = size.isdigit() and image_size_verdict(int(size), _limit(r, "ANNEX_IMAGE_MAX_MB", IMAGE_MAX_MB), m.name)
    if refusal:
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        raise AnnexError(refusal)
    user = r.out(f"docker image inspect -f '{{{{.Config.User}}}}' {image}").strip()
    if user in ("", "root", "0", "0:0"):
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        raise AnnexError("The image runs as root. End the Dockerfile with a non-root user, e.g. "
                         "'RUN useradd -u 10001 app' and 'USER 10001' (the templates already do this)")
    raw = r.out(f"docker image inspect -f '{{{{json .Config.Labels}}}}' {image}").strip()
    bad = platform_label_offenders(json.loads(raw or "null"), m.name)
    if bad:
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        raise AnnexError(f"The image carries labels that are Annex's job: {', '.join(bad)}. Remove the LABEL lines "
                         "from the Dockerfile. Annex sets the routing, schedule, discovery-page and backup labels "
                         "itself from annex.yaml; images may not carry traefik.*, ofelia.*, homepage.*, "
                         "docker-volume-backup.* or com.kaldi.annex* labels")


def _gate(r: Remote, m: mf.Manifest, progress: Progress) -> tuple[str, str] | None:
    """Health gate. None if the new container is healthy, else (reason, last log lines)."""
    c = container(m.name)
    if m.is_web:
        progress(f"Health gate: waiting up to {HEALTH_TIMEOUT} s for {m.healthcheck} to return 200")
        probe = shlex.quote(f"http://{c}:{m.port}{m.healthcheck}")
        res = r.run(f"end=$((SECONDS + {HEALTH_TIMEOUT})); while [ $SECONDS -lt $end ]; do "
                    f"docker exec {TRAEFIK} wget -q -O /dev/null -T 3 {probe} && exit 0; "
                    f"[ \"$(docker inspect -f '{{{{.State.Status}}}}' {c})\" = running ] || exit 2; "
                    f"sleep 2; done; exit 1", check=False)
        if res.returncode == 0:
            return None
        reason = ("the container stopped while starting (it crashed; see the log lines)" if res.returncode == 2 else
                  f"GET {m.healthcheck} on port {m.port} did not return 200 within {HEALTH_TIMEOUT} s. "
                  f"Make sure the app listens on 0.0.0.0:$PORT and {m.healthcheck} returns 200")
    else:
        state = r.out(f"sleep 3; docker inspect -f '{{{{.State.Status}}}}' {c}", check=False).strip() or "missing"
        if state == "running":
            return None
        reason = (f"the container is {state} instead of running (it exits; see the log lines). Job-only apps "
                  "idle with 'sleep infinity' between runs, so the image needs a 'sleep' binary and no ENTRYPOINT "
                  "that exits (the python-job template is fine)")
    return reason, r.out(f"docker logs --tail 40 {c} 2>&1", check=False)


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
                 f"the deploy carries on. Check: ssh {r.host} docker logs {OPS_CONTAINER}")


def _activate(r: Remote, m: mf.Manifest, image: str, release: int, progress: Progress,
              step: Callable[[str], None] = _quiet) -> tuple[tuple[str, str] | None, dict]:
    """Provision what manifest `m` asks for, switch the app to `image` and run the health gate.
    Returns (gate failure or None, provision result). `step` hears provision, switch and gate (the deploy lock)."""
    d = r.path("deploy", _name(m.name))
    step("provision")
    progress("Provisioning storage, data access and secrets")
    prov = r.vm("provision", *_provision_args(m.name, postgres=m.postgres, files=m.files, views=m.views,
                                              secrets=m.secrets, notify_email=m.notify_email,
                                              notify_slack=m.notify_slack, port=m.port,
                                              webhooks=[w.__dict__ for w in m.webhooks]))
    # before the switch: the app's network and the platform containers on it must exist when the container starts (D88)
    r.vm(*_network_args(m.name, web=m.is_web, postgres=m.postgres, views=bool(m.views), egress=bool(m.egress)))
    compose = yaml.safe_dump(compose_for(m, r, image, release), sort_keys=False)
    r.run(f"mkdir -p {d} && cat > {d}/compose.yaml", input=compose)
    _drain(r, m.name, progress)  # the container Compose is about to replace (a failed release's, on a rollback)
    step("switch")
    progress(f"Starting r{release} ({image})")
    r.run(f"cd {d} && docker compose up -d --remove-orphans 2>&1")
    step("gate")
    failed = _gate(r, m, progress)
    if failed or not m.is_web:
        return failed, prov
    progress("Waiting for the route and the container healthcheck")
    host = r.host_for(m.name)
    if r.settings["ANNEX_TLS_MODE"] == "local":
        route = f"curl -sk -o /dev/null -w '%{{http_code}}' --resolve {host}:443:127.0.0.1 https://{host}/"
    else:
        route = f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Host: {host}' http://127.0.0.1/"
    # routed = the front door answers with a login redirect instead of 404
    r.run(f"for i in $(seq 30); do [ \"$({route})\" = 302 ] && break; sleep 1; done; "
          f"for i in $(seq 45); do h=$(docker inspect -f '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}"
          f"{{{{else}}}}none{{{{end}}}}' {container(m.name)}); [ \"$h\" = healthy ] || [ \"$h\" = none ] && break; "
          f"sleep 2; done", check=False)
    return None, prov


def _reload_scheduler(r: Remote, progress: Progress, *schedules: object) -> None:
    """Ofelia reads job labels only at startup (D18)."""
    if any(schedules):
        progress("Reloading the scheduler")
        r.run(f"docker restart {OFELIA} >/dev/null")


def _write_record(r: Remote, m: mf.Manifest, image: str, release: int) -> dict:
    """deploy.json: what's running now (read by status, run, secret set, remove and restore)."""
    record = _record_for(r, m, image, release)
    r.run(f"cat > {r.path('deploy', _name(m.name), 'deploy.json')}", input=json.dumps(record, indent=2))
    return record


def _record_for(r: Remote, m: mf.Manifest, image: str | None, release: int | None) -> dict:
    """deploy.json's content. deploy-begin gets it without image and release and keeps it with the pending line, so
    that a deploy that died after its gate can still be recorded as live (deploy-reconcile writes it then)."""
    return {"name": m.name, "owner": m.owner, "type": m.type, "release": release, "image": image,
              "url": r.url_for(m.name) if m.is_web else None, "deployed_at": _now(), "postgres": m.postgres,
              "files": m.files, "views": m.views, "secrets": m.secrets, "schedule": [j.__dict__ for j in m.schedule],
              "memory_mb": m.memory_mb, "notify_email": m.notify_email, "notify_slack": m.notify_slack,
              "alerts_to": m.alerts_to, "webhooks": [w.__dict__ for w in m.webhooks], "port": m.port,
              "egress": m.egress}  # networks-sync (push.sh) reads type, postgres, views and egress from here


def _cleanup(r: Remote, app: str, current: str, progress: Progress = _quiet) -> list[str]:
    """Keep the images of the last KEEP_RELEASES live releases and as many snapshots; trim build cache. Runs under the
    deploy lock (another deploy's fresh image must not be removed), before end(): the release going live is still
    pending in the file, so its image (`current`) is named. Best effort: the release is live whatever happens here.
    Returns the warning for the deploy result when it fails (D95), so the disk filling up is not silent."""
    try:
        _prune(r, app, current)
    except (AnnexError, subprocess.SubprocessError, OSError) as e:
        msg = (f"Cleaning up old images and snapshots failed ({(str(e).splitlines() or [''])[0]}); the next deploy "
               "tries again. If this keeps happening, tell the Annex owner: the disk may fill up")
        progress("Warning: " + msg)
        return [msg]
    return []


def _prune(r: Remote, app: str, current: str) -> None:
    keep: list[str] = [current]
    for x in reversed(_releases(r, app)):
        if x["result"] == "live" and x["image"] not in keep:
            keep.append(x["image"])
        if len(keep) == KEEP_RELEASES:
            break
    pattern = " ".join(f"-e {shlex.quote(i)}" for i in keep)
    r.run(f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' annex-app/{app} | grep -vx {pattern} "
          f"| xargs -r docker rmi >/dev/null 2>&1; docker image prune -f >/dev/null; "
          f"docker builder prune -f --max-used-space 1500mb >/dev/null", check=False)
    r.vm("prune-snapshots", app, "--keep", str(KEEP_RELEASES))


def _gate_error(app: str, release: int, failed: tuple[str, str], outcome: str) -> AnnexError:
    reason, logs = failed
    return AnnexError(f"{app} r{release} failed the health gate: {reason}.\n{outcome}\n"
                      f"Last log lines of r{release}:\n{logs}")


def deploy(folder: Path, progress: Progress = _quiet, via: str = "cli") -> dict:
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

    # The lock covers everything from here: the source upload and the build too, which share deploy/<app>/src and the
    # image tag r<n> with any other deploy of the app (D92).
    line = {"manifest": m.to_dict(), "deployed_at": _now(), "deployed_by": _who(), "via": via,
            "source": str(folder).replace(str(Path.home()), "~", 1), "git": _git(folder), "snapshot": None}
    with _Lock(r, m.name, line, _record_for(r, m, None, None), progress) as lock:
        return _deploy(r, m, folder, lock, progress)


def _deploy(r: Remote, m: mf.Manifest, folder: Path, lock: _Lock, progress: Progress) -> dict:
    n, image = lock.n, lock.image
    previous = _record(r, m.name)
    live = _live(_releases(r, m.name))  # read under the lock, after deploy-begin reconciled
    d = r.path("deploy", _name(m.name))
    lock.step("upload")
    progress("Uploading source")
    r.run(f"set -e; mkdir -p {d}; rm -rf {d}/src.new; mkdir {d}/src.new; tar -xzf - -C {d}/src.new; "
          f"rm -rf {d}/src; mv {d}/src.new {d}/src", input=_tar(folder))

    lock.step("build")
    progress(f"Building r{n} on the VM")
    _build(r, m, image)

    rel: dict = {"snapshot": None}
    if live:
        lock.step("snapshot")
        progress(f"Snapshotting data before the switch (r{live['release']} is live)")
        snapshot = r.vm("snapshot", m.name, "--release", str(n), "--from-release", str(live["release"]))["snapshot"]
        rel["snapshot"] = snapshot and snapshot["name"]

    failed, prov = _activate(r, m, image, n, progress, lock.step)
    if failed:
        rel.update(result="failed", error=failed[0])
        if not live:
            lock.end(rel)
            raise _gate_error(m.name, n, failed, f"There's no earlier release to roll back to, so r{n} is left as "
                              f"is. Fix the app and deploy again; see its logs with: annex logs {m.name}")
        progress(f"r{n} failed the health gate; rolling back to r{live['release']}")
        prev = mf.Manifest.from_dict(live["manifest"])
        # a failed gate re-provisions the previous release's manifest (grants, secrets, networks), as before C1
        back, _ = _activate(r, prev, live["image"], live["release"], progress, lambda s: lock.step(f"back-{s}"))
        _reload_scheduler(r, progress, m.schedule, prev.schedule)
        _write_record(r, prev, live["image"], live["release"])
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        lock.end({**rel, "rolled_back_to": live["release"]})
        if back:
            raise _gate_error(m.name, n, failed, f"Rolling back to r{live['release']} failed too ({back[0]}), "
                              f"so {m.name} is down. Check: annex logs {m.name}")
        raise _gate_error(m.name, n, failed, f"Annex rolled back to r{live['release']}, which is live again. "
                          "Fix the app and deploy again.")

    lock.step("finish")
    _reload_scheduler(r, progress, m.schedule, previous and previous.get("schedule"))
    record = _write_record(r, m, image, n)
    cleanup_warnings = _cleanup(r, m.name, image, progress)
    lock.end({**rel, "result": "live"})
    progress("Done")
    warnings = list(lock.notes) + cleanup_warnings + _warnings(r, m, folder)
    if m.is_web:  # job-only apps are checked on their first run (a later step)
        lines = r.out(f"docker logs --tail 50 {container(m.name)} 2>&1", check=False).splitlines()
        warnings += [w for w in [logcheck.warning(lines)] if w]
    return {**record, "warnings": warnings, "webhooks": _webhook_urls(r, m.name, m.webhooks), "previous_release": live and live["release"], "snapshot": rel["snapshot"],
            "injected_env": sorted({"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL", *prov["injected"],
                                    *(["DATA_DIR"] if m.files else []),
                                    *(["ANNEX_GATEWAY_URL"] if m.views else []),
                                    *(["ANNEX_OPS_URL"] if m.notify_email or m.notify_slack else []),
                                    *(["ANNEX_HOOKS_URL"] if m.webhooks else [])}
                                  - ({"PORT"} if not m.is_web else set()))}


def _warnings(r: Remote, m: mf.Manifest, folder: Path | None = None) -> list[str]:
    """Things a successful deploy should still tell the builder. More are added here by later milestones."""
    out = []
    for name in ("annex_runtime.py", "annex_runtime.js"):
        mine, current = (folder / name if folder else None), RUNTIME / name
        if mine and mine.is_file() and current.is_file() and mine.read_bytes() != current.read_bytes():
            out.append(f"{name} differs from the platform's current helper, so some helpers may be missing or "
                       f"outdated. Unless you changed it on purpose, replace it with the copy from a new "
                       f"`annex init` folder (templates are kept up to date)")
    if m.memory_mb > mf.MEMORY_DEFAULT_MB:
        out.append(f"{m.name} runs with {m.memory_mb}m of memory instead of the default {mf.MEMORY_DEFAULT_MB}m. "
                   "That's fine if it needs it; the platform team sees raised limits in annex list")
    try:
        connected = _ops_ctl(r, "info")["slack_connected"]
        logins = set(_ops_ctl(r, "users"))
    except AnnexError:
        connected, logins = True, None  # ops isn't reachable: can't tell, so don't warn
    if logins is not None:
        unknown = [x for x in dict.fromkeys([m.owner if "owner" in m.alerts_to else None,
                                             *(a for a in m.alerts_to if a != "owner" and not a.startswith("#"))])
                   if x and x not in logins]
        if unknown:
            out.append(f"alerts: {', '.join(unknown)} isn't in the user directory, so they won't be emailed when "
                       f"{m.name} has a problem; admins are alerted instead. Fix the owner or alerts.to in annex.yaml")
    channels = [a for a in m.alerts_to if a.startswith("#")]
    if channels and not connected:
        out.append(f"alerts.to lists {', '.join(channels)} but Slack isn't connected yet, so those alerts go by email "
                   "only (docs/setup.md §7)")
    if m.notify_slack:
        if not connected:
            out.append(f"notify.slack is declared ({', '.join(m.notify_slack)}) but Slack isn't connected on this "
                       "Annex yet, so Slack posts fail with a clear error until the platform team connects it "
                       "(docs/setup.md §7). Email works either way")
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
                         f"Check it with: ssh {r.host} docker logs {OPS_CONTAINER}") from None


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
            "image": x["image"], "image_kept": x["image"] in images,
            "can_roll_back_to": x["result"] == "live" and not current and x["image"] in images,
            "data_snapshot": x["release"] in data_from, "note": note,
        })
    return out


def rollback(app: str, to: int | None = None, with_data: bool = False, progress: Progress = _quiet,
             via: str = "cli") -> dict:
    """Switch an app back to an earlier release (see _rollback). A data restore holds the app's alerts while the
    container is stopped (annex_vm opens the maintenance window); it ends once the app is back."""
    _name(app)
    try:
        return _rollback(app, to, with_data, progress, via)
    finally:
        if with_data:
            _maintenance(Remote(), app, None)


def _maintenance(r: Remote, app: str, minutes: int | None) -> None:
    """Hold (or release, with None) alerts for an app. Best effort: a failure must never block the operation."""
    args = ["--minutes", str(minutes)] if minutes else ["--end"]
    r.run(f"docker exec {OPS_CONTAINER} python -m annex_ops.ctl maintenance app:{_name(app)} {' '.join(args)} --json", check=False)


def _rollback_target(r: Remote, app: str, rels: list[dict], to: int | None, with_data: bool) -> tuple[dict, dict]:
    """(the live release, the one to switch to), or AnnexError saying why not. The live release itself is a target
    only when the app's container isn't running its image (a deploy died after its switch; reconcile says so, D92)."""
    live = _live(rels)
    if not live:
        raise AnnexError(f"{app} has no releases to roll back to. See: annex releases {app}")
    images = _images(r, app)
    targets = [x for x in rels if x["result"] == "live" and x["release"] != live["release"] and x["image"] in images]
    listing = ", ".join(f"r{x['release']}" for x in targets) or "none"
    if to is None:
        earlier = [x for x in targets if x["release"] < live["release"]]
        if not earlier:
            raise AnnexError(f"{app} has no earlier release to roll back to (r{live['release']} is live; "
                             f"possible targets: {listing}). See: annex releases {app}")
        target = earlier[-1]
    elif to == live["release"] and live["image"] in images and _running_image(r, app) != live["image"]:
        target = live  # put the live release back in place of a container a dead deploy left
    else:
        target = next((x for x in targets if x["release"] == to), None)
        if not target:
            raise AnnexError(f"Can't roll {app} back to r{to}. Possible targets: {listing} (releases that went live, "
                             f"aren't current and whose image is kept). See: annex releases {app}")
    t = target["release"]
    if with_data and t not in {s["from_release"] for s in r.vm("snapshots", app)["snapshots"]}:
        raise AnnexError(f"There's no data snapshot from while r{t} was live (snapshots are taken before each "
                         f"deploy, and the last {KEEP_RELEASES} are kept). Roll back without --with-data, or restore a "
                         f"nightly backup with scripts/restore.sh {app}")
    return live, target


def _running_image(r: Remote, app: str) -> str:
    return r.out(f"docker inspect -f '{{{{.Config.Image}}}}' {container(app)} 2>/dev/null", check=False).strip()


def _rollback(app: str, to: int | None = None, with_data: bool = False, progress: Progress = _quiet,
              via: str = "cli") -> dict:
    """Switch an app back to an earlier release (default: the one before the current), re-applying that
    release's manifest. with_data also restores the database and /data snapshot from that release's time,
    losing writes since (human only)."""
    _name(app)
    r = Remote()
    live, target = _rollback_target(r, app, _releases(r, app), to, with_data)  # refuse a bad target before locking
    tm = mf.Manifest.from_dict(target["manifest"])
    line = {"image": target["image"], "manifest": target["manifest"], "deployed_at": _now(), "deployed_by": _who(),
            "via": via, "source": target.get("source"), "git": target.get("git"), "snapshot": None,
            "rollback": {"from": live["release"], "to": target["release"], "with_data": with_data}}
    with _Lock(r, app, line, _record_for(r, tm, None, None), progress) as lock:
        now_live = _live(_releases(r, app))
        if not now_live or now_live["release"] != live["release"]:
            raise AnnexError(f"{app} changed while this rollback was starting (another deploy finished: "
                             f"r{now_live and now_live['release']} is live now). See annex releases {app} and run it again")
        return _rollback_locked(r, app, live, target, with_data, lock, progress)


def _rollback_locked(r: Remote, app: str, live: dict, target: dict, with_data: bool, lock: _Lock,
                     progress: Progress) -> dict:
    n, t = lock.n, target["release"]
    lock.step("snapshot")
    progress(f"Snapshotting data before the switch (r{live['release']} is live)")
    snapshot = r.vm("snapshot", app, "--release", str(n), "--from-release", str(live["release"]))["snapshot"]
    rel: dict = {"snapshot": snapshot and snapshot["name"]}
    restored = None
    if with_data:
        lock.step("restore")
        progress(f"Restoring the database and /data from while r{t} was live")
        try:
            restored = r.vm("restore-snapshot", app, "--from-release", str(t))
        except AnnexError:
            r.run(f"docker start {container(app)} >/dev/null 2>&1", check=False)
            raise
    tm = mf.Manifest.from_dict(target["manifest"])
    cm = mf.Manifest.from_dict(live["manifest"])

    failed, _ = _activate(r, tm, target["image"], n, progress, lock.step)
    if failed:
        progress(f"r{t} failed the health gate; switching back to r{live['release']}")
        if with_data and snapshot:
            lock.step("back-restore")
            r.vm("restore-snapshot", app, "--from-release", str(live["release"]))
        back, _ = _activate(r, cm, live["image"], live["release"], progress, lambda s: lock.step(f"back-{s}"))
        _reload_scheduler(r, progress, tm.schedule, cm.schedule)
        _write_record(r, cm, live["image"], live["release"])
        lock.end({**rel, "result": "failed", "error": failed[0], "rolled_back_to": live["release"]})
        outcome = (f"Switching back to r{live['release']} failed too ({back[0]}), so {app} is down."
                   if back else f"Annex switched back to r{live['release']}"
                   + (" and its data" if with_data else "") + ", which is live again.")
        raise _gate_error(app, n, failed, outcome)

    lock.step("finish")
    _reload_scheduler(r, progress, tm.schedule, cm.schedule)
    record = _write_record(r, tm, target["image"], n)
    cleanup_warnings = _cleanup(r, app, target["image"], progress)
    lock.end({**rel, "result": "live"})
    progress("Done")
    return {"app": app, "release": n, "rolled_back_from": live["release"], "rolled_back_to": t,
            "image": target["image"], "with_data": with_data, "restored": restored, "url": record["url"],
            "snapshot": rel["snapshot"], "warnings": list(lock.notes) + cleanup_warnings}


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
