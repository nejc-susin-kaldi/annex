"""The alert sender (D90): the outbox's retries, give-up and restart safety, with fakes for the table and the sends."""
import copy

from annex_ops import sender as s
from annex_ops.errors import OpsError

USERS = {"sam": {"email": "sam@annex.example", "groups": []},
         "kaldi-admin": {"email": "admin@annex.example", "groups": ["admins"]}}
ACTION = {"signals": {"stopped": {"since": 1_700_000_000.0, "detail": "stopped for 2 min", "cid": "c1"}},
          "opened_at": 1_700_000_000.0, "duration_s": 0}


def msg(*deliveries):
    return {"title": "[Annex] probe is down", "data": {"title": "probe is down", "body": "x"}, "slack_text": "probe is down",
            "deliveries": [dict(d) for d in deliveries]}


EMAIL = {"via": "email", "to": ["sam@annex.example", "admin@annex.example"], "status": "pending"}
SLACK = {"via": "slack", "to": "#annex-alerts", "status": "pending"}


class FakeTransport:
    """Fails each send with the next error queued for its target (an OpsError, or None = success)."""

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.sent = []

    def _go(self, key, what):
        errs = self.plan.get(key) or []
        err = errs.pop(0) if errs else None
        if err is not None:
            raise err
        self.sent.append(what)

    def email(self, to, title, data):
        self._go(tuple(to), ("email", tuple(to)))

    def slack(self, channel, text):
        self._go(channel, ("slack", channel))


class FakeStore:
    def __init__(self, rows=()):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.saves = 0

    def add(self, kind, subject, action):
        i = max(self.rows, default=0) + 1
        self.rows[i] = {"id": i, "kind": kind, "subject": subject, "action": action, "message": None, "attempts": 0,
                        "status": "pending"}
        return i

    def pending(self):
        return [copy.deepcopy(r) for r in sorted(self.rows.values(), key=lambda r: r["id"]) if r["status"] == "pending"]

    def save_message(self, id_, message):
        self.saves += 1
        self.rows[id_]["message"] = copy.deepcopy(message)

    def finish(self, id_, status, attempts, retry_in, error):
        self.rows[id_].update(status=status, attempts=attempts, retry_in=retry_in, error=error)

    def housekeeping(self):
        pass


def sender_with(store, transport, monkeypatch):
    snd = s.Sender(store, transport)
    monkeypatch.setattr(snd, "compose", lambda row: msg(EMAIL, SLACK))
    return snd


BACKOFF = [10.0, 60.0]


# --- pure ---------------------------------------------------------------------------------------------------------
def test_backoff_parses_and_its_last_step_repeats():
    assert s.parse_backoff("10,60") == [10.0, 60.0]
    assert s.parse_backoff("") == s.parse_backoff("x") == s.parse_backoff("0,5") == [10.0, 60.0]
    assert [s.retry_delay(n, BACKOFF) for n in (1, 2, 3, 9)] == [10, 60, 60, 60]


def test_due_is_the_oldest_row_of_each_subject_once_its_retry_time_has_come():
    rows = [{"id": 3, "subject": "app:a"}, {"id": 1, "subject": "app:a"}, {"id": 2, "subject": "platform:disk"}]
    assert [r["id"] for r in s.due(rows, 100, {})] == [1, 2]
    # a retrying "opened" holds back the "resolved" of the same subject, not other subjects
    assert [r["id"] for r in s.due(rows, 100, {1: 110})] == [2]
    assert [r["id"] for r in s.due(rows, 110, {1: 110})] == [1, 2]


def test_which_failures_are_worth_another_try():
    assert s.retryable(OpsError(502, "smtp down")) and s.retryable(OpsError(503, "slack")) and s.retryable(OpsError(429, "limit"))
    assert s.retryable(ConnectionError("x"))
    assert not s.retryable(OpsError(403, "outside the domains")) and not s.retryable(OpsError(400, "bad"))


