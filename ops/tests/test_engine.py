from annex_ops.engine import (Destroy, Die, Engine, HealthState, Health, JobResult, Maintenance, Oom, ReconcileDone,
                              Request, Restore, Seen, Start, Stop, Tuning)

APP = "app:probe"
C = dict(cid="c1", name="annex-app-probe", subject=APP)


def kinds(actions):
    return [(a.kind, a.signal) if a.signal else a.kind for a in actions if a.kind != "forget"]


def run(engine, events):
    """events: list of (time, input or 'tick'); returns all actions in order."""
    out = []
    for t, inp in events:
        out += engine.tick(t) if inp == "tick" else engine.handle(t, inp)
    return out


def engine(**kw):
    return Engine(Tuning(**kw))


def test_stop_noticed_by_docker_and_the_health_checker_is_one_incident():
    e = engine()
    out = run(e, [(0, Stop(**C)), (1, Die(exit_code=137, **C)), (61, "tick"),
                  (120, HealthState(APP, True, "connection refused")), (130, "tick")])
    assert kinds(out) == ["open", ("add_signal", "down")]
    assert out[0].signals.keys() == {"stopped"}
    out = run(e, [(200, Start(**C)), (210, HealthState(APP, False))])
    assert kinds(out) == [("clear_signal", "stopped"), ("clear_signal", "down"), "resolve"]
    assert out[-1].duration_s == 210 - 61 and not out[-1].quiet


def test_resolve_waits_until_every_signal_has_cleared():
    e = engine()
    run(e, [(0, Stop(**C)), (61, "tick"), (70, HealthState(APP, True))])
    out = run(e, [(100, Start(**C))])
    assert kinds(out) == [("clear_signal", "stopped")]  # the health checker still says down
    assert kinds(run(e, [(130, HealthState(APP, False))])) == [("clear_signal", "down"), "resolve"]


def test_a_deploy_length_stop_alerts_nobody():
    e = engine()
    out = run(e, [(0, Stop(**C)), (1, Die(exit_code=0, **C)), (30, "tick"), (45, Start(**C)), (90, "tick")])
    assert out == []


def test_just_past_the_grace_period_it_counts():
    e = engine()
    assert run(e, [(0, Stop(**C)), (59, "tick")]) == []
    assert kinds(run(e, [(60, "tick")])) == ["open"]


def test_extras_stopped_is_ignored_but_unhealthy_is_not():
    e = engine()
    mb = dict(cid="m1", name="annex-metabase-1", subject="platform:metabase", ignore_stopped=True)
    assert run(e, [(0, Stop(**mb)), (200, "tick")]) == []
    assert kinds(run(e, [(300, Health(healthy=False, **mb))])) == ["open"]


def test_platform_service_stop_opens_a_platform_incident():
    e = engine()
    gw = dict(cid="g1", name="annex-gateway-1", subject="platform:gateway")
    out = run(e, [(0, Stop(**gw)), (61, "tick")])
    assert out[0].subject == "platform:gateway"


# --- crash loop -----------------------------------------------------------------
def crash_events(times, code=1):
    return [(t, Die(exit_code=code, **C)) for t in times]


def test_crash_loop_needs_three_nonzero_exits_in_the_window():
    e = engine()
    assert run(e, crash_events([0, 100])) == []
    out = run(e, crash_events([200]))
    assert kinds(out) == ["open"] and "crash-loop" in out[0].signals


def test_exits_outside_the_window_dont_count():
    e = engine()
    assert run(e, crash_events([0, 100, 700])) == []  # the first is 700 s old at the third (window 600)


def test_zero_exits_dont_count():
    e = engine()
    assert run(e, crash_events([0, 1, 2, 3], code=0)) == []


def test_crash_loop_clears_after_the_quiet_period_while_running():
    e = engine()
    run(e, crash_events([0, 10, 20]) + [(21, Start(**C))])
    assert run(e, [(300, "tick")]) == []                                # 280 s since the last exit: not yet
    assert kinds(run(e, [(620, "tick")])) == [("clear_signal", "crash-loop"), "resolve"]


