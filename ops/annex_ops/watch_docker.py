"""Watch Docker: a reconcile pass at startup, then the event stream, turned into engine inputs."""
import sys
import threading
import time

from . import docker_api, engine

EVENTS = ["die", "start", "stop", "destroy", "oom", "health_status"]
FILTERS = {"type": ["container"], "label": ["com.kaldi.annex=true"], "event": EVENTS}
IGNORE_STOPPED = {"metabase", "dozzle"}  # the extras profile: `scripts/extras.sh off` stops them on purpose


def classify(labels: dict) -> tuple[str, bool] | None:
    """(subject, ignore_stopped) for a container that counts, else None. Only platform containers (Compose
    project `annex`) and apps (project `annex-app-<name>`) count: test probes and `docker run --rm` helpers
    carry the Annex label too and are ignored."""
    project = labels.get("com.docker.compose.project", "")
    if project == "annex":
        service = labels.get("com.docker.compose.service", "")
        return (f"platform:{service}", service in IGNORE_STOPPED) if service else None
    if project.startswith("annex-app-"):
        return f"app:{labels.get('com.kaldi.annex.app') or project[len('annex-app-'):]}", False
    return None


def event_to_input(ev: dict):
    attrs = ev.get("Actor", {}).get("Attributes", {})
    cls = classify(attrs)
    if cls is None:
        return None
    subject, ignore = cls
    cid, name, action = ev["Actor"]["ID"], attrs.get("name", ""), ev.get("Action", "")
    common = dict(cid=cid, name=name, subject=subject, ignore_stopped=ignore)
    if action == "die":
        try:
            code = int(attrs.get("exitCode", 0))
        except ValueError:
            code = 1
        return engine.Die(exit_code=code, **common)
    if action == "start":
        return engine.Start(**common)
    if action == "stop":
        return engine.Stop(**common)
    if action == "oom":
        return engine.Oom(**common)
    if action == "destroy":
        return engine.Destroy(cid, subject)
    if action.startswith("health_status:"):
        status = action.split(":", 1)[1].strip()
        if status in ("healthy", "unhealthy"):
            return engine.Health(healthy=status == "healthy", **common)
    return None


def inspect_to_seen(info: dict):
    labels = info.get("Config", {}).get("Labels") or {}
    cls = classify(labels)
    if cls is None:
        return None
    state = info.get("State", {})
    health = (state.get("Health") or {}).get("Status")
    return engine.Seen(cid=info["Id"], name=info.get("Name", "").lstrip("/"), subject=cls[0], ignore_stopped=cls[1],
                       running=bool(state.get("Running")) and not state.get("Restarting"), health=health)


def log(msg: str) -> None:
    print(f"watch_docker: {msg}", file=sys.stderr, flush=True)


class Watcher(threading.Thread):
    def __init__(self, submit, reconcile_extra=None):
        super().__init__(daemon=True, name="watch-docker")
        self.submit = submit
        self.reconcile_extra = reconcile_extra  # called after the reconcile pass (Gatus state)

    def reconcile(self) -> None:
        cids = set()
        for c in docker_api.containers():
            try:
                seen = inspect_to_seen(docker_api.inspect(c["Id"]))
            except docker_api.DockerError:
                continue
            if seen:
                cids.add(seen.cid)
                self.submit(seen)
        self.submit(engine.ReconcileDone(cids))
        if self.reconcile_extra:
            self.reconcile_extra()

    def run(self) -> None:
        last_nano = 0
        since = time.time() - 1
        reconcile = True
        while True:
            try:
                if reconcile:
                    started = time.time()
                    self.reconcile()
                    since, reconcile = started - 1, False
                for ev in docker_api.stream_events(since, FILTERS):
                    nano = ev.get("timeNano", 0)
                    if nano <= last_nano:
                        continue  # replayed after a reconnect
                    last_nano = nano
                    since = nano / 1e9
                    inp = event_to_input(ev)
                    if inp:
                        self.submit(inp)
            except TimeoutError:
                continue  # a quiet stream: just reconnect from where we were
            except Exception as e:  # noqa: BLE001: the watcher must never die
                log(f"{type(e).__name__}: {e}; reconnecting in 5 s")
                reconcile = True  # events may have been missed
                time.sleep(5)
