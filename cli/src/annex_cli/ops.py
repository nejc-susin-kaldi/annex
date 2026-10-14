"""Annex operations. Shared by the CLI and the MCP server.

Each function returns plain data (dicts, strings) and raises AnnexError or
ManifestError with a message that says what to do next.
"""
from __future__ import annotations

import io
import json
import shlex
import shutil
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


def compose_for(m: mf.Manifest, r: Remote, image: str) -> dict:
    data_dir = r.data
    url = r.url_for(m.name)
    env = {"ANNEX_APP_NAME": m.name, "ANNEX_BASE_URL": url, **m.env}
    labels = {
        "com.kaldi.annex": "true",
        "com.kaldi.annex.app": m.name,
        "com.kaldi.annex.owner": m.owner,
        "com.kaldi.annex.type": m.type,
        "homepage.group": "Tools",
        "homepage.name": m.name,
        "homepage.description": f"{m.description} · owner: {m.owner}" if m.description else f"owner: {m.owner}",
        "homepage.icon": "mdi-toolbox-outline",
    }
    networks = ["edge"]
    svc: dict = {
        "build": {"context": "./src", "labels": {"com.kaldi.annex": "true", "com.kaldi.annex.app": m.name}},
        "image": image,
        "container_name": container(m.name),
        "restart": "unless-stopped",
        "user": f"{APP_UID}:{APP_UID}",
        "read_only": True,
        "tmpfs": ["/tmp:size=64m,mode=1777"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": "256m",
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
    if m.schedule:
        labels["ofelia.enabled"] = "true"
        for j in m.schedule:
            key = f"ofelia.job-exec.{m.name}-{j.name}"
            labels[f"{key}.schedule"] = mf.ofelia_schedule(j.cron)
            labels[f"{key}.command"] = shlex.join(j.command)
            labels[f"{key}.no-overlap"] = "true"
    svc["networks"] = networks
    return {
        "name": project(m.name),
        "services": {m.name: svc},
        "networks": {n: {"external": True, "name": f"annex-{n}"} for n in networks},
    }


def _record(r: Remote, app: str) -> dict | None:
    res = r.run(f"cat {r.data}/deploy/{app}/deploy.json", check=False)
    return json.loads(res.stdout) if res.returncode == 0 else None


def deploy(folder: Path, progress: Progress = _quiet) -> dict:
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
    d = f"{r.data}/deploy/{m.name}"
    progress("Uploading source")
    r.run(f"set -e; mkdir -p {d}; rm -rf {d}/src.new; mkdir {d}/src.new; tar -xzf - -C {d}/src.new; "
          f"rm -rf {d}/src; mv {d}/src.new {d}/src", input=_tar(folder))

    progress("Provisioning storage, data access and secrets")
    prov = r.vm("provision", m.name, *(["--postgres"] if m.postgres else []), *(["--files"] if m.files else []),
                "--views", ",".join(m.views), "--secrets", ",".join(m.secrets))

    image = f"annex-app/{m.name}:{time.strftime('%Y%m%d-%H%M%S')}"
    compose = yaml.safe_dump(compose_for(m, r, image), sort_keys=False)
    r.run(f"cat > {d}/compose.yaml", input=compose)

    progress("Building image on the VM")
    build = r.run(f"cd {d} && docker compose build 2>&1", check=False)
    if build.returncode != 0:
        tail = "\n".join(build.stdout.decode(errors="replace").splitlines()[-40:])
        raise AnnexError(f"Docker build failed for {m.name}. Fix the Dockerfile or code and deploy again.\n{tail}")
    user = r.out(f"docker image inspect -f '{{{{.Config.User}}}}' {image}").strip()
    if user in ("", "root", "0", "0:0"):
        raise AnnexError("The image runs as root. End the Dockerfile with a non-root user, e.g. "
                         "'RUN useradd -u 10001 app' and 'USER 10001' (the templates already do this)")

    progress("Starting container")
    r.run(f"cd {d} && docker compose up -d --remove-orphans 2>&1")

    if m.is_web:
        progress(f"Waiting for {m.healthcheck} to return 200")
        probe = f"http://{container(m.name)}:{m.port}{m.healthcheck}"
        ok = r.run(f"for i in $(seq 45); do "
                   f"docker exec {TRAEFIK} wget -q -O /dev/null -T 3 {shlex.quote(probe)} && exit 0; "
                   f"[ \"$(docker inspect -f '{{{{.State.Running}}}}' {container(m.name)})\" = true ] || break; "
                   f"sleep 2; done; exit 1", check=False)
        if ok.returncode != 0:
            logs = r.out(f"docker logs --tail 40 {container(m.name)} 2>&1", check=False)
            raise AnnexError(f"{m.name} did not become healthy: GET {m.healthcheck} on port {m.port} never "
                             f"returned 200 within 90 s. Make sure the app listens on 0.0.0.0:$PORT and "
                             f"{m.healthcheck} returns 200.\nLast log lines:\n{logs}")
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
    else:
        state = r.out(f"docker inspect -f '{{{{.State.Status}}}}' {container(m.name)}").strip()
        if state != "running":
            raise AnnexError(f"{m.name} container is {state}. The image needs a 'sleep' binary "
                             "(the python-job template has one)")

    if m.schedule or (previous and previous.get("schedule")):
        progress("Reloading the scheduler")
        r.run(f"docker restart {OFELIA} >/dev/null")

    record = {"name": m.name, "owner": m.owner, "type": m.type, "image": image, "url": r.url_for(m.name) if m.is_web else None,
              "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "postgres": m.postgres, "files": m.files,
              "views": m.views, "secrets": m.secrets, "schedule": [j.__dict__ for j in m.schedule]}
    r.run(f"cat > {d}/deploy.json", input=json.dumps(record, indent=2))
    # keep only the running image; trim dangling images and build cache
    r.run(f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' annex-app/{m.name} | grep -vx {shlex.quote(image)} "
          f"| xargs -r docker rmi >/dev/null 2>&1; docker image prune -f >/dev/null; "
          f"docker builder prune -f --max-used-space 1500mb >/dev/null", check=False)
    progress("Done")
    return {**record, "injected_env": sorted({"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", *prov["injected"],
                                              *(["DATA_DIR"] if m.files else []),
                                              *(["ANNEX_GATEWAY_URL"] if m.views else [])} - ({"PORT"} if not m.is_web else set()))}


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
        info.update({k: record[k] for k in ("image", "deployed_at", "postgres", "files", "views", "secrets", "schedule")})
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
        r.vm("provision", app, *(["--postgres"] if record["postgres"] else []), *(["--files"] if record["files"] else []),
             "--views", ",".join(record["views"]), "--secrets", ",".join(record["secrets"]))
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