def test_crash_loop_does_not_clear_while_the_container_is_down():
    e = engine()
    run(e, crash_events([0, 10, 20]))
    out = run(e, [(1000, "tick")])
    assert ("clear_signal", "crash-loop") not in kinds(out)


def test_oom_clears_with_the_crash_loop_rule():
    e = engine()
    out = run(e, [(0, Oom(**C)), (1, Die(exit_code=137, **C)), (2, Start(**C))])
    assert kinds(out) == ["open"] and "oom" in out[0].signals
    assert run(e, [(100, "tick")]) == []
    assert kinds(run(e, [(700, "tick")])) == [("clear_signal", "oom"), "resolve"]


def test_unhealthy_and_healthy():
    e = engine()
    assert kinds(run(e, [(0, Health(healthy=False, **C))])) == ["open"]
    assert kinds(run(e, [(50, Health(healthy=True, **C))])) == [("clear_signal", "unhealthy"), "resolve"]


# --- 5xx ----------------------------------------------------------------------
def requests(start, n, status, step=1.0):
    return [(start + i * step, Request("probe", start + i * step, status)) for i in range(n)]


def test_error_spike_needs_both_the_ratio_and_the_minimum():
    e = engine(error_window_s=300)
    # 9 of 9 is 100 % but under the minimum of 10
    assert run(e, requests(0, 9, 500) + [(20, "tick")]) == []
    # 10 errors among 1000 requests is 1 %: enough errors, too small a share
    e = engine()
    assert run(e, requests(0, 990, 200, 0.1) + requests(0, 10, 500, 5) + [(100, "tick")]) == []
    # 10 of 100 is 10 %: both
    e = engine()
    out = run(e, requests(0, 90, 200) + requests(0, 10, 500) + [(100, "tick")])
    assert kinds(out) == ["open"] and "errors" in out[0].signals


def test_error_spike_clears_after_a_full_window_below_the_threshold():
    e = engine(error_window_s=60)
    out = run(e, requests(0, 20, 500) + [(30, "tick")])
    assert kinds(out) == ["open"]
    # the burst ages out of the window at t=80; the clock for "below" starts at the first tick after that
    assert run(e, [(85, "tick")]) == []
    assert run(e, [(120, "tick")]) == []
    assert kinds(run(e, [(146, "tick")])) == [("clear_signal", "errors"), "resolve"]


def test_a_new_burst_resets_the_below_clock():
    e = engine(error_window_s=60)
    run(e, requests(0, 20, 500) + [(30, "tick"), (85, "tick")])
    run(e, requests(100, 20, 500) + [(120, "tick")])
    assert run(e, [(150, "tick")]) == []  # still over, so nothing clears


def test_4xx_dont_count():
    e = engine()
    out = run(e, requests(0, 50, 404) + [(60, "tick")])
    assert out == []


# --- jobs -----------------------------------------------------------------------
def test_job_failure_resolves_on_the_next_good_run_of_the_same_job_only():
    e = engine()
    out = run(e, [(0, JobResult("probe", "check", False, "exit 1"))])
    assert kinds(out) == ["open"] and out[0].subject == "job:probe/check"
    assert run(e, [(10, JobResult("probe", "other", True))]) == []   # another job's good run
    assert run(e, [(20, JobResult("other-app", "check", True))]) == []
    assert run(e, [(30, JobResult("probe", "check", False, "exit 1"))]) == []  # still failing: nothing new
    assert kinds(run(e, [(60, JobResult("probe", "check", True))])) == [("clear_signal", "job-failed"), "resolve"]


def test_job_incident_is_separate_from_the_app_incident():
    e = engine()
    run(e, [(0, JobResult("probe", "check", False))])
    out = run(e, [(1, Health(healthy=False, **C))])
    assert out[0].kind == "open" and out[0].subject == APP


