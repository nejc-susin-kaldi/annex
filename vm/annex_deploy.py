"""The deploy and rollback sequence, run on the VM as one process (control-plane V1, D101).

`annex_vm.py deploy <app>` and `annex_vm.py rollback <app>` call serve(). The CLI on the builder's machine validates
the manifest, then makes one call: a JSON meta line on stdin (and, for a deploy, the source as tar.gz after it). This
process takes the app's lock (an flock held for its whole run), settles a release an earlier run left pending,
numbers the new release, receives the source, then forks a worker that does the rest (build, snapshot, provision,
switch, health gate, cleanup) in its own session, so it finishes even if the SSH session goes away. Every progress
line goes to deploy/<app>/run.log; the parent forwards them to the caller while it is there.

Stdout protocol: one JSON object per line. {"progress": "...", "step": "build"}; {"note": "..."} (reconcile
messages, warnings); {"heartbeat": "<step>"} every 30 s of silence; and last, exactly one {"ok": true, ...the result}
or {"ok": false, "error": "...", "step": "..."}.

Stdlib only (the stock python3 on the VM). The manifest and log-convention modules are the CLI's own files, copied
into the bundle as annex_manifest.py and annex_logcheck.py by scripts/render.py; in the repo checkout (unit tests)
they are loaded from cli/src/annex_cli.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
import zlib
from datetime import datetime
from pathlib import Path
from typing import Callable

import annex_vm as vm


def _module(name: str, filename: str):
    """The bundle's copy (annex_manifest, annex_logcheck), else the CLI's source file in the repo checkout."""
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    path = Path(__file__).resolve().parents[1] / "cli" / "src" / "annex_cli" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{name}: neither in the bundle nor at {path}; run scripts/push.sh")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # before exec: dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


mf = _module("annex_manifest", "manifest.py")
logcheck = _module("annex_logcheck", "logcheck.py")

APP_UID = vm.APP_UID
TRAEFIK = vm.platform_container("traefik")
OFELIA = vm.OFELIA
OPS_CONTAINER = vm.platform_container("ops")
GATEWAY_URL = "http://gateway:3000"
OPS_URL = "http://ops:8000"
HEALTH_TIMEOUT = 60  # s; ANNEX_HEALTH_TIMEOUT in the meta's overrides changes it (tests)
BUILD_MIN_FREE_GB = 3  # D95: a build is refused with less than this free on the data disk
IMAGE_MAX_MB = 2048  # D95: a built image above this is refused and removed
APP_PIDS_LIMIT = 256  # D95: processes an app container may have (a fork bomb stays inside it)
KEEP_RELEASES = 3  # images and data snapshots kept per app
# what a caller may set for one deploy (the tests lower the limits); anything else in the meta's overrides is ignored
OVERRIDE_KEYS = ("ANNEX_BUILD_MIN_FREE_GB", "ANNEX_IMAGE_MAX_MB", "ANNEX_HEALTH_TIMEOUT")
HEARTBEAT_S = 30
POLL_S = 0.2

# seams for the tests (D96): every process goes through vm.run; time through these
sleep = time.sleep
monotonic = time.monotonic


class Terminated(BaseException):
    """SIGTERM: the run records its release as failed at the step it reached and stops."""


def _terminate(signum, frame):
    raise Terminated()


def _now() -> str:
    # milliseconds: the audit trail orders by this time (D97)
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def container(app: str) -> str:
    return f"annex-app-{app}"


# --- one run: its lock-holder record, its output, its release --------------------------------------------------
class Run:
    """One deploy or rollback of one app, from the lock to the result. Holds what current.json says, writes the
    protocol lines (run.log always; the caller's stdout while it is there) and ends the release."""

    def __init__(self, base: Path, app: str, kind: str, actor: str, via: str, meta: dict, out,
                 settings: dict | None = None):
        self.base, self.app, self.kind, self.actor, self.via, self.meta, self.out = base, app, kind, actor, via, meta, out
        self.settings = vm.read_kv(base / "platform" / ".env") if settings is None else settings
        if not self.settings.get("ANNEX_HOST_PATTERN"):
            raise vm.Fail(f"No Annex platform settings at {base}/platform/.env. Run scripts/push.sh first")
        self.overrides = overrides(meta)
        self.d = base / "deploy" / app
        self.d.mkdir(parents=True, exist_ok=True)
        self.log_path = self.d / "run.log"
        if self.log_path.exists():
            self.log_path.replace(self.d / "run.log.1")
        self.log = open(self.log_path, "a", encoding="utf-8")
        self.at, self.n, self.image, self.ended = "begin", None, None, False
        self.line: dict = {}
        self.notes: list[str] = []  # what settling found: shown at once and returned with the result
        self.inspect = vm.container_state
        now = time.time()
        self.current = {"pid": os.getpid(), "actor": actor, "via": via, "kind": kind, "release": None, "image": None,
                        "step": "begin", "step_at": now, "started_at": now, "started": vm._iso(now)}

    # output
    def emit(self, obj: dict) -> None:
        text = json.dumps(obj, default=str) + "\n"
        self.log.write(text)
        self.log.flush()
        self._forward(text)

    def _forward(self, text: str) -> bool:
        """Write a line to the caller; False once the caller has gone (from then on only run.log gets lines)."""
        if self.out is None:
            return False
        try:
            self.out.write(text if text.endswith("\n") else text + "\n")
            self.out.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.out = None
            return False

    def progress(self, msg: str) -> None:
        self.emit({"progress": msg, "step": self.at})

    def note(self, msg: str) -> None:
        self.emit({"note": msg})

    # where the run is
    def write_current(self) -> None:
        vm.write_current(self.base, self.app, self.current)

    def step(self, name: str) -> None:
        self.at = name
        self.current.update(step=name, step_at=time.time())
        self.write_current()

    # settings
    def host_for(self, app: str) -> str:
        return self.settings["ANNEX_HOST_PATTERN"].replace("{name}", app)

    def url_for(self, app: str) -> str:
        return f"https://{self.host_for(app)}{self.settings.get('ANNEX_PORT_SUFFIX', '')}"

    # the release
    def settle(self) -> None:
        notes, done = vm.settle(self.base, self.app, self.current, inspect=self.inspect)
        found = notes + [x["message"] for x in done] + vm.after_reconcile(self.app, done)
        for msg in found:
            self.notes.append(msg)
            self.note(msg)

    def open(self, line: dict, record: dict | None) -> None:
        self.n, self.image = vm.open_release(self.base, self.app, line, record, self.current)
        self.line = {**line, "release": self.n, "image": self.image}

    def end(self, rel: dict) -> dict:
        final = vm.end(self.base, self.app, self.n, rel["result"], {**self.line, **rel})
        self.ended = True
        self.current.update(result=rel["result"], ended_at=time.time())
        self.write_current()
        warning = vm.record_release_db(self.app, final)
        if warning:
            self.note(f"Warning: {warning}")
        return final

    def fail(self, e: BaseException) -> None:
        """The run stops on `e`: a pending release is recorded failed at the step it reached (the old CLI lock's
        exit rule, D92), and the last line says why."""
        if isinstance(e, Terminated):
            msg = f"the {self.kind} process on the VM was stopped (SIGTERM) at the {self.at} step"
        elif isinstance(e, KeyboardInterrupt):
            msg = f"the {self.kind} process on the VM was interrupted at the {self.at} step"
        elif isinstance(e, vm.Fail):
            msg = str(e)
        elif isinstance(e, subprocess.CalledProcessError):
            detail = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            msg = f"{shlex.join(map(str, e.cmd))[:200]} failed (exit {e.returncode}): {detail.strip()[-400:]}"
        else:
            traceback.print_exc()
            msg = (f"{type(e).__name__}: {e} (a bug in Annex; deploy/{self.app}/run.err on the VM has the "
                   "traceback)")
        if self.n is not None and not self.ended:
            first = (msg.strip().splitlines() or [type(e).__name__])[0]
            try:
                self.end({"result": "failed", "error": f"broke off at {self.at}: {first}"[:300],
                          "interrupted_at": self.at})
            except BaseException:  # noqa: BLE001  the release stays pending: the next run reconciles it
                traceback.print_exc()
        elif not self.ended:
            self.current.update(result="refused", ended_at=time.time())
            try:
                self.write_current()
            except OSError:
                pass
        self.emit({"ok": False, "error": msg, "step": self.at})

    # the caller's side after the fork
    def follow(self, pid: int, offset: int) -> int:
        """Forward the worker's lines from run.log to the caller until its result; a heartbeat every HEARTBEAT_S of
        silence. Returns the exit code. A caller that has gone ends this quietly (the worker carries on)."""
        buf, beat, gone = "", monotonic(), False
        with open(self.log_path, encoding="utf-8") as rd:
            rd.seek(offset)
            while True:
                chunk = rd.read()
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict) and obj.get("step"):
                            self.at = obj["step"]
                        if not self._forward(line):
                            return 0
                        beat = monotonic()
                        if isinstance(obj, dict) and "ok" in obj:
                            _reap(pid)
                            return 0 if obj["ok"] else 1
                    continue
                if gone:
                    break
                if _reap(pid):
                    gone = True  # read once more: its last lines may have landed after the previous read
                    continue
                if monotonic() - beat >= HEARTBEAT_S:
                    if not self._forward(json.dumps({"heartbeat": self.at})):
                        return 0
                    beat = monotonic()
                sleep(POLL_S)
        step = (vm.read_current(self.base, self.app) or {}).get("step") or self.at
        self.emit({"ok": False, "step": step,
                   "error": f"the {self.kind} process on the VM ended at the {step} step without a result (killed?); "
                            f"r{self.n} of {self.app} is pending: the next deploy or rollback, or `annex_vm.py "
                            f"deploy-reconcile {self.app}` on the VM, settles it"})
        return 1


