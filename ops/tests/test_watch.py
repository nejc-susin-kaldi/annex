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