# --- reminders ---------------------------------------------------------------------
def test_reminders_after_an_hour_then_daily():
    e = engine()
    run(e, [(0, Start(**C)), (1, HealthState(APP, True))])
    assert run(e, [(3600, "tick")]) == []
    assert kinds(run(e, [(3601, "tick")])) == ["remind"]
    assert run(e, [(3602, "tick"), (3700 + 80000, "tick")]) == []
    assert kinds(run(e, [(3601 + 86400, "tick")])) == ["remind"]
    assert kinds(run(e, [(3601 + 86400 + 10, "tick")])) == []


def test_reminder_lists_the_current_signals_and_the_age():
    e = engine()
    run(e, [(0, Start(**C)), (0, HealthState(APP, True, "x")), (10, Health(healthy=False, **C))])
    out = run(e, [(3601, "tick")])
    assert out[0].signals.keys() == {"down", "unhealthy"} and out[0].duration_s == 3601


# --- maintenance ---------------------------------------------------------------------
def test_maintenance_collects_signals_then_opens_when_it_ends():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 900))])
    assert run(e, [(10, Stop(**C)), (80, "tick"), (500, "tick")]) == []
    out = run(e, [(901, "tick")])
    assert kinds(out) == ["open"] and "stopped" in out[0].signals


def test_signals_that_cleared_during_maintenance_never_alert():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 900)), (10, Stop(**C)), (80, "tick"), (100, Start(**C))])
    assert run(e, [(901, "tick")]) == []


def test_ending_maintenance_early_releases_active_signals():
    e = engine()
    run(e, [(0, Start(**C)), (1, Maintenance(APP, 9000)), (10, Stop(**C)), (80, "tick")])
    out = run(e, [(100, Maintenance(APP, None))])
    assert kinds(out) == ["open"]


def test_app_maintenance_covers_its_jobs():
    e = engine()
    run(e, [(0, Maintenance(APP, 900))])
    assert run(e, [(5, JobResult("probe", "check", False))]) == []


def test_reminders_are_held_during_maintenance():
    e = engine()
    run(e, [(0, Start(**C)), (1, HealthState(APP, True)), (3000, Maintenance(APP, 9000))])
    assert run(e, [(3700, "tick")]) == []


# --- destroy, recreate, reconcile ----------------------------------------------------------
def test_destroy_closes_the_incident_without_a_message():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (62, "tick"), (70, HealthState(APP, True))])
    out = run(e, [(100, Destroy("c1", APP))])
    assert [a.kind for a in out if a.kind != "forget"] == ["clear_signal", "clear_signal", "resolve"]
    assert out[-1].quiet
    assert any(a.kind == "forget" for a in out)
    assert e.open == {} and e.signals == {}


def test_destroy_also_closes_the_apps_job_incidents():
    e = engine()
    run(e, [(0, Start(**C)), (1, JobResult("probe", "check", False))])
    out = run(e, [(5, Destroy("c1", APP))])
    assert out[-1].kind == "resolve" and out[-1].quiet and out[-1].subject == "job:probe/check"


def test_a_health_result_for_an_app_with_no_container_is_ignored():
    e = engine()
    assert run(e, [(0, HealthState("app:ghost", True))]) == []


def test_platform_front_door_needs_no_container():
    e = engine()
    assert kinds(run(e, [(0, HealthState("platform:front-door", True))])) == ["open"]


def test_compose_recreate_does_not_alert():
    e = engine()
    new = dict(C, cid="c2")
    out = run(e, [(0, Start(**C)), (10, Stop(**C)), (10, Die(exit_code=137, **C)), (11, Start(**new)),
                  (12, Destroy("c1", APP)), (100, "tick")])
    assert [a for a in out if a.kind != "forget"] == []