def _reap(pid: int) -> bool:
    """True once the worker has exited (and is reaped)."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] != 0
    except ChildProcessError:
        return True


def overrides(meta: dict) -> dict[str, str]:
    raw = meta.get("overrides") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in OVERRIDE_KEYS and v is not None and str(v).strip()}


# --- the entry point and the detach ------------------------------------------------------------------------------
def read_meta(stdin) -> dict:
    raw = stdin.readline()
    try:
        meta = json.loads(raw or b"{}")
    except ValueError:
        meta = None
    if not isinstance(meta, dict):
        raise vm.Fail("the first line on stdin must be the run's JSON meta ({\"line\": {...}}); is the CLI current?")
    return meta


# --- version coupling (D103) -------------------------------------------------------------------------------------
def parse_version_stamp(text: str) -> dict:
    """platform/VERSION (written by scripts/render.py): key=value lines; {} if there are none."""
    out: dict = {}
    for line in (text or "").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            out[k] = v
    return out


def platform_versions(base: Path) -> dict:
    """{"commit", "dirty", "contract": int|None} of the pushed platform; {} when there is no VERSION (unit tests, a
    platform pushed before the stamp)."""
    try:
        stamp = parse_version_stamp((base / "platform" / "VERSION").read_text())
    except OSError:
        return {}
    contract = stamp.get("contract", "")
    return {"commit": stamp.get("commit", ""), "dirty": stamp.get("dirty") == "true",
            "contract": int(contract) if contract.isdigit() else None}


def _short(commit) -> str:
    return (commit or "unknown")[:12]


def check_versions(cli: dict | None, platform: dict) -> tuple[str | None, list[str]]:
    """(refusal, notes): may this CLI deploy to this platform? Pure.

    cli is the meta's {"commit", "dirty", "contract_version"} (None: a CLI from before version reporting), platform
    is platform_versions(). The rule is on the contract version only: a CLI whose contract is newer than the
    platform's is refused (it would deploy apps that follow rules the platform doesn't know yet: push the platform
    first); an older one deploys with a note; commits only appear in the messages (two checkouts differ in commits
    all the time). A missing number on either side checks nothing."""
    if not isinstance(cli, dict):
        return None, ["your CLI doesn't report its version (it predates the version check): update your checkout"]
    mine, theirs = cli.get("contract_version"), platform.get("contract")
    if not isinstance(mine, int) or isinstance(mine, bool) or theirs is None:
        return None, []
    who = (f"the CLI is at {_short(cli.get('commit'))}{' (modified)' if cli.get('dirty') else ''}, "
           f"the platform at {_short(platform.get('commit'))}{' (modified)' if platform.get('dirty') else ''}")
    if mine > theirs:
        return (f"your CLI is newer than the platform (contract v{mine} > v{theirs}; {who}). Nothing was deployed. "
                "Push the platform first: scripts/push.sh", [])
    if mine < theirs:
        return None, [f"your CLI is older than the platform (contract v{mine} < v{theirs}; {who}): update your checkout"]
    return None, []


def revalidate_source(run: Run, line: dict) -> None:
    """The uploaded annex.yaml is validated here with the CLI's own module (the bundle's annex_manifest.py), so a
    modified or stale CLI can't skip a rule, and it must equal the manifest the CLI sent (that one is what the release
    line records). Same messages as the CLI gives."""
    src = run.d / "src"
    try:
        m = mf.load(src)
    except mf.ManifestError as e:
        raise vm.Fail(f"annex.yaml in the upload is not valid on the VM ({len(e.problems)} problem(s)):\n"
                      + "\n".join(f"  - {p}" for p in e.problems)
                      + "\nFix annex.yaml, or run: annex contract (and update your checkout if the CLI accepted it)") from None
    except ImportError as e:  # PyYAML missing on the VM: a platform problem, not the builder's
        raise vm.Fail(f"the VM can't read annex.yaml ({e}): install python3-yaml on the VM") from None
    sent = json.loads(json.dumps(line.get("manifest")))
    mine = json.loads(json.dumps(m.to_dict()))
    if mine != sent:
        keys = sorted(k for k in set(mine) | set(sent or {}) if mine.get(k) != (sent or {}).get(k))
        raise vm.Fail("the manifest in the upload doesn't match what the CLI validated "
                      f"(differs in: {', '.join(keys)}). Nothing was built. Deploy again from an unchanged folder; "
                      "if it keeps happening, update your checkout")


def serve(kind: str, a, stdin=None, stdout=None, base: Path | None = None) -> int:
    """annex_vm.py deploy|rollback. Refusals before the lock (bad meta, the lock held) raise vm.Fail, which
    annex_vm prints as the one {"ok": false} line."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout
    base = base if base is not None else vm.DATA
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a dropped session must not end the upload half-way silently
    signal.signal(signal.SIGTERM, _terminate)
    meta = read_meta(stdin)
    fd = vm.hold_app_lock(base, a.app)
    try:
        run = Run(base, a.app, kind, a.actor, a.via, meta, stdout)
    except BaseException:
        os.close(fd)
        raise
    try:
        job = (prepare_deploy if kind == "deploy" else prepare_rollback)(run, stdin, a)
    except BaseException as e:  # noqa: BLE001  refused or broken before the fork: one final line, lock freed
        run.fail(e)
        os.close(fd)
        return 1
    return detach(run, fd, job)


