"""Byte budgets and bounded queues (D86): the log sink, the usage buffer and the engine's input queue."""
import queue
from datetime import datetime, timezone

from annex_ops import engine as eng
from annex_ops import incidents, logstore, usage
from annex_ops.logstore import Row, Sink


def row(i: int, msg: str = "x", fields=None) -> Row:
    return Row(datetime(2026, 10, 22, tzinfo=timezone.utc), i % 1000, "app", "c", 1, "stdout", "info", None, msg, fields, f"h{i}")


def urow(i: int, path: str = "/") -> usage.Row:
    return usage.Row(1.0 + i, "app", "cm", "GET", path, 200, 5)


def down(*_args):
    raise RuntimeError("database down")


# --- log sink ----------------------------------------------------------------------------
def test_row_bytes_counts_text_and_fields():
    small, big = logstore.row_bytes(row(1)), logstore.row_bytes(row(1, "y" * 1000, {"k": "v" * 500}))
    assert small >= logstore.ROW_OVERHEAD and big - small > 1500


def test_sink_drops_the_oldest_by_rows_and_counts():
    s = Sink(max_rows=3, max_bytes=10**9)
    for i in range(5):
        s.add(row(i))
    assert [r.h for r in s.buf] == ["h2", "h3", "h4"] and s.lost == 2
    assert s.bytes == sum(s.sizes) == sum(logstore.row_bytes(r) for r in s.buf)


def test_sink_drops_the_oldest_by_bytes_and_counts():
    one = logstore.row_bytes(row(0, "z" * 1000))
    s = Sink(max_rows=10**6, max_bytes=one * 3 + 10)
    for i in range(6):
        s.add(row(i, "z" * 1000))
    assert [r.h for r in s.buf] == ["h3", "h4", "h5"] and s.lost == 3 and s.bytes <= s.max_bytes
    assert s.status()["bytes"] == s.bytes and s.status()["max_bytes"] == s.max_bytes


def test_one_huge_row_is_dropped_rather_than_kept_over_budget():
    s = Sink(max_rows=100, max_bytes=2000)
    s.add(row(1, "q" * 5000))
    assert s.buf == [] and s.lost == 1 and s.bytes == 0


def test_a_failed_insert_puts_rows_back_within_both_budgets(monkeypatch):
    monkeypatch.setattr(logstore, "insert_rows", down)
    one = logstore.row_bytes(row(0, "z" * 1000))
    s = Sink(max_rows=10**6, max_bytes=one * 4 + 10)
    for i in range(3):
        s.add(row(i, "z" * 1000))
    assert s.flush() is False and len(s.buf) == 3
    for i in range(3, 6):
        s.add(row(i, "z" * 1000))
    assert s.flush() is False
    assert [r.h for r in s.buf] == ["h2", "h3", "h4", "h5"] and s.lost == 2 and s.bytes == sum(s.sizes) <= s.max_bytes


def test_a_good_flush_empties_the_buffer_and_the_byte_count(monkeypatch):
    monkeypatch.setattr(logstore, "insert_rows", lambda rows, known: 0)
    s = Sink(max_rows=10, max_bytes=10**6)
    s.add(row(1))
    assert s.flush() is True and s.buf == [] and s.sizes == [] and s.bytes == 0 and s.lost == 0


def test_sink_defaults_come_from_the_log_settings(monkeypatch):
    monkeypatch.setenv("OPS_LOG_BUFFER_MB", "1")
    monkeypatch.setenv("OPS_LOG_BUFFER_ROWS", "7")
    s = Sink()
    assert (s.max_rows, s.max_bytes) == (7, 2**20)
    monkeypatch.delenv("OPS_LOG_BUFFER_MB")
    monkeypatch.delenv("OPS_LOG_BUFFER_ROWS")
    d = Sink()
    assert (d.max_rows, d.max_bytes) == (20000, 20 * 2**20)


# --- usage buffer --------------------------------------------------------------------------
def fill(ing, rows):
    ing.buf += rows
    ing.sizes += [usage.row_bytes(r) for r in rows]
    ing.bytes = sum(ing.sizes)


def test_usage_retry_buffer_is_capped_by_rows_and_counted(monkeypatch):
    monkeypatch.setattr(usage, "insert_rows", down)
    ing = usage.Ingest(max_rows=4, max_bytes=10**9)
    fill(ing, [urow(i) for i in range(3)])
    assert ing.flush() == 0 and len(ing.buf) == 3
    fill(ing, [urow(i) for i in range(3, 6)])
    assert ing.flush() == 0
    assert [r.ts for r in ing.buf] == [3.0, 4.0, 5.0, 6.0] and ing.dropped == 2 and ing.bytes == sum(ing.sizes)


def test_usage_retry_buffer_is_capped_by_bytes(monkeypatch):
    monkeypatch.setattr(usage, "insert_rows", down)
    one = usage.row_bytes(urow(0, "/" + "p" * 100))
    ing = usage.Ingest(max_rows=10**6, max_bytes=one * 2 + 5)
    fill(ing, [urow(i, "/" + "p" * 100) for i in range(5)])
    assert ing.flush() == 0
    assert [r.ts for r in ing.buf] == [4.0, 5.0] and ing.dropped == 3 and ing.bytes <= ing.max_bytes


def test_usage_handle_stays_within_budget_while_the_database_is_down(monkeypatch):
    monkeypatch.setattr(usage, "insert_rows", down)
    ing = usage.Ingest(max_rows=250, max_bytes=10**9)
    monkeypatch.setattr(usage.follow_logs, "access_entry", lambda line: {})
    n = [0]

    def fake_parse(entry, paths):
        n[0] += 1
        return urow(n[0])

    monkeypatch.setattr(usage, "parse_access_entry", fake_parse)
    monkeypatch.setattr(ing.health, "get", lambda: {})
    for _ in range(1000):
        ing.handle(0, "stdout", "x")
    assert len(ing.buf) <= 250 and ing.dropped >= 700 and ing.dropped + len(ing.buf) == 1000


def test_usage_defaults_come_from_the_settings():
    ing = usage.Ingest()
    assert (ing.max_rows, ing.max_bytes) == (50000, 8 * 2**20)


# --- the engine's queue --------------------------------------------------------------------
def make_manager(soft: int, hard: int):
    m = incidents.Manager()
    m.queue_soft = soft
    m.q = queue.Queue(maxsize=hard)
    return m


def test_requests_are_dropped_and_counted_past_the_soft_limit():
    m = make_manager(3, 6)
    for i in range(10):
        m.submit(eng.Request("app", float(i), 500))
    assert m.q.qsize() == 3 and m.dropped == 7


def test_docker_events_are_kept_when_requests_have_filled_their_share():
    m = make_manager(3, 6)
    for i in range(10):
        m.submit(eng.Request("app", float(i), 500))
    m.submit(eng.Destroy("cid", "app:x"))
    m.submit(eng.Destroy("cid2", "app:x"))
    m.submit(eng.Destroy("cid3", "app:x"))
    assert m.q.qsize() == 6 and m.dropped == 7
    assert [type(m.q.get_nowait()).__name__ for _ in range(6)] == ["Request"] * 3 + ["Destroy"] * 3


def test_a_full_queue_drops_a_request_without_blocking():
    m = make_manager(10, 2)
    m.submit(eng.Destroy("a", "app:x"))
    m.submit(eng.Destroy("b", "app:x"))
    m.submit(eng.Request("app", 1.0, 500))  # queue is full: dropped, and this returns
    assert m.q.qsize() == 2 and m.dropped == 1