def test_recreate_of_a_stopped_app_resolves_it_loudly():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (62, "tick")])
    new = dict(C, cid="c2")
    out = run(e, [(100, Start(**new)), (101, Destroy("c1", APP))])
    assert out[-1].kind == "resolve" and not out[-1].quiet


def test_reconcile_clears_a_stopped_signal_that_ended_while_ops_was_down():
    e = engine()
    run(e, [(0, Restore([{"subject": APP, "opened_at": 0, "last_notified_at": 0, "reminders": 0,
                          "signals": {"stopped": {"since": 0, "detail": "x", "cid": None}}}]))])
    out = run(e, [(100, Seen(running=True, health=None, **C)), (101, ReconcileDone({"c1"}))])
    assert kinds(out) == [("clear_signal", "stopped"), "resolve"]


def test_reconcile_forgets_containers_that_vanished_while_ops_was_down():
    e = engine()
    run(e, [(0, Start(**C))])
    out = run(e, [(50, ReconcileDone(set()))])
    assert any(a.kind == "forget" for a in out) and e.ctrs == {}


def test_reconcile_of_a_stopped_container_starts_the_grace_period():
    e = engine()
    run(e, [(0, Seen(running=False, **C))])
    assert run(e, [(30, "tick")]) == []
    assert kinds(run(e, [(61, "tick")])) == ["open"]


def test_restored_incident_keeps_reminding_from_its_original_time():
    e = engine()
    run(e, [(0, Restore([{"subject": APP, "opened_at": 1000, "last_notified_at": 1000, "reminders": 0,
                          "signals": {"down": {"since": 1000, "detail": "x", "cid": None}}}]))])
    assert kinds(run(e, [(1000 + 3601, "tick")])) == ["remind"]


def test_an_errors_signal_restored_after_an_ops_restart_still_clears():
    e = engine(error_window_s=60)
    run(e, [(0, Restore([{"subject": APP, "opened_at": 0, "last_notified_at": 0, "reminders": 0,
                          "signals": {"errors": {"since": 0, "detail": "x", "cid": None}}}]))])
    assert run(e, [(10, "tick")]) == []               # no requests in memory: the quiet period starts now
    assert kinds(run(e, [(71, "tick")])) == [("clear_signal", "errors"), "resolve"]


def test_a_job_failing_because_its_app_is_down_adds_no_second_alert():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C))])
    # the container is stopped but the grace period isn't over: the job's failure is a consequence
    assert run(e, [(5, JobResult("probe", "check", False, "container not running"))]) == []
    run(e, [(62, "tick")])  # now the app incident is open
    assert run(e, [(65, JobResult("probe", "check", False, "container not running"))]) == []
    out = run(e, [(100, Start(**C))])
    assert kinds(out) == [("clear_signal", "stopped"), "resolve"]
    assert e.signals == {}


def test_a_job_that_still_fails_after_the_app_is_back_alerts_on_its_own():
    e = engine()
    run(e, [(0, Start(**C)), (1, Stop(**C)), (5, JobResult("probe", "check", False)), (100, Start(**C))])
    out = run(e, [(160, JobResult("probe", "check", False, "exit 1"))])
    assert kinds(out) == ["open"] and out[0].subject == "job:probe/check"


# --- webhooks ---------------------------------------------------------------------------------------------
from annex_ops.engine import HookFailing, HookOk  # noqa: E402


def test_a_failing_hook_raises_the_webhook_signal_and_resolves_when_delivered():
    e = engine()
    out = run(e, [(0, Start(**C)), (10, HookFailing("probe", "trello", 0))])
    assert kinds(out) == ["open"] and "webhook" in out[0].signals and "trello" in out[0].signals["webhook"]["detail"]
    assert kinds(run(e, [(20, HookOk("probe", "trello"))])) == [("clear_signal", "webhook"), "resolve"]