def test_compose_addresses_the_owner_and_admins_and_the_alert_channel():
    m = s.compose("open", "app:probe", ACTION, {"owner": "sam"}, USERS, True, "#annex-alerts", "https://console/status",
                  "https://probe", ["line 1", "line 2"])
    assert m["title"] == "[Annex] probe is down"
    assert m["deliveries"] == [{"via": "email", "to": ["sam@annex.example", "admin@annex.example"], "status": "pending"},
                               {"via": "slack", "to": "#annex-alerts", "status": "pending"}]
    assert "line 2" in m["data"]["details"] and m["data"]["link"] == "https://probe"
    r = s.compose("resolve", "platform:disk", {"signals": {"disk": {}}, "opened_at": 0, "duration_s": 600}, {}, USERS, False,
                  "#annex-alerts", "", "", ["ignored"])
    assert r["title"].startswith("[Annex] resolved: platform disk") and "details" not in r["data"]
    assert r["deliveries"] == [{"via": "email", "to": ["admin@annex.example"], "status": "pending"}]


def test_attempt_marks_each_delivery_and_saves_after_every_one():
    m, saves = msg(EMAIL, SLACK), []
    s.attempt(m, FakeTransport({"#annex-alerts": [OpsError(502, "slack unreachable")]}), lambda: saves.append(1))
    assert [d["status"] for d in m["deliveries"]] == ["sent", "pending"] and len(saves) == 2
    assert m["deliveries"][1]["error"] == "slack unreachable"
    assert s.outcome(m, 1, 3) == ("pending", "slack #annex-alerts: slack unreachable")


def test_a_refused_group_email_is_split_so_the_others_still_hear():
    t = FakeTransport({("sam@annex.example", "admin@annex.example"): [OpsError(403, "x is outside")],
                       ("sam@annex.example",): [OpsError(403, "sam@annex.example is outside")]})
    m = msg(EMAIL)
    s.attempt(m, t, lambda: None)
    assert [d["status"] for d in m["deliveries"]] == ["split", "refused", "sent"]
    assert t.sent == [("email", ("admin@annex.example",))]
    status, error = s.outcome(m, 1, 3)
    assert status == "failed" and "sam@annex.example is outside" in error


def test_outcome_sent_pending_given_up_and_nobody_to_tell():
    assert s.outcome(msg(dict(EMAIL, status="sent")), 1, 3) == ("sent", None)
    pending = msg(dict(EMAIL, error="smtp down"))
    assert s.outcome(pending, 2, 3)[0] == "pending"
    status, error = s.outcome(pending, 3, 3)
    assert status == "failed" and error.startswith("gave up after 3 attempts") and "smtp down" in error
    assert s.outcome(msg(), 1, 3)[0] == "failed"


# --- the thread's work, with fakes ------------------------------------------------------------------------------------
def test_a_failing_send_is_retried_with_backoff_then_delivered(monkeypatch):
    store = FakeStore()
    store.add("open", "app:probe", ACTION)
    t = FakeTransport({("sam@annex.example", "admin@annex.example"): [OpsError(502, "smtp down"), OpsError(502, "smtp down")]})
    snd = sender_with(store, t, monkeypatch)
    snd.process(store.pending()[0], 0.0, BACKOFF, 3)
    assert store.rows[1]["status"] == "pending" and store.rows[1]["attempts"] == 1 and snd.retry_at[1] == 10.0
    assert t.sent == [("slack", "#annex-alerts")]                         # Slack went out at once, alone
    assert s.due(store.pending(), 9.9, snd.retry_at) == []
    snd.process(store.pending()[0], 10.0, BACKOFF, 3)
    assert store.rows[1]["attempts"] == 2 and snd.retry_at[1] == 70.0
    assert snd.process(store.pending()[0], 70.0, BACKOFF, 3) == "sent"
    assert store.rows[1]["status"] == "sent" and 1 not in snd.retry_at and snd.sent == 1
    # Slack was not posted again by the retries
    assert t.sent == [("slack", "#annex-alerts"), ("email", ("sam@annex.example", "admin@annex.example"))]


