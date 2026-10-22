"""The stored logs' partitions, retention, size guard and query parts (M2.2): pure functions only."""
import threading
import time
from datetime import date, datetime, timezone

import pytest

from annex_ops import logstore as st
from annex_ops.errors import OpsError

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def test_partitions_for_today_and_two_days_ahead():
    assert st.partition_days(NOW) == [date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)]
    assert st.partition_days(datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc))[-1] == date(2027, 1, 2)


def test_a_line_belongs_to_its_utc_day():
    assert st.day_of(datetime(2026, 10, 16, 23, 59, 59, tzinfo=timezone.utc)) == date(2026, 10, 16)
    # 01:30 in Ljubljana (UTC+2) is still the 15th in UTC
    from zoneinfo import ZoneInfo
    assert st.day_of(datetime(2026, 10, 16, 1, 30, tzinfo=ZoneInfo("Europe/Ljubljana"))) == date(2026, 10, 15)


def test_partition_sql():
    assert st.partition_name(date(2026, 10, 16)) == "logs_20261016"
    sql = st.create_partition_sql(date(2026, 12, 31))
    assert "annex_ops.logs_20261231 PARTITION OF annex_ops.logs" in sql
    assert "FROM ('2026-12-31 00:00:00+00') TO ('2027-01-01 00:00:00+00')" in sql


def test_names_round_trip_and_foreign_names_are_ignored():
    assert st.partition_from_name("logs_20261016") == date(2026, 10, 16)
    assert st.partition_from_name("logs_dedupe") is None and st.partition_from_name("logs_2026") is None


def test_retention_drops_only_whole_partitions_older_than_the_window():
    days = [date(2026, 10, d) for d in range(1, 19)]
    expired = st.expired_days(days, NOW, 14)       # cutoff 2 Oct 10:00: a day is dropped once it has ended before that
    assert expired == [date(2026, 10, 1)]
    assert date(2026, 10, 2) not in expired        # it still holds lines from after 10:00
    assert st.expired_days(days, NOW, 1) == [date(2026, 10, d) for d in range(1, 15)]
    assert st.expired_days([], NOW, 14) == []


def test_the_size_guard_drops_oldest_first_and_never_the_newest():
    mb = 2**20
    sizes = {date(2026, 10, 10): 300 * mb, date(2026, 10, 11): 300 * mb, date(2026, 10, 12): 300 * mb, date(2026, 10, 13): 300 * mb}
    assert st.trim_plan(sizes, 2000 * mb) == []
    assert st.trim_plan(sizes, 700 * mb) == [date(2026, 10, 10), date(2026, 10, 11)]
    assert st.trim_plan(sizes, 1) == [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]   # the newest stays
    assert st.trim_plan({date(2026, 10, 13): 999 * mb}, 1) == []


def test_levels_include_the_ones_above():
    assert st.levels_from("warn") == ["warn", "error"] and st.levels_from("debug") == ["debug", "info", "warn", "error"]
    with pytest.raises(OpsError) as e:
        st.levels_from("fatal")
    assert "debug, info, warn or error" in e.value.message


def test_grep_text_is_literal():
    assert st.like_pattern("novak") == "%novak%"
    assert st.like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


# --- B6: add() only enqueues; the sink thread inserts; flush() waits on it ------------------------------------------
def _row(i=0):
    return st.Row(NOW, i, "x", "c", 1, "stdout", "info", None, f"m{i}", None, f"{i:032x}")


def test_add_never_touches_the_database_even_for_a_full_batch(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("the database was touched")
    monkeypatch.setattr(st.db, "connect", fail)
    monkeypatch.setattr(st, "insert_rows", fail)
    sink = st.Sink(max_rows=10**6, max_bytes=10**9)
    for i in range(st.BATCH * 2):
        sink.add(_row(i))
    assert sink.status()["buffered"] == st.BATCH * 2 and sink.wake.is_set()


def test_flush_waits_for_the_sink_thread_and_does_not_insert_itself(monkeypatch):
    inserting = []

    def insert(rows, known):
        inserting.append(threading.current_thread().name)
        time.sleep(0.2)
        return 0

    monkeypatch.setattr(st, "insert_rows", insert)
    sink = st.Sink(max_rows=100, max_bytes=10**6)
    sink.start()
    sink.add(_row(1))
    start = time.monotonic()
    assert sink.flush(5) is True
    assert time.monotonic() - start >= 0.2 and inserting == ["log-sink"] and sink.status()["buffered"] == 0


def test_flush_times_out_while_an_insert_hangs_and_the_rows_are_not_called_stored(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(st, "insert_rows", lambda rows, known: gate.wait(5) and 0)
    sink = st.Sink(max_rows=100, max_bytes=10**6)
    sink.start()
    sink.add(_row(1))
    start = time.monotonic()
    assert sink.flush(0.3) is False and time.monotonic() - start < 2
    gate.set()
    assert sink.flush(5) is True


def test_flush_returns_false_at_once_when_the_insert_fails_and_keeps_the_rows(monkeypatch):
    def boom(rows, known):
        raise OSError("down")

    monkeypatch.setattr(st, "insert_rows", boom)
    sink = st.Sink(max_rows=100, max_bytes=10**6)
    sink.start()
    sink.add(_row(1))
    start = time.monotonic()
    assert sink.flush(5) is False and time.monotonic() - start < 3
    assert sink.status()["buffered"] == 1 and sink.lost == 0


def test_rows_added_during_an_insert_are_not_covered_by_it(monkeypatch):
    gate, started = threading.Event(), threading.Event()
    seen = []

    def insert(rows, known):
        seen.append(len(rows))
        started.set()
        gate.wait(5)
        return 0

    monkeypatch.setattr(st, "insert_rows", insert)
    sink = st.Sink(max_rows=100, max_bytes=10**6)
    sink.add(_row(1))
    t = threading.Thread(target=sink._insert_pending)
    t.start()
    assert started.wait(2)
    sink.add(_row(2))                          # arrives while row 1's batch is in flight
    with sink.lock:
        assert sink.stored == 0 and sink.added == 2
    gate.set()
    t.join(2)
    with sink.lock:
        assert sink.stored == 1                # only the first row is stored; a cursor past row 2 would be a gap
    assert sink.flush() is True and seen == [1, 1]


def test_a_flush_with_nothing_buffered_is_true_and_rows_the_budget_dropped_do_not_block_it(monkeypatch):
    monkeypatch.setattr(st, "insert_rows", lambda rows, known: 0)
    sink = st.Sink(max_rows=2, max_bytes=10**6)
    assert sink.flush() is True
    for i in range(5):
        sink.add(_row(i))
    assert sink.lost == 3 and sink.flush() is True and sink.lost == 3


def test_a_sink_without_a_thread_inserts_inline_in_flush(monkeypatch):
    got = []
    monkeypatch.setattr(st, "insert_rows", lambda rows, known: got.append(len(rows)) or 0)
    sink = st.Sink(max_rows=100, max_bytes=10**6)
    sink.add(_row(1))
    assert got == [] and sink.flush() is True and got == [1]