def test_webhook_joins_the_open_incident_of_a_stopped_app():
    e = engine()
    out = run(e, [(0, Start(**C)), (1, Stop(**C)), (62, "tick"), (70, HookFailing("probe", "trello", 5))])
    assert kinds(out) == ["open", ("add_signal", "webhook")]
    out = run(e, [(80, Start(**C)), (90, HookOk("probe", "trello"))])
    assert kinds(out) == [("clear_signal", "stopped"), ("clear_signal", "webhook"), "resolve"]


def test_the_signal_stays_until_every_failing_hook_is_ok():
    e = engine()
    run(e, [(0, Start(**C)), (1, HookFailing("probe", "a1", 0)), (2, HookFailing("probe", "b2", 0))])
    assert run(e, [(3, HookOk("probe", "a1"))]) == []
    assert kinds(run(e, [(4, HookOk("probe", "b2"))])) == [("clear_signal", "webhook"), "resolve"]


def test_a_hook_that_stops_being_reported_is_not_failing_any_more():
    e = engine()
    run(e, [(0, Start(**C)), (1, HookFailing("probe", "trello", 0))])
    run(e, [(30, HookFailing("probe", "trello", 0))])      # the worker repeats the report while it lasts
    assert run(e, [(80, "tick")]) == []
    assert kinds(run(e, [(100, "tick")])) == [("clear_signal", "webhook"), "resolve"]


def test_a_restored_webhook_signal_clears_if_no_report_follows_the_restart():
    e = engine()
    run(e, [(1000, Restore([{"subject": APP, "opened_at": 900, "last_notified_at": 900, "reminders": 0,
                             "signals": {"webhook": {"since": 900, "detail": "x", "cid": None}}}]))])
    assert run(e, [(1020, "tick")]) == []
    assert kinds(run(e, [(1061, "tick")])) == [("clear_signal", "webhook"), "resolve"]


def test_a_restored_webhook_signal_stays_while_the_worker_still_reports_it():
    e = engine()
    run(e, [(1000, Restore([{"subject": APP, "opened_at": 900, "last_notified_at": 900, "reminders": 0,
                             "signals": {"webhook": {"since": 900, "detail": "x", "cid": None}}}])),
            (1002, HookFailing("probe", "trello", 800)), (1040, HookFailing("probe", "trello", 800))])
    assert run(e, [(1065, "tick")]) == []


def test_removing_the_app_closes_a_webhook_incident_quietly():
    e = engine()
    run(e, [(0, Start(**C)), (1, HookFailing("probe", "trello", 0))])
    out = run(e, [(5, Destroy("c1", APP))])
    assert out[-1].kind == "resolve" and out[-1].quiet and e.hooks_failing == {}


# --- one incident per front-door outage (D90) ---------------------------------------------------------------
from annex_ops.engine import FRONT_DOOR, HostSample, LoadState  # noqa: E402

TR = dict(cid="t1", name="annex-traefik-1", subject="platform:traefik")
AU = dict(cid="a1", name="annex-authelia-1", subject="platform:authelia")


def test_a_traefik_outage_is_one_incident_even_when_the_front_door_is_noticed_first():
    e = engine()
    # docker stop annex-traefik-1: the checker confirms the front door down before Traefik's grace period is over
    out = run(e, [(0, Start(**TR)), (1, Stop(**TR)), (30, HealthState(FRONT_DOOR, True, "connection refused")), (61, "tick")])
    assert kinds(out) == ["open", ("add_signal", "stopped")]
    assert out[0].subject == "platform:traefik" and "front door" in out[0].signals["down"]["detail"]
    assert FRONT_DOOR not in e.open
    out = run(e, [(100, Start(**TR)), (160, HealthState(FRONT_DOOR, False))])
    assert kinds(out) == [("clear_signal", "stopped"), ("clear_signal", "down"), "resolve"]
    assert {a.subject for a in out} == {"platform:traefik"}