def detach(run: Run, fd: int, job: Callable[[], dict]) -> int:
    """Fork the worker that runs `job` in its own session (it keeps the lock's descriptor), then follow it."""
    for f in (sys.stdout, sys.stderr, run.log):
        f.flush()
    offset = run.log.tell()
    pid = os.fork()
    if pid == 0:
        _worker(run, job)  # never returns
    os.close(fd)  # the worker's copy holds the lock now
    return run.follow(pid, offset)


def _worker(run: Run, job: Callable[[], dict]) -> None:
    code = 0
    try:
        os.setsid()  # out of the SSH session's process group: a hangup there doesn't reach this process
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, _terminate)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        err = os.open(run.d / "run.err", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(err, 1)  # the SSH session's pipes are closed in this process: the session can end without it
        os.dup2(err, 2)
        os.close(err)
        run.out = None
        run.current["pid"] = os.getpid()
        run.write_current()
        try:
            res = job()
        except BaseException as e:  # noqa: BLE001
            run.fail(e)
            code = 1
        else:
            run.emit({"ok": True, **res})
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(code)


# --- what a release runs with ------------------------------------------------------------------------------------
def _compose_networks(app: str, egress: bool) -> tuple[list[str], dict]:
    """(the service's networks, the file's top-level networks): both external, created by networks-sync (D88)."""
    nets = {"app": {"external": True, "name": vm.app_network(app)}}
    if egress:
        nets["egress"] = {"external": True, "name": vm.egress_network(app)}
    return list(nets), nets


def rewire_compose(doc: dict, app: str, egress: bool) -> tuple[dict, dict]:
    """An app's compose file from before D88 (on annex-edge, and annex-appdata with Postgres) moved onto its own
    network, for an app whose source isn't at hand (migrate-networks, D102). Returns (new document, what the app
    uses: the flags for networks-sync), read from the file itself: routed = web, on appdata = Postgres, a gateway
    URL = views."""
    doc = json.loads(json.dumps(doc))  # a deep copy; the file is plain YAML data
    app_net = vm.app_network(app)
    services = doc.get("services") or {}
    if len(services) != 1:
        raise vm.Fail(f"{app}'s compose file has {len(services)} services; an Annex app has exactly one")
    svc = next(iter(services.values()))
    labels = svc.get("labels") or {}
    if not isinstance(labels, dict):  # Annex always writes a mapping; a list means someone edited the file
        raise vm.Fail(f"{app}'s compose file has its labels as a list; it wasn't written by Annex. Redeploy it from its source")
    env = svc.get("environment") or {}
    old = svc.get("networks") or []
    old = list(old) if isinstance(old, (list, dict)) else []
    old_names = {(doc.get("networks") or {}).get(k, {}).get("name", k) for k in old}
    uses = {"web": str(labels.get("traefik.enable", "")).lower() == "true",
            "postgres": bool(old_names & {"annex-appdata"}),
            "views": "ANNEX_GATEWAY_URL" in (env if isinstance(env, dict) else {}),
            "egress": egress}
    if uses["web"]:
        labels["traefik.docker.network"] = app_net
        svc["labels"] = labels
    svc["networks"], doc["networks"] = _compose_networks(app, egress)
    return doc, uses


def _tile_labels(m, fields: list[tuple[str, str, str | None]]) -> dict[str, str]:
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


def compose_for(run: Run, m, image: str, release: int) -> dict:
    data_dir = str(run.base)
    url = run.url_for(m.name)
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
        # built by build() before the switch; a rollback reuses an older image
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
            "traefik.docker.network": vm.app_network(m.name),
            f"traefik.http.routers.app-{m.name}.rule": f"Host(`{run.host_for(m.name)}`)",
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
        env["ANNEX_HOOKS_URL"] = f"{run.settings.get('ANNEX_URL_HOOKS', '')}/h/{m.name}"  # the token is in the env file
    labels["ofelia.enabled"] = "true" if m.schedule else "false"  # an image label can't turn the scheduler on (D83)
    for j in m.schedule:
        key = f"ofelia.job-exec.{m.name}-{j.name}"
        labels[f"{key}.schedule"] = mf.ofelia_schedule(j.cron)
        labels[f"{key}.command"] = shlex.join(j.command)
        labels[f"{key}.no-overlap"] = "true"
        labels[f"{key}.user"] = str(APP_UID)  # Ofelia's job-exec defaults to root
    svc["networks"], networks = _compose_networks(m.name, bool(m.egress))
    return {"name": container(m.name), "services": {m.name: svc}, "networks": networks}


def record_for(run: Run, m, image: str | None, release: int | None) -> dict:
    """deploy.json's content (read by status, run, secret set, remove, restore and networks-sync). The pending line
    carries it, so that a deploy that died after its gate can still be recorded as live (reconcile writes it then)."""
    return {"name": m.name, "owner": m.owner, "type": m.type, "release": release, "image": image,
            "url": run.url_for(m.name) if m.is_web else None, "deployed_at": _now(), "postgres": m.postgres,
            "files": m.files, "views": m.views, "secrets": m.secrets, "schedule": [j.__dict__ for j in m.schedule],
            "memory_mb": m.memory_mb, "notify_email": m.notify_email, "notify_slack": m.notify_slack,
            "alerts_to": m.alerts_to, "webhooks": [w.__dict__ for w in m.webhooks], "port": m.port,
            "egress": m.egress}


def write_record(run: Run, m, image: str, release: int) -> dict:
    record = record_for(run, m, image, release)
    vm.write_atomic(run.d / "deploy.json", json.dumps(record, indent=2))
    return record


def read_record(run: Run) -> dict | None:
    try:
        return json.loads((run.d / "deploy.json").read_text())
    except (OSError, ValueError):
        return None


def webhook_urls(run: Run, app: str, webhooks) -> list[dict]:
    """The public URL of each declared webhook (what the provider is told)."""
    base = run.settings.get("ANNEX_URL_HOOKS", "")
    return [{"name": w["name"], "verify": w["verify"], "url": f"{base}/h/{app}/{w['name']}"}
            for w in (x if isinstance(x, dict) else x.__dict__ for x in webhooks)]


# --- build (D83, D95) --------------------------------------------------------------------------------------------
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


def limit(run: Run, key: str, default: float) -> float:
    """A build limit: the caller's override first (tests), then the platform's .env (the VM owner), then the default."""
    raw = run.overrides.get(key) or run.settings.get(key) or ""
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise vm.Fail(f"{key}={raw!r} is not a number. Set it to a plain number in the platform's .env "
                      "(scripts/push.sh applies it) or unset it to use the default")
    return value


def health_timeout(run: Run) -> int:
    raw = run.overrides.get("ANNEX_HEALTH_TIMEOUT", "")
    if not raw:
        return HEALTH_TIMEOUT
    if not raw.isdigit():
        raise vm.Fail(f"ANNEX_HEALTH_TIMEOUT={raw!r} is not a number of seconds")
    return int(raw)


def _inspect(image: str, fmt: str, check: bool = False) -> str:
    r = vm.run(["docker", "image", "inspect", "-f", fmt, image], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise vm.Fail(f"docker image inspect {image} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _rmi(*images: str) -> None:
    if images:
        vm.run(["docker", "rmi", *images], capture_output=True)


def build(run: Run, m, image: str) -> None:
    try:
        free = shutil.disk_usage(run.base).free
    except OSError:
        free = None  # a disk that can't answer must not block a deploy
    if free is not None:
        refusal = build_space_verdict(free, limit(run, "ANNEX_BUILD_MIN_FREE_GB", BUILD_MIN_FREE_GB))
        if refusal:
            raise vm.Fail(refusal)
    log = run.d / "build.log"
    with open(log, "w") as f:
        res = vm.run(["docker", "build", "--progress=plain", "--label", "com.kaldi.annex=true",
                      "--label", f"com.kaldi.annex.app={m.name}", "-t", image, str(run.d / "src")],
                     stdout=f, stderr=subprocess.STDOUT)
    if res.returncode != 0:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-40:])
        raise vm.Fail(f"Docker build failed for {m.name}. Fix the Dockerfile or code and deploy again. "
                      f"The running version is unchanged.\n{tail}")
    size = _inspect(image, "{{.Size}}")
    refusal = size.isdigit() and image_size_verdict(int(size), limit(run, "ANNEX_IMAGE_MAX_MB", IMAGE_MAX_MB), m.name)
    if refusal:
        _rmi(image)
        raise vm.Fail(refusal)
    user = _inspect(image, "{{.Config.User}}", check=True)
    if user in ("", "root", "0", "0:0"):
        _rmi(image)
        raise vm.Fail("The image runs as root. End the Dockerfile with a non-root user, e.g. "
                      "'RUN useradd -u 10001 app' and 'USER 10001' (the templates already do this)")
    raw = _inspect(image, "{{json .Config.Labels}}", check=True)
    bad = platform_label_offenders(json.loads(raw or "null"), m.name)
    if bad:
        _rmi(image)
        raise vm.Fail(f"The image carries labels that are Annex's job: {', '.join(bad)}. Remove the LABEL lines "
                      "from the Dockerfile. Annex sets the routing, schedule, discovery-page and backup labels "
                      "itself from annex.yaml; images may not carry traefik.*, ofelia.*, homepage.*, "
                      "docker-volume-backup.* or com.kaldi.annex* labels")


# --- switch and gate ---------------------------------------------------------------------------------------------
def _status(c: str) -> str:
    r = vm.run(["docker", "inspect", "-f", "{{.State.Status}}", c], capture_output=True, text=True)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _logs(c: str, n: int) -> str:
    r = vm.run(["docker", "logs", "--tail", str(n), c], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return r.stdout or ""


def gate(run: Run, m) -> tuple[str, str] | None:
    """Health gate. None if the new container is healthy, else (reason, last log lines)."""
    c = container(m.name)
    if m.is_web:
        timeout = health_timeout(run)
        run.progress(f"Health gate: waiting up to {timeout} s for {m.healthcheck} to return 200")
        url = f"http://{c}:{m.port}{m.healthcheck}"
        deadline, crashed = monotonic() + timeout, False
        while monotonic() < deadline:
            probe = vm.run(["docker", "exec", TRAEFIK, "wget", "-q", "-O", "/dev/null", "-T", "3", url],
                           capture_output=True)
            if probe.returncode == 0:
                return None
            if _status(c) != "running":
                crashed = True
                break
            sleep(2)
        reason = ("the container stopped while starting (it crashed; see the log lines)" if crashed else
                  f"GET {m.healthcheck} on port {m.port} did not return 200 within {timeout} s. "
                  f"Make sure the app listens on 0.0.0.0:$PORT and {m.healthcheck} returns 200")
    else:
        sleep(3)
        state = _status(c) or "missing"
        if state == "running":
            return None
        reason = (f"the container is {state} instead of running (it exits; see the log lines). Job-only apps "
                  "idle with 'sleep infinity' between runs, so the image needs a 'sleep' binary and no ENTRYPOINT "
                  "that exits (the python-job template is fine)")
    return reason, _logs(c, 40)


def bound_port(run: Run, key: str, default: int) -> int:
    """The port Traefik publishes on this host: the port part of ANNEX_BIND_HTTPS / ANNEX_BIND_HTTP (local mode
    binds loopback ports), else the default. Not ANNEX_PUBLIC_PORT: that is the browser's port (the Mac's tunnel
    listens on 8443, D6), not the one Traefik binds on the VM."""
    raw = run.settings.get(key, "").strip()
    m = re.search(r"(?:^|:)(\d+)$", raw)
    return int(m.group(1)) if m else default


def wait_route(run: Run, m) -> None:
    """Routed = the front door answers with a login redirect instead of 404; then Docker's own health settles."""
    host = run.host_for(m.name)
    if run.settings.get("ANNEX_TLS_MODE") == "local":
        port = bound_port(run, "ANNEX_BIND_HTTPS", 443)
        probe = ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
                 "--resolve", f"{host}:{port}:127.0.0.1", f"https://{host}:{port}/"]
    else:
        port = bound_port(run, "ANNEX_BIND_HTTP", 80)
        probe = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
                 "-H", f"Host: {host}", f"http://127.0.0.1:{port}/"]
    for _ in range(30):
        if (vm.run(probe, capture_output=True, text=True).stdout or "").strip() == "302":
            break
        sleep(1)
    fmt = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    for _ in range(45):
        h = (vm.run(["docker", "inspect", "-f", fmt, container(m.name)], capture_output=True, text=True).stdout or "")
        if h.strip() in ("healthy", "none"):
            break
        sleep(2)