def test_a_send_that_fails_three_times_is_given_up_and_logged(monkeypatch, capsys):
    store = FakeStore()
    store.add("open", "platform:disk", ACTION)
    down = [OpsError(502, "smtp down")] * 5
    snd = sender_with(store, FakeTransport({("sam@annex.example", "admin@annex.example"): list(down), "#annex-alerts": list(down)}),
                      monkeypatch)
    for now in (0.0, 10.0, 70.0):
        snd.process(store.pending()[0], now, BACKOFF, 3)
    row = store.rows[1]
    assert row["status"] == "failed" and row["attempts"] == 3 and row["retry_in"] is None
    assert "gave up after 3 attempts" in row["error"] and snd.failed == 1 and store.pending() == []
    assert "was not delivered" in capsys.readouterr().err


def test_the_message_is_composed_once_and_retries_send_the_same_text(monkeypatch):
    store = FakeStore()
    store.add("open", "app:probe", ACTION)
    snd = s.Sender(store, FakeTransport({"#annex-alerts": [OpsError(502, "x")]}))
    calls = []
    monkeypatch.setattr(snd, "compose", lambda row: calls.append(1) or msg(SLACK))
    snd.process(store.pending()[0], 0.0, BACKOFF, 3)
    snd.process(store.pending()[0], 10.0, BACKOFF, 3)
    assert calls == [1] and store.rows[1]["status"] == "sent"


def test_after_a_restart_pending_rows_are_retried_and_delivered_parts_are_not_sent_again(monkeypatch):
    # what the last process saved before it died: the email went out, the Slack post is still owed
    saved = msg(dict(EMAIL, status="sent"), dict(SLACK, error="slack unreachable"))
    store = FakeStore([{"id": 7, "kind": "open", "subject": "app:probe", "action": ACTION, "message": saved, "attempts": 1,
                        "status": "pending"},
                       {"id": 8, "kind": "resolve", "subject": "app:probe", "action": ACTION, "message": None, "attempts": 0,
                        "status": "pending"}])
    t = FakeTransport()
    snd = sender_with(store, t, monkeypatch)       # a new process: no retry times in memory
    monkeypatch.setattr(s.config, "tuning", _tuning)
    snd.cycle(1000.0)
    assert t.sent == [("slack", "#annex-alerts")]  # only the part that was owed; the resolve waits for its open
    assert store.rows[7]["status"] == "sent" and store.rows[7]["attempts"] == 2 and store.rows[8]["status"] == "pending"
    snd.cycle(1000.5)
    assert store.rows[8]["status"] == "sent"


def _tuning():
    from annex_ops.engine import Tuning
    return Tuning()


def test_a_sent_delivery_is_saved_before_the_next_one_is_tried(monkeypatch):
    """A crash between two deliveries can't repeat the first: the store has it as sent before Slack is called."""
    store = FakeStore()
    store.add("open", "app:probe", ACTION)
    seen_in_store = []

    class Peek(FakeTransport):
        def slack(self, channel, text):
            seen_in_store.append(store.rows[1]["message"]["deliveries"][0]["status"])
            super().slack(channel, text)

    snd = sender_with(store, Peek(), monkeypatch)
    snd.process(store.pending()[0], 0.0, BACKOFF, 3)
    assert seen_in_store == ["sent"]


def test_the_engine_hands_messages_to_the_outbox_with_wall_times(monkeypatch):
    from annex_ops import incidents as inc
    from annex_ops.engine import Action

    rows, woken = [], []

    class Outbox:
        def add(self, kind, subject, action):
            rows.append((kind, subject, action))

    m = inc.Manager(outbox=Outbox(), state=object())
    m.wake_sender = lambda: woken.append(1)
    m.offset = 1000.0
    m.notify("remind", Action("remind", "app:probe", signals={"down": {"since": 5.0, "detail": "x", "cid": None}},
                              duration_s=3600, opened_at=5.0))
    assert rows == [("remind", "app:probe", {"signals": {"down": {"since": 1005.0, "detail": "x", "cid": None}},
                                             "duration_s": 3600, "opened_at": 1005.0})]
    assert woken == [1]
