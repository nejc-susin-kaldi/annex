"""Disk, memory and backup age: the measurements and the engine's rules for them (D56)."""
from types import SimpleNamespace

from annex_ops import engine as eng, host, messages


def run(e, now, inp):
    return [(a.kind, a.subject, a.signal) for a in e.handle(now, inp)]


def sample(disk=50.0, mem=1500.0, backup_age=3600.0, now=100000.0):
    return eng.HostSample(disk, 10.0, mem, None if backup_age is None else now - backup_age)


def test_meminfo():
    assert host.mem_available_mb("MemTotal: 4000000 kB\nMemAvailable:    1474708 kB\n") == 1474708 / 1024
    assert host.mem_available_mb("MemTotal: 1 kB\n") is None


def test_disk_usage_like_df():
    st = SimpleNamespace(f_blocks=1000, f_bfree=300, f_bavail=250, f_frsize=4096)  # 700 used, 250 available
    pct, free = host.disk_usage(st)
    assert round(pct, 1) == 73.7 and round(free * 2**30) == 250 * 4096


def test_disk_alerts_with_hysteresis():
    e = eng.Engine()
    assert run(e, 1, sample(disk=86)) == [("open", "platform:disk", None)]
    assert run(e, 2, sample(disk=82)) == []            # between the thresholds: still open, no flapping
    acts = run(e, 3, sample(disk=79))
    assert ("resolve", "platform:disk", None) in acts


def test_memory_needs_to_stay_low():
    e = eng.Engine()
    assert run(e, 0, sample(mem=200)) == []
    assert run(e, 200, sample(mem=200)) == []          # 200 s < 300 s
    assert run(e, 301, sample(mem=200)) == [("open", "platform:memory", None)]
    assert run(e, 302, sample(mem=300)) == []          # recovering, but not yet above 400 MB
    assert ("resolve", "platform:memory", None) in run(e, 303, sample(mem=500))


def test_memory_blip_resets():
    e = eng.Engine()
    run(e, 0, sample(mem=200))
    run(e, 200, sample(mem=600))                       # recovered before the 5 minutes were up
    assert run(e, 400, sample(mem=200)) == []          # the clock starts again


def test_backup_age():
    e = eng.Engine()
    now = 200000.0
    assert run(e, now, eng.HostSample(None, None, None, now - 3600)) == []
    assert run(e, now, eng.HostSample(None, None, None, now - 27 * 3600)) == [("open", "platform:backup", None)]
    assert ("resolve", "platform:backup", None) in run(e, now, eng.HostSample(None, None, None, now - 60))


def test_backup_stale_dump():
    e = eng.Engine()
    now = 500000.0
    fresh = lambda gap: eng.HostSample(None, None, None, now - 60, now - 60 - gap)  # noqa: E731
    assert run(e, now, fresh(600)) == []                                   # dumped minutes before the archive
    assert run(e, now, fresh(86400)) == []                                 # exactly a day: still fine
    assert run(e, now, fresh(86401)) == [("open", "platform:backup", None)]
    assert run(e, now, fresh(3 * 86400)) == []                             # already open, same signal
    assert ("resolve", "platform:backup", None) in run(e, now, fresh(30))
    assert e.signals == {}


def test_backup_stale_adds_to_overdue_incident():
    e = eng.Engine()
    now = 500000.0
    assert run(e, now, eng.HostSample(None, None, None, now - 27 * 3600, now - 80 * 3600)) == [
        ("open", "platform:backup", None), ("add_signal", "platform:backup", "backup-stale")]
    # the archive came back but the dumps are still old: the incident stays open on the second signal
    acts = run(e, now + 1, eng.HostSample(None, None, None, now, now - 80 * 3600))
    assert ("clear_signal", "platform:backup", "backup") in acts and ("resolve", "platform:backup", None) not in acts


def test_unknown_marker_leaves_stale_signal_alone():
    e = eng.Engine()
    now = 500000.0
    run(e, now, eng.HostSample(None, None, None, now, now - 3 * 86400))
    assert run(e, now + 1, eng.HostSample(None, None, None, now, None)) == []   # marker unreadable: no verdict
    assert "backup-stale" in e.signals["platform:backup"]


def test_marker_parse():
    assert host.parse_marker("2026-10-22T02:00:07Z\n") == 1792634407.0
    assert host.parse_marker("") is None and host.parse_marker("garbage") is None


def test_unmeasured_values_leave_signals_alone():
    e = eng.Engine()
    run(e, 1, sample(disk=90))
    assert run(e, 2, eng.HostSample(None, None, None, None)) == []
    assert "disk" in e.signals["platform:disk"]


def test_host_messages_are_admin_only_and_helpful():
    sigs = {"disk": {"since": 0, "detail": "the VM's disk is 90 % full"}}
    assert messages.subject_line("open", "platform:disk", sigs) == "[Annex] platform: disk is nearly full"
    body = messages.body("open", "platform:disk", sigs, 0, 0, [], "https://status.example")
    assert "docker system df" in body and "docker logs annex-disk-1" not in body
    assert "scripts/backup.sh" in messages.body("open", "platform:backup", {"backup": {"since": 0}}, 0, 0, [], "")
    emails, _, _ = messages.recipients("platform", None, None, {
        "sam": {"email": "sam@x", "groups": ["builders"]}, "kaldi-admin": {"email": "a@x", "groups": ["admins"]}},
        False, "#annex-alerts")
    assert emails == ["a@x"]


def test_a_sample_is_stored_every_five_minutes_and_the_first_one_always():
    assert host.due(None, 1000) and not host.due(1000, 1200) and host.due(1000, 1300) and host.due(1000, 4600, 3600)


def test_a_failing_database_never_stops_the_sampler(monkeypatch, capsys):
    from annex_ops.errors import OpsError

    def boom():
        raise OpsError(503, "no database")
    monkeypatch.setattr(host.db, "connect", boom)
    host.store(sample())          # must not raise
    host.housekeeping()
    assert "couldn't store a sample" in capsys.readouterr().err


def test_path_read_one_small_file(monkeypatch):
    import io
    import tarfile

    from annex_ops import docker_api

    def tar_of(name, data):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
        return buf.getvalue()

    class Resp:
        def __init__(self, raw): self.raw = raw
        def read(self, n): return self.raw[:n]

    class Conn:
        def close(self): pass

    monkeypatch.setattr(docker_api, "_get", lambda path, timeout: (Conn(), Resp(tar_of("MARKER", b"2026-10-22T02:00:07Z\n"))))
    assert host.parse_marker(docker_api.path_read("c", "/backup/dumps/current/MARKER")) == 1792634407.0
    monkeypatch.setattr(docker_api, "_get", lambda path, timeout: (Conn(), Resp(tar_of("big", b"x" * 9000))))
    try:
        docker_api.path_read("c", "/x")
        raise AssertionError("expected a refusal")
    except docker_api.DockerError:
        pass