def drain(run: Run, app: str) -> None:
    """Before Docker replaces or removes an app's container, have annex-ops store the lines it hasn't read yet
    (D64; the container's log goes with it). Never blocks the operation: annex-ops being down, slow or confused costs
    a warning and nothing else. Lines written while the old container shuts down can still be missed."""
    why = None
    try:
        res = vm.run(["timeout", "10", "docker", "exec", OPS_CONTAINER, "python", "-m", "annex_ops.ctl", "logs-drain",
                      container(app), "--timeout", "5", "--json"], capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            why = "annex-ops didn't answer"
        else:
            answer = json.loads(res.stdout.strip().splitlines()[-1])
            if answer.get("timed_out") or not answer.get("stored", True):
                why = "annex-ops was too slow or its database is down"
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, AttributeError):
        why = "annex-ops didn't answer"
    if why:
        run.progress(f"Warning: couldn't store {container(app)}'s last log lines before replacing it ({why}); "
                     f"the {run.kind} carries on. Check on the VM: docker logs {OPS_CONTAINER}")


def activate(run: Run, m, image: str, release: int, prefix: str = "") -> tuple[tuple[str, str] | None, dict]:
    """Provision what manifest `m` asks for, switch the app to `image` and run the health gate.
    Returns (gate failure or None, provision result). Steps: provision, switch, gate (with `prefix`: back-...)."""
    run.step(prefix + "provision")
    run.progress("Provisioning storage, data access and secrets")
    prov = vm.provision(m.name, postgres=m.postgres, files=m.files, views=m.views, secrets=m.secrets,
                        notify_email=m.notify_email, slack=m.notify_slack,
                        hooks=[w.__dict__ for w in m.webhooks], hook_port=(m.port or 0) if m.webhooks else 0)
    # before the switch: the app's network and the platform containers on it must exist when the container starts (D88)
    vm.sync_app(m.name, m.is_web, m.postgres, bool(m.views), bool(m.egress))
    # JSON is YAML: Compose reads it, and so does migrate-networks
    vm.write_atomic(run.d / "compose.yaml", json.dumps(compose_for(run, m, image, release), indent=2) + "\n")
    drain(run, m.name)  # the container Compose is about to replace (a failed release's, on a switch back)
    run.step(prefix + "switch")
    run.progress(f"Starting r{release} ({image})")
    up = vm.run(["docker", "compose", "up", "-d", "--remove-orphans"], cwd=str(run.d), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
    if up.returncode != 0:
        tail = "\n".join((up.stdout or "").strip().splitlines()[-40:])
        raise vm.Fail(f"docker compose up failed for {m.name} r{release}:\n{tail}")
    run.step(prefix + "gate")
    failed = gate(run, m)
    if failed or not m.is_web:
        return failed, prov
    run.progress("Waiting for the route and the container healthcheck")
    wait_route(run, m)
    return None, prov


def reload_scheduler(run: Run, *schedules: object) -> None:
    """Ofelia reads job labels only at startup (D18)."""
    if any(schedules):
        run.progress("Reloading the scheduler")
        r = vm.run(["docker", "restart", OFELIA], capture_output=True, text=True)
        if r.returncode != 0:
            raise vm.Fail(f"couldn't restart the scheduler ({OFELIA}): {(r.stderr or r.stdout).strip()[-300:]}")


def gate_error(app: str, release: int, failed: tuple[str, str], outcome: str) -> vm.Fail:
    reason, logs = failed
    return vm.Fail(f"{app} r{release} failed the health gate: {reason}.\n{outcome}\n"
                   f"Last log lines of r{release}:\n{logs}")


# --- cleanup -----------------------------------------------------------------------------------------------------
def images(app: str) -> list[str]:
    r = vm.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", f"annex-app/{app}"],
               capture_output=True, text=True)
    return (r.stdout or "").split() if r.returncode == 0 else []


