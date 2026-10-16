"""Log shipping's decisions: parsing, secrets, the rate cap, what is shipped (M2.2). All pure: no database, no Docker."""
import json
import time

from annex_ops import docker_api, engine as eng, logship as ls, logstore, watch_docker

SECRETS = ["S3CRET-field", "S3CRET-nested", "S3CRET-text", "S3CRET-bearer", "S3CRET-url", "S3CRET-jwtpart"]


def test_a_structured_line_keeps_its_fields_and_moves_the_request_id():
    p = ls.parse_app_line('{"ts": "2026-10-16T10:00:00+00:00", "level": "info", "msg": "request", "request_id": "abc123def456abcd", '
                          '"user": "sam", "path": "/", "status": 200, "ms": 4}')
    assert (p.level, p.msg, p.request_id) == ("info", "request", "abc123def456abcd")
    assert p.fields == {"user": "sam", "path": "/", "status": 200, "ms": 4}   # ts, level, msg and request_id are columns


def test_unsafe_request_ids_are_not_stored():
    p = ls.parse_app_line('{"level": "info", "msg": "x", "request_id": "bad id\\nwith newline"}')
    assert p.request_id is None


def test_secret_looking_fields_are_dropped_at_every_depth():
    line = json.dumps({"level": "info", "msg": "login", "password": SECRETS[0], "Authorization": "Bearer x",
                       "nested": {"api_key": SECRETS[1], "ok": 1, "list": [{"Cookie": "c", "keep": "yes"}]},
                       "session_token": "t", "count": 3})
    p = ls.parse_app_line(line)
    assert p.fields == {"nested": {"ok": 1, "list": [{"keep": "yes"}]}, "count": 3}
    assert not any(s in json.dumps(p.fields) for s in SECRETS)


def test_secrets_in_text_are_redacted():
    for raw, secret in [(f"connecting with password={SECRETS[2]} now", SECRETS[2]),
                        (f"Authorization: Bearer {SECRETS[3]}abcdef", SECRETS[3]),
                        (f"postgres://app:{SECRETS[4]}@db:5432/x", SECRETS[4]),
                        (f'{{"password": "{SECRETS[0]}"}}', SECRETS[0]),
                        (f"token: {SECRETS[2]}", SECRETS[2]),
                        ("eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYXBwX3giLCJhcHAiOiJ4In0.abcdefghijk", "eyJyb2xlIjoi")]:
        p = ls.parse_app_line(raw)
        assert secret not in p.msg, raw
        assert "[redacted" in p.msg, raw


def test_secrets_inside_a_structured_msg_or_string_field_too():
    p = ls.parse_app_line(json.dumps({"level": "warn", "msg": f"retry with token={SECRETS[2]}", "detail": f"password={SECRETS[0]}"}))
    assert SECRETS[2] not in p.msg and SECRETS[0] not in json.dumps(p.fields)


def test_ordinary_text_is_left_alone():
    p = ls.parse_app_line("tokens refreshed for 3 users; password reset email sent")
    assert p.msg == "tokens refreshed for 3 users; password reset email sent"


def test_plain_lines_are_kept_as_they_are_with_a_guessed_level():
    assert ls.parse_app_line("got a request for /x").level is None
    assert ls.parse_app_line("got a request for /x").fields is None   # not structured
    for line in ("Traceback (most recent call last):", '  File "/app/x.py", line 3, in f', "ValueError: bad",
                 "2026-10-16 ERROR could not connect", "java.lang.IllegalStateException: boom", "FATAL: out of memory"):
        assert ls.parse_app_line(line).level == "error", line
    for line in ("no exception was raised", "errors: none", "error handling is on"):
        assert ls.parse_app_line(line).level is None, line


def test_json_without_a_valid_level_or_msg_is_not_structured():
    for line in ('{"msg": "x"}', '{"level": "INFO", "msg": "x"}', '{"level": "info"}', '{"level": 30, "msg": "x"}', '{broken'):
        p = ls.parse_app_line(line)
        assert p.fields is None and p.msg, line


def test_empty_lines_are_skipped():
    assert ls.parse_app_line("") is None and ls.parse_app_line("   \r") is None


def test_nul_ansi_surrogates_and_nan_cannot_break_the_insert():
    p = ls.parse_app_line('{"level": "info", "msg": "a\\u0000b", "x": NaN, "y": "\\ud800", "z": {"k\\u0000": 1}}')
    json.dumps(p.fields)
    assert "\x00" not in p.msg and p.fields["x"] == "nan"
    assert "\x00" not in json.dumps(p.fields)
    assert "\x1b" not in ls.parse_app_line("\x1b[31mred\x1b[0m").msg


def test_long_lines_and_big_fields_are_bounded():
    p = ls.parse_app_line("x" * 20000)
    assert len(p.msg) <= ls.MAX_MSG and p.msg.endswith("…[truncated]")
    big = ls.parse_app_line(json.dumps({"level": "info", "msg": "m", **{f"k{i}": "v" * 1500 for i in range(30)}}))
    assert "_truncated" in big.fields and len(json.dumps(big.fields)) < 1000