def test_the_front_door_joins_an_open_traefik_incident():
    e = engine()
    out = run(e, [(0, Start(**TR)), (1, Stop(**TR)), (61, "tick"), (90, HealthState(FRONT_DOOR, True))])
    assert kinds(out) == ["open", ("add_signal", "down")] and {a.subject for a in out} == {"platform:traefik"}


def test_an_authelia_outage_takes_the_front_door_too():
    e = engine()
    out = run(e, [(0, Start(**AU)), (1, Health(healthy=False, **AU)), (60, HealthState(FRONT_DOOR, True))])
    assert kinds(out) == ["open", ("add_signal", "down")] and {a.subject for a in out} == {"platform:authelia"}


def test_a_front_door_failure_with_traefik_fine_is_its_own_incident():
    e = engine()
    out = run(e, [(0, Start(**TR)), (0, Start(**AU)), (60, HealthState(FRONT_DOOR, True, "got HTTP 404"))])
    assert kinds(out) == ["open"] and out[0].subject == FRONT_DOOR
    # Traefik going down afterwards is a second problem: the front door's signal stays where it was raised
    out = run(e, [(70, Stop(**TR)), (131, "tick"), (140, HealthState(FRONT_DOOR, True))])
    assert kinds(out) == ["open"] and out[0].subject == "platform:traefik"
    out = run(e, [(150, Start(**TR)), (160, HealthState(FRONT_DOOR, False))])
    assert {a.subject for a in out if a.kind == "resolve"} == {"platform:traefik", FRONT_DOOR}


def test_a_restored_front_door_signal_on_traefik_clears_when_the_front_door_is_up():
    e = engine()
    run(e, [(0, Restore([{"subject": "platform:traefik", "opened_at": 0, "last_notified_at": 0, "reminders": 0,
                          "signals": {"down": {"since": 0, "detail": "the front door isn't answering", "cid": None}}}])),
            (5, Seen(running=True, **TR))])
    # the checker's seeded verdict after a restart: still down changes nothing, up resolves it
    assert run(e, [(6, HealthState(FRONT_DOOR, True))]) == []
    assert kinds(run(e, [(70, HealthState(FRONT_DOOR, False))])) == [("clear_signal", "down"), "resolve"]


# --- counters and timers across an ops restart (D90) ---------------------------------------------------------
def restart(e, offset_before, offset_after):
    """A new engine loaded from e's saved state: the times were saved as wall (engine time + offset_before) and the new
    process's monotonic clock has a different origin (offset_after)."""
    saved = e.export_state(offset_before)
    e2 = Engine(e.tuning)
    e2.handle(0, LoadState(saved, offset_after))
    return e2, saved


def test_a_crash_loop_is_counted_across_an_ops_restart():
    e = engine()
    run(e, crash_events([1000, 1100]))
    # ops restarts: wall time 1000 was engine time 1000 before (offset 0) and is engine time 0 after (offset 1000)
    e2, saved = restart(e, 0, 1000)
    assert saved["ctr:c1"]["dies"] == [[1000, 1], [1100, 1]]
    out = run(e2, [(150, Seen(running=False, **C)), (151, ReconcileDone({"c1"})), (200, Die(exit_code=1, **C))])
    assert kinds(out) == ["open"] and "3 crashes" in out[0].signals["crash-loop"]["detail"]


def test_the_stopped_grace_period_continues_across_an_ops_restart():
    e = engine()
    run(e, [(0, Start(**C)), (10, Stop(**C))])
    e2, _ = restart(e, 5000, 4900)  # stopped at wall 5010 = new engine time 110
    run(e2, [(130, Seen(running=False, **C)), (131, ReconcileDone({"c1"}))])
    assert run(e2, [(169, "tick")]) == []
    assert kinds(run(e2, [(170, "tick")])) == ["open"]