def prune(run: Run, current: str) -> None:
    keep: list[str] = [current]
    for x in reversed(vm.read_releases(run.base, run.app)):
        if x["result"] == "live" and x["image"] not in keep:
            keep.append(x["image"])
        if len(keep) == KEEP_RELEASES:
            break
    _rmi(*[i for i in images(run.app) if i not in keep])
    if run.settings.get("ANNEX_TARGET") == "local":
        # a Mac's Docker also holds the user's own images and build cache: only Annex's dangling images go (D99)
        vm.run(["docker", "image", "prune", "-f", "--filter", "label=com.kaldi.annex=true"], capture_output=True)
    else:
        vm.run(["docker", "image", "prune", "-f"], capture_output=True)
        vm.run(["docker", "builder", "prune", "-f", "--max-used-space", "1500mb"], capture_output=True)
    vm.prune_snapshots(run.app, KEEP_RELEASES)


def cleanup(run: Run, current: str) -> list[str]:
    """Keep the images of the last KEEP_RELEASES live releases and as many snapshots; trim build cache. Runs under the
    lock (another deploy's fresh image must not be removed), before end(): the release going live is still pending in
    the file, so its image (`current`) is named. Best effort: the release is live whatever happens here. Returns the
    warning for the result when it fails (D95), so the disk filling up is not silent."""
    try:
        prune(run, current)
    except Exception as e:  # noqa: BLE001
        msg = (f"Cleaning up old images and snapshots failed ({(str(e).splitlines() or [''])[0]}); the next deploy "
               "tries again. If this keeps happening, tell the Annex owner: the disk may fill up")
        run.progress("Warning: " + msg)
        return [msg]
    return []


