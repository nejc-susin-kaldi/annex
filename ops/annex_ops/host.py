"""The VM itself: disk, memory and the age of the newest backup, sampled for the alert engine (D56).

ops sees the host's own numbers without extra privileges: `/proc/meminfo` in a container is the host's, and
statvfs on a bind-mounted folder reports the host filesystem it lives on. The backup's age is the mtime of the
backup container's archive folder, read through the read-only Docker proxy (a HEAD request: a stat, never the
archives, which hold every app's data).
"""
import os
import sys
import threading
import time

from . import config, docker_api, engine

BACKUP_CONTAINER = "annex-backup-1"
BACKUP_FOLDER = "/archive"
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


def sample() -> engine.HostSample:
    """One measurement; a part that can't be measured is None (and the engine leaves its signal alone)."""
    pct = free = mem = backup_at = None
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
    return engine.HostSample(pct, free, mem, backup_at)


class Sampler(threading.Thread):
    def __init__(self, submit):
        super().__init__(daemon=True, name="host-sampler")
        self.submit = submit

    def run(self) -> None:
        while True:
            try:
                self.submit(sample())
            except Exception as e:  # noqa: BLE001  (never let the sampler die)
                print(f"host: sample failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(max(5.0, config.tuning().host_sample_s))
