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