# --- what a successful deploy should still say -------------------------------------------------------------------
def ops_ctl(*args: str):
    """An annex-ops admin command's JSON answer (vm.Fail if it doesn't give one)."""
    r = vm.run(["docker", "exec", OPS_CONTAINER, "python", "-m", "annex_ops.ctl", *args, "--json"],
               capture_output=True, text=True)
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise vm.Fail(f"annex-ops isn't answering ({(r.stderr or r.stdout or '').strip()[-300:]})") from None


def ops_warnings(m) -> list[str]:
    """Alert recipients that won't get mail, and Slack that isn't connected (the CLI adds its own: runtime copies,
    raised memory)."""
    out = []
    try:
        connected = ops_ctl("info")["slack_connected"]
        logins = set(ops_ctl("users"))
    except (vm.Fail, KeyError, TypeError):
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
    if m.notify_slack and not connected:
        out.append(f"notify.slack is declared ({', '.join(m.notify_slack)}) but Slack isn't connected on this "
                   "Annex yet, so Slack posts fail with a clear error until the platform team connects it "
                   "(docs/setup.md §7). Email works either way")
    return out


def injected_env(m, prov: dict) -> list[str]:
    return sorted({"PORT", "ANNEX_APP_NAME", "ANNEX_BASE_URL", "LOG_LEVEL", *prov["injected"],
                   *(["DATA_DIR"] if m.files else []),
                   *(["ANNEX_GATEWAY_URL"] if m.views else []),
                   *(["ANNEX_OPS_URL"] if m.notify_email or m.notify_slack else []),
                   *(["ANNEX_HOOKS_URL"] if m.webhooks else [])}
                  - ({"PORT"} if not m.is_web else set()))