def test_a_saved_container_waits_for_the_reconcile_pass():
    e = engine()
    run(e, [(0, Stop(**C))])
    e2, _ = restart(e, 0, 0)
    assert e2.ctrs["c1"].known is False
    assert run(e2, [(500, "tick")]) == []  # stopped "for 500 s" by the saved state, but nobody has looked yet
    assert run(e2, [(501, Seen(running=True, **C)), (502, ReconcileDone({"c1"})), (600, "tick")]) == []


def test_a_saved_container_that_is_gone_is_forgotten():
    e = engine()
    run(e, [(0, Stop(**C))])
    e2, _ = restart(e, 0, 0)
    out = run(e2, [(10, ReconcileDone(set()))])
    assert any(a.kind == "forget" for a in out) and e2.ctrs == {} and e2.export_state() == {}


def test_a_crash_loops_quiet_period_continues_from_the_saved_last_exit():
    e = engine(crash_stable_s=600)
    run(e, crash_events([0, 10, 20]))
    e2, _ = restart(e, 0, 0)
    run(e2, [(25, Restore([{"subject": APP, "opened_at": 20, "last_notified_at": 20, "reminders": 0,
                            "signals": {"crash-loop": {"since": 20, "detail": "x", "cid": "c1"}}}])),
             (30, Seen(running=True, **C))])
    assert run(e2, [(619, "tick")]) == []
    assert kinds(run(e2, [(620, "tick")])) == [("clear_signal", "crash-loop"), "resolve"]


def test_low_memory_time_survives_an_ops_restart():
    e = engine(mem_alert_mb=256, mem_alert_s=300)
    run(e, [(0, HostSample(50, 10, 100, None))])
    e2, saved = restart(e, 0, 0)
    assert saved["host:mem_low_since"] == {"since": 0}
    assert run(e2, [(200, HostSample(50, 10, 100, None))]) == []
    assert kinds(run(e2, [(300, HostSample(50, 10, 100, None))])) == ["open"]
    run(e2, [(400, HostSample(50, 10, 900, None))])
    assert "host:mem_low_since" not in e2.export_state()


def test_only_containers_with_a_timer_are_saved_and_unreadable_rows_are_skipped():
    e = engine()
    run(e, [(0, Start(**C)), (1, Start(**TR))])
    assert e.export_state() == {}
    e2 = engine()
    e2.handle(0, LoadState({"ctr:x": {"name": "n"}, "ctr:y": "junk", "host:mem_low_since": {}, "other": 1,
                            "ctr:c1": {"name": "annex-app-probe", "subject": APP, "stopped_since": 5, "dies": [[4, 1]]}}))
    assert list(e2.ctrs) == ["c1"] and e2.ctrs["c1"].dies == [(4.0, 1)] and e2.mem_low_since is None


# --- the engine's clock is monotonic; the manager converts wall times at the edges (D90) ----------------------
import annex_ops.incidents as inc  # noqa: E402


class FakeClock:
    def __init__(self, mono, offset):
        self.mono, self.offset = mono, offset

    def now(self):
        return self.mono, self.offset


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=()):
        self.log.append((sql, args))
        return self

    def fetchone(self):
        return (len(self.log),)

    def fetchall(self):
        return []

    def cursor(self):
        return self


class FakeOutbox:
    def __init__(self):
        self.rows = []

    def add(self, kind, subject, action):
        self.rows.append((kind, subject, action))
        return len(self.rows)


class FakeState:
    def __init__(self, saved=None):
        self.saved, self.writes = saved or {}, []

    def load(self):
        return self.saved

    def save(self, changed, removed):
        self.writes.append((changed, removed))


def manager(monkeypatch, clock, state=None):
    sql = []
    monkeypatch.setattr(inc.db, "connect", lambda: FakeConn(sql))
    monkeypatch.setattr(inc.config, "tuning", lambda: Tuning())
    return inc.Manager(outbox=FakeOutbox(), state=state or FakeState(), clock=clock), sql


