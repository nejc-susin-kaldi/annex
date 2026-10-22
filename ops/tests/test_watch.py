from annex_ops import engine, watch_docker as w


def ev(action, **attrs):
    base = {"name": "annex-app-probe", "com.docker.compose.project": "annex-app-probe",
            "com.docker.compose.service": "probe", "com.kaldi.annex.app": "probe"}
    return {"Type": "container", "Action": action, "Actor": {"ID": "abc123", "Attributes": {**base, **attrs}}}


def test_classify_apps_platform_and_strangers():
    assert w.classify({"com.docker.compose.project": "annex-app-x", "com.kaldi.annex.app": "x"}) == ("app:x", False)
    assert w.classify({"com.docker.compose.project": "annex-app-x"}) == ("app:x", False)
    assert w.classify({"com.docker.compose.project": "annex", "com.docker.compose.service": "gateway"}) == ("platform:gateway", False)
    assert w.classify({"com.docker.compose.project": "annex", "com.docker.compose.service": "metabase"}) == ("platform:metabase", True)
    assert w.classify({"com.docker.compose.project": "annex", "com.docker.compose.service": "dozzle"})[1] is True
    # test probes and `docker run` helpers carry the Annex label but no Compose project of ours
    assert w.classify({"com.kaldi.annex": "true"}) is None
    assert w.classify({"com.docker.compose.project": "something-else"}) is None


def test_die_carries_the_exit_code():
    i = w.event_to_input(ev("die", exitCode="137"))
    assert isinstance(i, engine.Die) and i.exit_code == 137 and i.subject == "app:probe" and i.cid == "abc123"


def test_other_actions():
    assert isinstance(w.event_to_input(ev("start")), engine.Start)
    assert isinstance(w.event_to_input(ev("stop")), engine.Stop)
    assert isinstance(w.event_to_input(ev("oom")), engine.Oom)
    d = w.event_to_input(ev("destroy"))
    assert isinstance(d, engine.Destroy) and d.subject == "app:probe"


def test_health_status_actions():
    assert w.event_to_input(ev("health_status: unhealthy")).healthy is False
    assert w.event_to_input(ev("health_status: healthy")).healthy is True
    assert w.event_to_input(ev("health_status: starting")) is None


def test_events_of_strangers_and_other_actions_are_ignored():
    assert w.event_to_input(ev("die", **{"com.docker.compose.project": "other"})) is None
    assert w.event_to_input(ev("kill")) is None
    assert w.event_to_input(ev("exec_die")) is None


def test_inspect_to_seen():
    info = {"Id": "abc", "Name": "/annex-app-probe", "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Config": {"Labels": {"com.docker.compose.project": "annex-app-probe", "com.kaldi.annex.app": "probe"}}}
    s = w.inspect_to_seen(info)
    assert (s.cid, s.name, s.subject, s.running, s.health) == ("abc", "annex-app-probe", "app:probe", True, "healthy")
    info["State"] = {"Running": True, "Restarting": True}
    assert w.inspect_to_seen(info).running is False
    info["State"] = {"Running": False}
    assert w.inspect_to_seen(info).health is None
    assert w.inspect_to_seen({"Id": "x", "Config": {"Labels": {"com.kaldi.annex": "true"}}, "State": {}}) is None


# --- the reconcile pass (D90): only a complete pass may tell the engine which containers exist -------------------------
import pytest  # noqa: E402

from annex_ops import docker_api  # noqa: E402


def info(cid):
    return {"Id": cid, "Name": f"/annex-app-{cid}", "State": {"Running": True},
            "Config": {"Labels": {"com.docker.compose.project": f"annex-app-{cid}", "com.kaldi.annex.app": cid}}}


def reconcile_with(monkeypatch, failures):
    """Run one reconcile over containers a, b, c, where `failures` maps an id to the error its inspect raises."""
    monkeypatch.setattr(docker_api, "containers", lambda: [{"Id": c} for c in ("a", "b", "c")])

    def inspect(cid):
        if cid in failures:
            raise failures[cid]
        return info(cid)

    monkeypatch.setattr(docker_api, "inspect", inspect)
    got, extra = [], []
    watcher = w.Watcher(got.append, lambda: extra.append(1))
    return watcher, got, extra


def test_a_complete_reconcile_ends_with_the_ids_it_saw(monkeypatch):
    watcher, got, extra = reconcile_with(monkeypatch, {})
    watcher.reconcile()
    assert [type(i).__name__ for i in got] == ["Seen", "Seen", "Seen", "ReconcileDone"] and got[-1].cids == {"a", "b", "c"}
    assert extra == [1]


def test_a_container_destroyed_during_the_pass_is_left_out(monkeypatch):
    watcher, got, _ = reconcile_with(monkeypatch, {"b": docker_api.DockerError(404, "no such container")})
    watcher.reconcile()
    assert isinstance(got[-1], engine.ReconcileDone) and got[-1].cids == {"a", "c"}


@pytest.mark.parametrize("error", [docker_api.DockerError(500, "proxy busy"), TimeoutError("timed out")])
def test_a_failed_inspect_keeps_the_reconcile_pending(monkeypatch, error):
    watcher, got, extra = reconcile_with(monkeypatch, {"b": error})
    with pytest.raises(w.ReconcileIncomplete):
        watcher.reconcile()
    # the others are reported, but nothing tells the engine that b is gone: run() repeats the pass in 5 s
    assert [i.cid for i in got] == ["a", "c"] and not any(isinstance(i, engine.ReconcileDone) for i in got)
    assert extra == [1]  # the health checker starts anyway (a verdict for an unseen app is ignored)


def test_an_incomplete_pass_does_not_close_the_missing_containers_incident(monkeypatch):
    e = engine.Engine()
    e.handle(0, engine.Seen(cid="b", name="annex-app-b", subject="app:b", running=True))
    e.handle(1, engine.Health(cid="b", name="annex-app-b", subject="app:b", healthy=False))
    watcher, got, _ = reconcile_with(monkeypatch, {"b": docker_api.DockerError(500, "proxy busy")})
    with pytest.raises(w.ReconcileIncomplete):
        watcher.reconcile()
    for i in got:
        e.handle(2, i)
    assert "app:b" in e.open and "b" in e.ctrs


def test_only_a_real_destroy_event_reaches_the_log_shipper():
    """The shipper deletes a follower's cursor only when it stops a follower, and it stops one only on a Destroy input,
    which comes from Docker's destroy event: a reconcile's ReconcileDone (or a failed inspect) never stops one."""
    from annex_ops import logship

    shipper = logship.Shipper(sink=None, releases=None, store=object())
    shipper.followers["a"] = object()
    shipper.submit(engine.ReconcileDone(set()))
    assert shipper.q.qsize() == 0
    shipper.submit(engine.Destroy("unknown", "app:x"))
    assert shipper.q.qsize() == 0
    shipper.submit(w.event_to_input({"Type": "container", "Action": "destroy", "Actor": {"ID": "a", "Attributes": {
        "com.docker.compose.project": "annex-app-a", "com.kaldi.annex.app": "a"}}}))
    assert shipper.q.qsize() == 1