# --- deploy ------------------------------------------------------------------------------------------------------
def upload(run: Run, stdin) -> None:
    """The rest of stdin is the source as tar.gz: extract it as it arrives into src.new, then swap it in."""
    new, src = run.d / "src.new", run.d / "src"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    try:
        with tarfile.open(fileobj=stdin, mode="r|gz") as tar:
            tar.extractall(new, filter="data")
    except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
        shutil.rmtree(new, ignore_errors=True)
        raise vm.Fail(f"The upload of {run.app}'s source broke off ({e}). Nothing was built and the running version "
                      "is unchanged; deploy again") from None
    shutil.rmtree(src, ignore_errors=True)
    new.rename(src)


def manifest_of(run: Run, line: dict):
    try:
        m = mf.Manifest.from_dict(line["manifest"])
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise vm.Fail(f"the deploy's meta has no valid manifest ({type(e).__name__}: {e}); is the CLI current?") from None
    if m.name != run.app:
        raise vm.Fail(f"the manifest is for {m.name!r}, not {run.app!r}")
    return m


def prepare_deploy(run: Run, stdin, a) -> Callable[[], dict]:
    """Before the fork: settle, number the release, receive the source. Returns the rest of the deploy."""
    line = {k: v for k, v in (run.meta.get("line") or {}).items() if k not in ("release", "image", "result", "record")}
    m = manifest_of(run, line)
    cli = run.meta.get("cli")
    refusal, notes = check_versions(cli, platform_versions(run.base))
    if refusal:
        raise vm.Fail(refusal)  # before any release line
    if isinstance(cli, dict):  # the release line records the CLI's commit (the status and the console show it)
        line["cli"] = {"commit": cli.get("commit") or "unknown", "dirty": bool(cli.get("dirty"))}
    for msg in notes:
        run.notes.append(msg)
        run.note(msg)
    line.setdefault("deployed_at", _now())
    line.setdefault("deployed_by", run.actor)
    line.setdefault("via", run.via)
    run.settle()
    previous = read_record(run)
    live = vm.live_release(vm.read_releases(run.base, run.app))  # read under the lock, after settling
    run.open(line, record_for(run, m, None, None))
    run.step("upload")
    run.progress("Uploading source")
    upload(run, stdin)
    run.progress("Validating annex.yaml on the VM")
    revalidate_source(run, line)
    return lambda: deploy_rest(run, m, live, previous)


def deploy_rest(run: Run, m, live: dict | None, previous: dict | None) -> dict:
    n, image = run.n, run.image
    run.step("build")
    run.progress(f"Building r{n} on the VM")
    build(run, m, image)

    rel: dict = {"snapshot": None}
    if live:
        run.step("snapshot")
        run.progress(f"Snapshotting data before the switch (r{live['release']} is live)")
        snap = vm.snapshot(m.name, n, live["release"])
        rel["snapshot"] = snap and snap["name"]

    failed, prov = activate(run, m, image, n)
    if failed:
        rel.update(result="failed", error=failed[0])
        if not live:
            run.end(rel)
            raise gate_error(m.name, n, failed, f"There's no earlier release to roll back to, so r{n} is left as "
                             f"is. Fix the app and deploy again; see its logs with: annex logs {m.name}")
        run.progress(f"r{n} failed the health gate; rolling back to r{live['release']}")
        prev = mf.Manifest.from_dict(live["manifest"])
        # a failed gate re-provisions the previous release's manifest (grants, secrets, networks), as before C1
        back, _ = activate(run, prev, live["image"], live["release"], prefix="back-")
        reload_scheduler(run, m.schedule, prev.schedule)
        write_record(run, prev, live["image"], live["release"])
        _rmi(image)
        run.end({**rel, "rolled_back_to": live["release"]})
        if back:
            raise gate_error(m.name, n, failed, f"Rolling back to r{live['release']} failed too ({back[0]}), "
                             f"so {m.name} is down. Check: annex logs {m.name}")
        raise gate_error(m.name, n, failed, f"Annex rolled back to r{live['release']}, which is live again. "
                         "Fix the app and deploy again.")

    run.step("finish")
    reload_scheduler(run, m.schedule, previous and previous.get("schedule"))
    record = write_record(run, m, image, n)
    cleanup_warnings = cleanup(run, image)
    run.end({**rel, "result": "live"})
    run.progress("Done")
    warnings = list(run.notes) + cleanup_warnings + ops_warnings(m)
    if m.is_web:  # job-only apps are checked on their first run (annex status)
        warnings += [w for w in [logcheck.warning(_logs(container(m.name), 50).splitlines())] if w]
    return {**record, "warnings": warnings, "webhooks": webhook_urls(run, m.name, m.webhooks),
            "previous_release": live and live["release"], "snapshot": rel["snapshot"],
            "injected_env": injected_env(m, prov)}