def test_a_wall_clock_step_backwards_doesnt_freeze_reminders(monkeypatch):
    wall0 = 1_700_000_000.0
    clock = FakeClock(100.0, wall0 - 100)
    m, sql = manager(monkeypatch, clock)
    m.step(HealthState(FRONT_DOOR, True, "no answer"))
    assert [r[0] for r in m.outbox.rows] == ["open"] and m.outbox.rows[0][2]["opened_at"] == wall0
    assert any("to_timestamp" in s and wall0 in a for s, a in sql)  # stored in wall time
    # an hour passes, and NTP steps the wall clock back by an hour: by wall time one second went by
    clock.mono, clock.offset = 100.0 + 3601, wall0 - 100 - 3600
    m.step(None)
    assert [r[0] for r in m.outbox.rows] == ["open", "remind"]
    assert m.outbox.rows[1][2]["opened_at"] == wall0 and m.outbox.rows[1][2]["duration_s"] == 3601


def test_wall_times_in_inputs_are_put_on_the_engines_clock():
    off = 1_000_000.0
    assert inc.to_engine_time(Request("x", off + 5, 500), off).ts == 5
    h = inc.to_engine_time(HostSample(1, 2, 3, off + 10, None), off)
    assert (h.backup_at, h.dump_at) == (10, None)
    assert inc.to_engine_time(HookFailing("a", "h", off + 7), off).since == 7
    assert inc.to_engine_time(Maintenance(APP, off + 900), off).until == 900
    assert inc.to_engine_time(Maintenance(APP, None), off).until is None
    s = Stop(**C)
    assert inc.to_engine_time(s, off) is s


def test_a_backup_age_is_measured_in_wall_time_whatever_the_engines_clock(monkeypatch):
    wall = 1_700_000_000.0
    m, _ = manager(monkeypatch, FakeClock(50.0, wall - 50))
    m.step(HostSample(10, 20, 2000, wall - 30 * 3600, None))  # 30 h old: overdue
    assert [(r[0], r[1]) for r in m.outbox.rows] == [("open", "platform:backup")]
    assert m.outbox.rows[0][2]["signals"]["backup"]["since"] == wall  # signals go out in wall time


def test_engine_state_is_saved_on_change_only_in_wall_time(monkeypatch):
    state = FakeState()
    clock = FakeClock(10.0, 1000.0)
    m, _ = manager(monkeypatch, clock, state)
    m.step(Die(exit_code=1, **C))
    assert len(state.writes) == 1
    changed, removed = state.writes[0]
    assert changed["ctr:c1"]["last_die"] == 1010.0 and changed["ctr:c1"]["dies"] == [[1010.0, 1]] and removed == []
    clock.offset += 1e-6  # the offset jitters between passes: not a change
    m.step(None)
    m.step(Request("probe", 1011.0, 200))
    assert len(state.writes) == 1
    m.step(Destroy("c1", APP))
    assert state.writes[-1] == ({}, ["ctr:c1"])


def test_restore_loads_saved_timers_on_the_new_clock_and_drops_unreadable_rows(monkeypatch):
    # saved by the last process in wall time: Traefik stopped at wall 1_000_000; this process's clock reads 5 at wall 1_000_050
    state = FakeState({"ctr:t1": {"name": "annex-traefik-1", "subject": "platform:traefik", "stopped_since": 1_000_000.0,
                                  "last_die": 1_000_000.0, "dies": []}, "ctr:junk": 7})
    clock = FakeClock(5.0, 1_000_045.0)
    m, _ = manager(monkeypatch, clock, state)
    m.restore()
    assert m.engine.ctrs["t1"].stopped_since == -45.0 and not m.engine.ctrs["t1"].known
    m.step(Seen(running=False, **TR))
    assert m.outbox.rows == []  # stopped 50 s ago by the wall clock; the grace period is 60
    assert state.writes and state.writes[0][1] == ["ctr:junk"]
    clock.mono = 25.0
    m.step(None)
    assert [(r[0], r[1]) for r in m.outbox.rows] == [("open", "platform:traefik")]
