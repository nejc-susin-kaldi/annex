"""The VM itself: disk, memory and the age of the newest backup and of its dumps, sampled for the alert engine (D56).

ops sees the host's own numbers without extra privileges: `/proc/meminfo` in a container is the host's, and
statvfs on a bind-mounted folder reports the host filesystem it lives on. The backup's age is the mtime of the
backup container's archive folder, read through the read-only Docker proxy (a HEAD request: a stat, never the
archives, which hold every app's data).
"""
import os
import sys
import tarfile
import threading
import time

from . import config, db, docker_api, engine, threads
from .errors import OpsError

BACKUP_CONTAINER = "annex-backup-1"
BACKUP_FOLDER = "/archive"
DUMP_MARKER = "/backup/dumps/current/MARKER"   # written by db/appdata/hooks/dump-all.sh (D87)
STORE_EVERY_S = 300        # one stored sample per 5 minutes (about 8,600 rows in 30 days) for the console's trends
KEEP_DAYS = 30
STATVFS_PATH = os.environ.get("OPS_HOST_PATH", "/annex/state")  # any bind mount from the host's root filesystem


def mem_available_mb(meminfo: str) -> float | None:
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024  # kB
    return None


def disk_usage(st) -> tuple[float, float]:
    """(percent used, GB free) as `df` shows them: used / (used + available to unprivileged users)."""
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return (100.0 * used / (used + avail) if used + avail else 0.0), avail / 2**30


def parse_marker(text: str) -> float | None:
    """dump-all.sh's marker (`2026-10-22T02:00:07Z`, one line) -> epoch seconds; None when it isn't one."""
    return docker_api.parse_ts(text.strip().split("\n")[0]) if text.strip() else None


def sample() -> engine.HostSample:
    """One measurement; a part that can't be measured is None (and the engine leaves its signal alone)."""
    pct = free = mem = backup_at = dump_at = None
    try:
        pct, free = disk_usage(os.statvfs(STATVFS_PATH))
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = mem_available_mb(f.read())
    except OSError:
        pass
    try:
        backup_at = docker_api.parse_ts(docker_api.path_stat(BACKUP_CONTAINER, BACKUP_FOLDER).get("mtime", ""))
    except (docker_api.DockerError, OSError, ValueError):
        pass
    try:
        # The dumps folder is part of every archive, and the hook replaces it as a whole, so its marker is the
        # one inside the newest archive. Reading the live file costs one small request; scanning the archive
        # (a gzip tar of every app's /data) would mean decompressing it all every minute.
        dump_at = parse_marker(docker_api.path_read(BACKUP_CONTAINER, DUMP_MARKER))
    except (docker_api.DockerError, OSError, ValueError, tarfile.TarError):
        pass
    return engine.HostSample(pct, free, mem, backup_at, dump_at)


def store(s: engine.HostSample) -> None:
    """Keep a sample for the console (the Platform page). Never raises: a failure is only logged, the alert engine
    carries on with its own samples."""
    try:
        with db.connect() as conn:
            conn.execute("INSERT INTO annex_ops.host_samples (disk_used_pct, disk_free_gb, mem_available_mb, backup_at) "
                         "VALUES (%s, %s, %s, CASE WHEN %s::float8 IS NULL THEN NULL ELSE to_timestamp(%s::float8) END)",
                         (s.disk_used_pct, s.disk_free_gb, s.mem_available_mb, s.backup_at, s.backup_at))
    except (OpsError, OSError) as e:
        print(f"host: couldn't store a sample: {e}", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"host: couldn't store a sample: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def housekeeping() -> None:
    try:
        with db.connect() as conn:
            conn.execute("DELETE FROM annex_ops.host_samples WHERE ts < now() - make_interval(days => %s)", (KEEP_DAYS,))
    except Exception as e:  # noqa: BLE001
        print(f"host: housekeeping failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def due(last: float | None, now: float, every: float = STORE_EVERY_S) -> bool:
    """Whether a sample should be stored now (the first one always is)."""
    return last is None or now - last >= every


class Sampler(threading.Thread):
    def __init__(self, submit):
        super().__init__(daemon=True, name="host-sampler")
        self.submit = submit

    def run(self) -> None:
        last_store = last_clean = None
        threads.register(self.name, 60)
        while True:
            threads.beat(self.name, max(5.0, config.tuning().host_sample_s) + 30)  # sleep plus a slow sample
            try:
                s = sample()
                self.submit(s)
                if due(last_store, time.monotonic()):
                    last_store = time.monotonic()
                    store(s)
                if due(last_clean, time.monotonic(), 3600):
                    last_clean = time.monotonic()
                    housekeeping()
            except Exception as e:  # noqa: BLE001  (never let the sampler die)
                print(f"host: sample failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(max(5.0, config.tuning().host_sample_s))
