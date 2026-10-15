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

from . import manifest as mf
from .remote import REPO, AnnexError, Remote

TEMPLATES = REPO / "templates"
CONTRACT = Path(__file__).with_name("contract.md")
APP_UID = 10001
TRAEFIK = "annex-traefik-1"
OFELIA = "annex-ofelia-1"
GATEWAY_URL = "http://gateway:3000"
OPS_URL = "http://ops:8000"
OPS_CONTAINER = "annex-ops-1"
HEALTH_TIMEOUT = int(os.environ.get("ANNEX_HEALTH_TIMEOUT", "60"))
KEEP_RELEASES = 3  # images and data snapshots kept per app
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache"}

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


def container(app: str) -> str:
    return f"annex-app-{app}"


def project(app: str) -> str:
    return f"annex-app-{app}"


# --- contract and init -----------------------------------------------------
def contract() -> str:
    return CONTRACT.read_text()


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


def compose_for(m: mf.Manifest, r: Remote, image: str, release: int) -> dict:
    data_dir = r.data
    url = r.url_for(m.name)
    env = {"ANNEX_APP_NAME": m.name, "ANNEX_BASE_URL": url, **m.env}
    labels = {
        "com.kaldi.annex": "true",
        "com.kaldi.annex.app": m.name,
        "com.kaldi.annex.owner": m.owner,
        "com.kaldi.annex.type": m.type,
        "com.kaldi.annex.release": str(release),
        "com.kaldi.annex.memory": f"{m.memory_mb}m",
        "homepage.group": "Tools",
        "homepage.name": m.name,
        "homepage.description": f"{m.description} · owner: {m.owner}" if m.description else f"owner: {m.owner}",
        "homepage.icon": "mdi-toolbox-outline",
    }
    networks = ["edge"]
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
        "env_file": [f"{data_dir}/secrets/apps/{m.name}/runtime.env"],
        "environment": env,
        "labels": labels,
    }
    if m.is_web:
        env["PORT"] = str(m.port)
        labels.update({
            "traefik.enable": "true",
            f"traefik.http.routers.app-{m.name}.rule": f"Host(`{r.host_for(m.name)}`)",
            f"traefik.http.services.app-{m.name}.loadbalancer.server.port": str(m.port),
            "homepage.href": url,
        })
    else:
        # job-only apps idle between runs; jobs are executed inside this container
        svc["command"] = ["sleep", "infinity"]
    if m.postgres:
        networks.append("appdata")
    if m.files:
        env["DATA_DIR"] = "/data"
        svc["volumes"] = [f"{data_dir}/apps/{m.name}/data:/data"]
    if m.views:
        env["ANNEX_GATEWAY_URL"] = GATEWAY_URL
    if m.notify_email or m.notify_slack:
        env["ANNEX_OPS_URL"] = OPS_URL  # the token is in the env file
    if m.schedule:
        labels["ofelia.enabled"] = "true"
        for j in m.schedule:
            key = f"ofelia.job-exec.{m.name}-{j.name}"
            labels[f"{key}.schedule"] = mf.ofelia_schedule(j.cron)
            labels[f"{key}.command"] = shlex.join(j.command)
            labels[f"{key}.no-overlap"] = "true"
            labels[f"{key}.user"] = str(APP_UID)  # Ofelia's job-exec defaults to root
    svc["networks"] = networks
    return {
        "name": project(m.name),
        "services": {m.name: svc},
        "networks": {n: {"external": True, "name": f"annex-{n}"} for n in networks},
    }


def _record(r: Remote, app: str) -> dict | None:
    res = r.run(f"cat {r.data}/deploy/{app}/deploy.json", check=False)
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
# Each deploy or rollback appends one line to /srv/annex/apps/<app>/releases.jsonl:
# release number, image, manifest, who/when/where from, data snapshot and result
# ("live", or "failed" when it didn't pass the health gate).
def _releases(r: Remote, app: str) -> list[dict]:
    res = r.run(f"cat {r.data}/apps/{app}/releases.jsonl", check=False)
    if res.returncode != 0:
        return []
    return [json.loads(line) for line in res.stdout.decode().splitlines() if line.strip()]


def _append_release(r: Remote, app: str, rel: dict) -> None:
    r.run(f"mkdir -p {r.data}/apps/{app} && cat >> {r.data}/apps/{app}/releases.jsonl",
          input=json.dumps(rel, default=str) + "\n")