def test_the_apps_own_health_check_noise_is_not_filtered_but_the_platforms_is():
    hz = 'INFO:     127.0.0.1:60284 - "GET /healthz HTTP/1.1" 200 OK'
    assert ls.parse_platform_line(hz) is None
    assert ls.parse_platform_line('INFO:     172.1.1.1:5 - "POST /h/x/y HTTP/1.1" 200 OK') is not None
    assert ls.parse_platform_line('INFO:     172.1.1.1:5 - "GET /healthz HTTP/1.1" 503 Service Unavailable') is not None


def test_authelia_keeps_sign_in_lines_and_drops_the_noise():
    anon = ('time="2026-10-16T12:25:56+02:00" level=info msg="Access to https://home/ (method GET) is not authorized to user '
            '<anonymous>, responding with status code 302" method=GET path=/api/authz/forward-auth remote_ip=172.21.0.3')
    reload = 'time="x" level=info msg="Reloaded successfully" file=/secrets/users.yml op=WRITE'
    fail = ('time="2026-10-16T12:00:00+02:00" level=error msg="Unsuccessful 1FA authentication attempt by user \'sam\'" '
            'error="user not found" method=POST path=/api/firstfactor remote_ip=172.21.0.1')
    denied = 'time="x" level=info msg="Access to \'https://x/\' is forbidden to user \'cm\'" method=GET path=/api/authz/forward-auth'
    boring = 'time="x" level=info msg="Startup complete"'
    odd = 'time="x" level=warning msg="Something odd happened"'
    assert ls.parse_authelia_line(anon) is None and ls.parse_authelia_line(reload) is None and ls.parse_authelia_line(boring) is None
    f = ls.parse_authelia_line(fail)
    assert (f.level, f.msg) == ("error", "Unsuccessful 1FA authentication attempt by user 'sam'")
    assert f.fields["error"] == "user not found" and f.fields["path"] == "/api/firstfactor" and "time" not in f.fields
    assert ls.parse_authelia_line(denied).level == "info"
    assert ls.parse_authelia_line(odd).level == "warn"


def test_logfmt_with_quotes_and_escapes():
    kv = ls.parse_logfmt(r'level=info msg="say \"hi\" there" n=3 empty= path=/a/b')
    assert kv == {"level": "info", "msg": 'say "hi" there', "n": "3", "empty": "", "path": "/a/b"}


def test_gateway_lines_become_app_reads():
    line = "2026-10-16 12:40:00.872 CEST [130445] authenticator@product LOG:  annex-gateway role=app_monthly_report method=GET path=/monthly_positions"
    p = ls.parse_gateway_line(line)
    assert (p.app, p.msg, p.fields) == ("monthly-report", "GET /monthly_positions",
                                       {"method": "GET", "path": "/monthly_positions", "view": "monthly_positions"})
    assert ls.parse_gateway_line("... CONTEXT:  PL/pgSQL function annex_gateway.log_request() line 3 at RAISE") is None
    assert ls.parse_gateway_line("... LOG:  annex-gateway role=authenticator method=GET path=/x") is None


def test_rows_have_docker_time_an_exact_hash_and_keep_same_microsecond_lines_apart():
    ns = 1_792_000_000_123_456_789
    a = ls.make_row(ns, "stdout", "line", ls.Parsed("info", "m"), "annex-app-x", "x", 3, "annex-app-x")
    b = ls.make_row(ns + 5, "stdout", "line", ls.Parsed("info", "m"), "annex-app-x", "x", 3, "annex-app-x")
    assert a.ts == b.ts and a.ns == 789 and b.ns == 794 and a.h != b.h
    assert a.h == ls.make_row(ns, "stdout", "line", ls.Parsed("info", "m"), "annex-app-x", "x", 3, "annex-app-x").h  # a replay hashes the same
    assert a.ts.microsecond == 123456 and a.ts.tzinfo is not None


def test_nanosecond_timestamps_from_docker():
    assert docker_api.parse_ts_ns("2026-10-16T10:20:00.863412345Z") % 10**9 == 863_412_345
    ns = docker_api.parse_ts_ns("2026-10-16T10:20:00.5Z")
    assert ns % 10**9 == 500_000_000
    assert docker_api.parse_ts_ns("2026-10-16T10:20:00Z") % 10**9 == 0
    assert docker_api.parse_ts_ns("not a time") is None
    assert abs(docker_api.parse_ts_ns("2026-10-16T10:20:00.25Z") / 1e9 - docker_api.parse_ts("2026-10-16T10:20:00.25Z")) < 1e-6


def test_the_rate_cap_counts_in_log_time_and_reports_once():
    cap = ls.RateCap(3)
    sec = 1_000
    results = [cap.admit(sec * 10**9 + i) for i in range(5)]
    assert [ok for ok, _ in results] == [True, True, True, False, False]
    assert all(rep is None for _, rep in results)
    ok, report = cap.admit((sec + 1) * 10**9)     # the next second: the drops of the last one are reported, once
    assert ok and report == (sec, 2)
    assert cap.admit((sec + 1) * 10**9 + 1) == (True, None)