# --- rollback ----------------------------------------------------------------------------------------------------
def running_image(app: str) -> str:
    r = vm.run(["docker", "inspect", "-f", "{{.Config.Image}}", container(app)], capture_output=True, text=True)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def rollback_target(run: Run, rels: list[dict], to: int | None, with_data: bool) -> tuple[dict, dict]:
    """(the live release, the one to switch to), or vm.Fail saying why not. The live release itself is a target
    only when the app's container isn't running its image (a deploy died after its switch; reconcile says so, D92)."""
    app = run.app
    live = vm.live_release(rels)
    if not live:
        raise vm.Fail(f"{app} has no releases to roll back to. See: annex releases {app}")
    kept = set(images(app))
    targets = [x for x in rels if x["result"] == "live" and x["release"] != live["release"] and x["image"] in kept]
    listing = ", ".join(f"r{x['release']}" for x in targets) or "none"
    if to is None:
        earlier = [x for x in targets if x["release"] < live["release"]]
        if not earlier:
            raise vm.Fail(f"{app} has no earlier release to roll back to (r{live['release']} is live; "
                          f"possible targets: {listing}). See: annex releases {app}")
        target = earlier[-1]
    elif to == live["release"] and live["image"] in kept and running_image(app) != live["image"]:
        target = live  # put the live release back in place of a container a dead deploy left
    else:
        target = next((x for x in targets if x["release"] == to), None)
        if not target:
            raise vm.Fail(f"Can't roll {app} back to r{to}. Possible targets: {listing} (releases that went live, "
                          f"aren't current and whose image is kept). See: annex releases {app}")
    t = target["release"]
    snaps = [json.loads((p / "meta.json").read_text()) for p in vm.snapshot_dirs(app)]
    if with_data and t not in {s["from_release"] for s in snaps}:
        raise vm.Fail(f"There's no data snapshot from while r{t} was live (snapshots are taken before each "
                      f"deploy, and the last {KEEP_RELEASES} are kept). Roll back without --with-data, or restore a "
                      f"nightly backup with scripts/restore.sh {app}")
    return live, target


def prepare_rollback(run: Run, stdin, a) -> Callable[[], dict]:
    """Before the fork: settle, then resolve the target (a bad one is refused before any release line), number."""
    with_data = bool(getattr(a, "with_data", False))
    run.settle()
    live, target = rollback_target(run, vm.read_releases(run.base, run.app), getattr(a, "to", None), with_data)
    tm = mf.Manifest.from_dict(target["manifest"])
    given = run.meta.get("line") or {}
    line = {"image": target["image"], "manifest": target["manifest"],
            "deployed_at": given.get("deployed_at") or _now(), "deployed_by": given.get("deployed_by") or run.actor,
            "via": given.get("via") or run.via, "source": target.get("source"), "git": target.get("git"),
            "snapshot": None, "rollback": {"from": live["release"], "to": target["release"], "with_data": with_data}}
    run.open(line, record_for(run, tm, None, None))
    return lambda: rollback_rest(run, live, target, with_data)


def rollback_rest(run: Run, live: dict, target: dict, with_data: bool) -> dict:
    """A data restore holds the app's alerts while the container is stopped (restore_snapshot opens the maintenance
    window); it ends once the app is back, whatever happened."""
    try:
        return _rollback(run, live, target, with_data)
    finally:
        if with_data:
            vm.maintenance(run.app, None)


def _rollback(run: Run, live: dict, target: dict, with_data: bool) -> dict:
    app, n, t = run.app, run.n, target["release"]
    run.step("snapshot")
    run.progress(f"Snapshotting data before the switch (r{live['release']} is live)")
    snap = vm.snapshot(app, n, live["release"])
    rel: dict = {"snapshot": snap and snap["name"]}
    restored = None
    if with_data:
        run.step("restore")
        run.progress(f"Restoring the database and /data from while r{t} was live")
        try:
            restored = vm.restore_snapshot(app, t)
        except BaseException:
            vm.run(["docker", "start", container(app)], capture_output=True)
            raise
    tm = mf.Manifest.from_dict(target["manifest"])
    cm = mf.Manifest.from_dict(live["manifest"])

    failed, _ = activate(run, tm, target["image"], n)
    if failed:
        run.progress(f"r{t} failed the health gate; switching back to r{live['release']}")
        if with_data and snap:
            run.step("back-restore")
            vm.restore_snapshot(app, live["release"])
        back, _ = activate(run, cm, live["image"], live["release"], prefix="back-")
        reload_scheduler(run, tm.schedule, cm.schedule)
        write_record(run, cm, live["image"], live["release"])
        run.end({**rel, "result": "failed", "error": failed[0], "rolled_back_to": live["release"]})
        outcome = (f"Switching back to r{live['release']} failed too ({back[0]}), so {app} is down."
                   if back else f"Annex switched back to r{live['release']}"
                   + (" and its data" if with_data else "") + ", which is live again.")
        raise gate_error(app, n, failed, outcome)

    run.step("finish")
    reload_scheduler(run, tm.schedule, cm.schedule)
    record = write_record(run, tm, target["image"], n)
    cleanup_warnings = cleanup(run, target["image"])
    run.end({**rel, "result": "live"})
    run.progress("Done")
    return {"app": app, "release": n, "rolled_back_from": live["release"], "rolled_back_to": t,
            "image": target["image"], "with_data": with_data, "restored": restored, "url": record["url"],
            "snapshot": rel["snapshot"], "warnings": list(run.notes) + cleanup_warnings}