def _live(releases: list[dict]) -> dict | None:
    """The release that's running now: the last one that went live."""
    live = [x for x in releases if x["result"] == "live"]
    return live[-1] if live else None


def _images(r: Remote, app: str) -> set[str]:
    return set(r.out(f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' annex-app/{app}").split())


def _build(r: Remote, m: mf.Manifest, image: str) -> None:
    d = f"{r.data}/deploy/{m.name}"
    build = r.run(f"docker build --progress=plain --label com.kaldi.annex=true --label com.kaldi.annex.app={m.name} "
                  f"-t {image} {d}/src 2>&1", check=False)
    if build.returncode != 0:
        tail = "\n".join(build.stdout.decode(errors="replace").splitlines()[-40:])
        raise AnnexError(f"Docker build failed for {m.name}. Fix the Dockerfile or code and deploy again. "
                         f"The running version is unchanged.\n{tail}")
    user = r.out(f"docker image inspect -f '{{{{.Config.User}}}}' {image}").strip()
    if user in ("", "root", "0", "0:0"):
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        raise AnnexError("The image runs as root. End the Dockerfile with a non-root user, e.g. "
                         "'RUN useradd -u 10001 app' and 'USER 10001' (the templates already do this)")


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
                    notify_email: bool = False, notify_slack: list[str] | None = None) -> list[str]:
    """Arguments for `annex_vm.py provision`. Records written before M1.2 have no notify keys: no grant."""
    return [app, *(["--postgres"] if postgres else []), *(["--files"] if files else []),
            "--views", ",".join(views), "--secrets", ",".join(secrets),
            *(["--notify-email"] if notify_email else []),
            *(["--slack", ",".join(notify_slack)] if notify_slack else [])]


def _activate(r: Remote, m: mf.Manifest, image: str, release: int, progress: Progress
              ) -> tuple[tuple[str, str] | None, dict]:
    """Provision what manifest `m` asks for, switch the app to `image` and run the health gate.
    Returns (gate failure or None, provision result)."""
    d = f"{r.data}/deploy/{m.name}"
    progress("Provisioning storage, data access and secrets")
    prov = r.vm("provision", *_provision_args(m.name, postgres=m.postgres, files=m.files, views=m.views,
                                              secrets=m.secrets, notify_email=m.notify_email,
                                              notify_slack=m.notify_slack))
    compose = yaml.safe_dump(compose_for(m, r, image, release), sort_keys=False)
    r.run(f"mkdir -p {d} && cat > {d}/compose.yaml", input=compose)
    progress(f"Starting r{release} ({image})")
    r.run(f"cd {d} && docker compose up -d --remove-orphans 2>&1")
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
    record = {"name": m.name, "owner": m.owner, "type": m.type, "release": release, "image": image,
              "url": r.url_for(m.name) if m.is_web else None, "deployed_at": _now(), "postgres": m.postgres,
              "files": m.files, "views": m.views, "secrets": m.secrets, "schedule": [j.__dict__ for j in m.schedule],
              "memory_mb": m.memory_mb, "notify_email": m.notify_email, "notify_slack": m.notify_slack}
    r.run(f"cat > {r.data}/deploy/{m.name}/deploy.json", input=json.dumps(record, indent=2))
    return record


def _cleanup(r: Remote, app: str) -> None:
    """Keep the images of the last KEEP_RELEASES live releases and as many snapshots; trim build cache."""
    keep: list[str] = []
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

    previous = _record(r, m.name)
    releases = _releases(r, m.name)
    live = _live(releases)
    n = max((x["release"] for x in releases), default=0) + 1
    image = f"annex-app/{m.name}:r{n}"
    d = f"{r.data}/deploy/{m.name}"
    progress("Uploading source")
    r.run(f"set -e; mkdir -p {d}; rm -rf {d}/src.new; mkdir {d}/src.new; tar -xzf - -C {d}/src.new; "
          f"rm -rf {d}/src; mv {d}/src.new {d}/src", input=_tar(folder))

    progress(f"Building r{n} on the VM")
    _build(r, m, image)

    snapshot = None
    if live:
        progress(f"Snapshotting data before the switch (r{live['release']} is live)")
        snapshot = r.vm("snapshot", m.name, "--release", str(n), "--from-release", str(live["release"]))["snapshot"]
    rel = {"release": n, "image": image, "manifest": m.to_dict(), "deployed_at": _now(), "deployed_by": _who(),
           "via": via, "source": str(folder).replace(str(Path.home()), "~", 1), "git": _git(folder),
           "snapshot": snapshot and snapshot["name"]}

    failed, prov = _activate(r, m, image, n, progress)
    if failed:
        rel.update(result="failed", error=failed[0])
        if not live:
            _append_release(r, m.name, rel)
            raise _gate_error(m.name, n, failed, f"There's no earlier release to roll back to, so r{n} is left as "
                              f"is. Fix the app and deploy again; see its logs with: annex logs {m.name}")
        progress(f"r{n} failed the health gate; rolling back to r{live['release']}")
        prev = mf.Manifest.from_dict(live["manifest"])
        back, _ = _activate(r, prev, live["image"], live["release"], progress)
        _reload_scheduler(r, progress, m.schedule, prev.schedule)
        _write_record(r, prev, live["image"], live["release"])
        _append_release(r, m.name, {**rel, "rolled_back_to": live["release"]})
        r.run(f"docker rmi {image} >/dev/null 2>&1", check=False)
        if back:
            raise _gate_error(m.name, n, failed, f"Rolling back to r{live['release']} failed too ({back[0]}), "
                              f"so {m.name} is down. Check: annex logs {m.name}")
        raise _gate_error(m.name, n, failed, f"Annex rolled back to r{live['release']}, which is live again. "
                          "Fix the app and deploy again.")

    _reload_scheduler(r, progress, m.schedule, previous and previous.get("schedule"))
    record = _write_record(r, m, image, n)
    _append_release(r, m.name, {**rel, "result": "live"})
    _cleanup(r, m.name)
    progress("Done")
    warnings = _warnings(r, m)
    return {**record, "warnings": warnings, "previous_release": live and live["release"], "snapshot": rel["snapshot"],
            "injected_env": sorted({"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", *prov["injected"],
                                    *(["DATA_DIR"] if m.files else []),
                                    *(["ANNEX_GATEWAY_URL"] if m.views else []),
                                    *(["ANNEX_OPS_URL"] if m.notify_email or m.notify_slack else [])}
                                  - ({"PORT"} if not m.is_web else set()))}


def _warnings(r: Remote, m: mf.Manifest) -> list[str]:
    """Things a successful deploy should still tell the builder. More are added here by later milestones."""
    out = []
    if m.memory_mb > mf.MEMORY_DEFAULT_MB:
        out.append(f"{m.name} runs with {m.memory_mb}m of memory instead of the default {mf.MEMORY_DEFAULT_MB}m. "
                   "That's fine if it needs it; the platform team sees raised limits in annex list")
    if m.notify_slack:
        try:
            connected = _ops_ctl(r, "info")["slack_connected"]
        except AnnexError:
            connected = True  # ops isn't reachable: can't tell, so don't warn
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
        raise AnnexError(f"annex-ops isn't answering ({detail or 'is the container running?'}). "
                         f"Check it with: ssh {r.host} docker logs {OPS_CONTAINER}") from None


def sends(app: str | None = None, limit: int = 50) -> list[dict]:
    """Email and Slack send attempts, newest first (every attempt, rejections too; never a body)."""
    return _ops_ctl(Remote(), "sends", *(["--app", app] if app else []), "--limit", str(max(1, min(int(limit), 500))))


def releases(app: str) -> list[dict]:
    """An app's releases, newest first, with which can be rolled back to (and with data)."""
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
    """Switch an app back to an earlier release (default: the one before the current), re-applying that
    release's manifest. with_data also restores the database and /data snapshot from that release's time,
    losing writes since (human only)."""
    r = Remote()
    rels = _releases(r, app)
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

    n = max(x["release"] for x in rels) + 1
    progress(f"Snapshotting data before the switch (r{live['release']} is live)")
    snapshot = r.vm("snapshot", app, "--release", str(n), "--from-release", str(live["release"]))["snapshot"]
    restored = None
    if with_data:
        progress(f"Restoring the database and /data from while r{t} was live")
        try:
            restored = r.vm("restore-snapshot", app, "--from-release", str(t))
        except AnnexError:
            r.run(f"docker start {container(app)} >/dev/null 2>&1", check=False)
            raise
    tm = mf.Manifest.from_dict(target["manifest"])
    cm = mf.Manifest.from_dict(live["manifest"])
    rel = {"release": n, "image": target["image"], "manifest": target["manifest"], "deployed_at": _now(),
           "deployed_by": _who(), "via": via, "source": target.get("source"), "git": target.get("git"),
           "snapshot": snapshot and snapshot["name"],
           "rollback": {"from": live["release"], "to": t, "with_data": with_data}}

    failed, _ = _activate(r, tm, target["image"], n, progress)
    if failed:
        progress(f"r{t} failed the health gate; switching back to r{live['release']}")
        if with_data and snapshot:
            r.vm("restore-snapshot", app, "--from-release", str(live["release"]))
        back, _ = _activate(r, cm, live["image"], live["release"], progress)
        _reload_scheduler(r, progress, tm.schedule, cm.schedule)
        _write_record(r, cm, live["image"], live["release"])
        _append_release(r, app, {**rel, "result": "failed", "error": failed[0], "rolled_back_to": live["release"]})
        outcome = (f"Switching back to r{live['release']} failed too ({back[0]}), so {app} is down."
                   if back else f"Annex switched back to r{live['release']}"
                   + (" and its data" if with_data else "") + ", which is live again.")
        raise _gate_error(app, n, failed, outcome)

    _reload_scheduler(r, progress, tm.schedule, cm.schedule)
    record = _write_record(r, tm, target["image"], n)
    _append_release(r, app, {**rel, "result": "live"})
    _cleanup(r, app)
    progress("Done")
    return {"app": app, "release": n, "rolled_back_from": live["release"], "rolled_back_to": t,
            "image": target["image"], "with_data": with_data, "restored": restored, "url": record["url"],
            "snapshot": rel["snapshot"]}


# --- inspect ---------------------------------------------------------------
def _containers(r: Remote, app: str | None = None) -> list[dict]:
    flt = f"label=com.kaldi.annex.app={app}" if app else "label=com.kaldi.annex.app"
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
    info["recent_logs"] = r.out(f"docker logs --tail 15 {container(app)} 2>&1", check=False).splitlines()
    return info


def logs(app: str, tail: int = 100, since: str | None = None) -> str:
    r = Remote()
    if not _containers(r, app):
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    s = f" --since {shlex.quote(since)}" if since else ""
    return r.out(f"docker logs --timestamps --tail {int(tail)}{s} {container(app)} 2>&1")


def follow_logs(app: str, tail: int = 100) -> int:
    return Remote().stream(f"docker logs --timestamps --tail {int(tail)} -f {container(app)}")


def run_job(app: str, job: str) -> dict:
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
    for p in pairs:
        k, sep, _ = p.partition("=")
        if not sep or not mf.KEY_RE.match(k):
            raise AnnexError(f"{k!r}: use KEY=VALUE with an UPPER_SNAKE_CASE key")
    r = Remote()
    res = r.vm("secret-set", app, input="\n".join(pairs) + "\n")
    record = _record(r, app)
    if record:
        undeclared = [k for k in res["set"] if k not in record.get("secrets", [])]
        r.vm("provision", *_provision_args(
            app, postgres=record["postgres"], files=record["files"], views=record["views"], secrets=record["secrets"],
            notify_email=record.get("notify_email", False), notify_slack=record.get("notify_slack", [])))
        r.run(f"cd {r.data}/deploy/{app} && docker compose up -d 2>&1")
        res["applied"] = "restarted with new values"
        if undeclared:
            res["warning"] = (f"{', '.join(undeclared)} not declared under secrets: in annex.yaml, "
                              "so not injected. Add them there and deploy again")
    else:
        res["applied"] = "stored; injected on the next deploy"
    return res


def remove(app: str, keep_data: bool = False) -> dict:
    r = Remote()
    record = _record(r, app)
    if not record and not _containers(r, app):
        raise AnnexError(f"No app named {app!r}. See deployed apps with: annex list")
    d = f"{r.data}/deploy/{app}"
    r.run(f"if [ -f {d}/compose.yaml ]; then cd {d} && docker compose down --remove-orphans 2>&1; "
          f"else docker rm -f {container(app)} >/dev/null 2>&1 || true; fi; "
          f"docker image ls -q annex-app/{app} | xargs -r docker rmi -f >/dev/null 2>&1 || true")
    res = r.vm("deprovision", app, *(["--keep-data"] if keep_data else []))
    if record and record.get("schedule"):
        r.run(f"docker restart {OFELIA} >/dev/null")
    return res