def test_a_flood_that_stops_is_still_reported():
    cap = ls.RateCap(1)
    cap.admit(5 * 10**9); cap.admit(5 * 10**9 + 1); cap.admit(5 * 10**9 + 2)
    assert cap.pending(6) is None            # the second isn't over yet
    assert cap.pending(8) == (5, 2)
    assert cap.pending(9) is None


def test_old_and_future_lines_are_not_stored():
    now = time.time_ns()
    day = 86400 * 10**9
    assert ls.usable(now, now, 14) and ls.usable(now - 13 * day, now, 14)
    assert not ls.usable(now - 15 * day, now, 14) and not ls.usable(now + 3 * day, now, 14)


def test_what_is_shipped():
    assert ls.spec_for("app:client-lookup").kind == "app"
    assert ls.spec_for("platform:ops").kind == "platform" and ls.spec_for("platform:hooks").kind == "platform"
    assert ls.spec_for("platform:authelia").kind == "authelia"
    assert ls.spec_for("platform:product-db").container == "gateway"
    for not_shipped in ("platform:socket-ro", "platform:socket-exec", "platform:traefik", "platform:gatus", "platform:ofelia",
                        "platform:metabase", "platform:homepage", "platform:mailpit", "platform:dozzle", "platform:backup"):
        assert ls.spec_for(not_shipped) is None, not_shipped


def test_the_shipper_only_queues_what_it_ships():
    s = ls.Shipper(sink=None, releases=None, store=object())
    s.submit(eng.Seen("c1", "annex-app-x", "app:x", True))
    s.submit(eng.Seen("c2", "annex-traefik-1", "platform:traefik", True))
    s.submit(eng.Start("c3", "annex-hooks-1", "platform:hooks"))
    s.submit(eng.Destroy("c9", "app:x"))            # we never followed c9
    s.followers["c1"] = object()
    s.submit(eng.Destroy("c1", "app:x"))
    kinds = []
    while not s.q.empty():
        i = s.q.get()
        kinds.append((type(i).__name__, i.cid))
    assert kinds == [("Seen", "c1"), ("Start", "c3"), ("Destroy", "c1")]


def test_cursors_are_per_container_id_so_a_recreate_has_its_own():
    assert ls.ship_key("annex-app-x", "abcdef0123456789") == "ship:annex-app-x:abcdef012345"
    assert watch_docker.classify({"com.docker.compose.project": "annex-app-x", "com.kaldi.annex.app": "x"}) == ("app:x", False)


def test_the_log_size_guard_signal():
    e = eng.Engine()
    now = 1000.0
    acts = e.handle(now, eng.LogSample(500, 2048, ["2026-10-01"]))
    assert [(a.kind, a.subject) for a in acts] == [("open", "platform:logs")]
    assert e.handle(now + 1, eng.LogSample(1900, 2048, [])) == []          # under the limit, not yet well under: stay open
    acts = e.handle(now + 2, eng.LogSample(1000, 2048, []))
    assert ("resolve", "platform:logs") in [(a.kind, a.subject) for a in acts]


def test_a_long_python_stack_keeps_its_end_where_it_was_raised_and_node_keeps_its_start():
    frames = "".join(f'  File "/lib/x{i}.py", line {i}, in f{i}\n    call()\n' for i in range(400))
    py = "Traceback (most recent call last):\n" + frames + '  File "/app/main.py", line 9, in index\nValueError: x'
    p = ls.parse_app_line(json.dumps({"level": "error", "msg": "unhandled error", "exc": "ValueError: x", "stack": py}))
    assert len(p.fields["stack"]) <= ls.MAX_STACK + 20 and p.fields["stack"].endswith("ValueError: x")
    assert 'in index' in p.fields["stack"] and p.fields["stack"].startswith("…")
    node = "Error: x\n" + "    at fn (/app/a.js:1:1)\n" * 1000
    q = ls.parse_app_line(json.dumps({"level": "error", "msg": "m", "stack": node}))
    assert q.fields["stack"].startswith("Error: x") and q.fields["stack"].endswith("[truncated]")


def test_authelia_at_debug_keeps_only_successful_sign_ins():
    ok = 'time="x" level=debug msg="Successful 1FA authentication attempt made by user \'sam\'" method=POST path=/api/firstfactor remote_ip=172.21.0.1'
    mark = 'time="x" level=debug msg="Mark 1FA authentication attempt made by user \'sam\'" method=POST path=/api/firstfactor'
    chatter = ['time="x" level=debug msg="Checking the authentication backend for an updated profile for user" username=sam',
               'time="x" level=debug msg="Check authorization of subject username=sam groups=builders ip=127.0.0.1 and object https://x/ (method GET)."']
    kept = ls.parse_authelia_line(ok)
    assert kept is not None and kept.level == "debug" and "user 'sam'" in kept.msg
    assert ls.parse_authelia_line(mark) is None and all(ls.parse_authelia_line(c) is None for c in chatter)
