"""The connection pool wrapper (D84): the call signature, the error mapping and the sizes. No database: the pool is faked."""
from contextlib import contextmanager

import psycopg
import pytest
from annex_ops.db import PoolClosed, PoolTimeout

from annex_ops import config, db
from annex_ops.errors import OpsError


class FakePool:
    def __init__(self, fail=None):
        self.fail, self.returned = fail, 0

    @contextmanager
    def connection(self):
        if self.fail:
            raise self.fail
        try:
            yield "conn"
        finally:
            self.returned += 1


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(db, "_pool", None)
    made = []

    def make(url, max_size):
        made.append((url, max_size))
        return FakePool()
    monkeypatch.setattr(db, "_make_pool", make)
    yield made
    monkeypatch.setattr(db, "_pool", None)


def test_connect_hands_out_a_connection_and_returns_it_even_when_the_block_fails(fake):
    with db.connect() as conn:
        assert conn == "conn"
    assert db._pool.returned == 1
    with pytest.raises(ValueError):
        with db.connect():
            raise ValueError("caller's own error")
    assert db._pool.returned == 2  # the caller's exception is not remapped


def test_a_database_error_inside_the_block_is_not_turned_into_a_503(fake):
    with pytest.raises(psycopg.OperationalError):
        with db.connect():
            raise psycopg.OperationalError("lost")


def test_the_pool_is_made_once_with_the_configured_size(fake, monkeypatch):
    monkeypatch.setattr(config, "DB_POOL_MAX", 2)
    with db.connect():
        pass
    with db.connect():
        pass
    assert fake == [("postgres://x", 2)]


def test_a_full_pool_is_a_clear_503_not_a_hang(fake):
    db._pool = FakePool(fail=PoolTimeout("waited"))
    with pytest.raises(OpsError) as e:
        with db.connect():
            pytest.fail("no connection was handed out")
    assert e.value.status == 503 and "busy" in e.value.message and str(db.POOL_WAIT_S) in e.value.message


@pytest.mark.parametrize("err", [PoolClosed("closed"), psycopg.OperationalError("refused")])
def test_an_unreachable_database_is_the_old_503(fake, err):
    db._pool = FakePool(fail=err)
    with pytest.raises(OpsError) as e:
        with db.connect():
            pass
    assert e.value.status == 503 and "can't reach its database" in e.value.message


def test_no_database_url_is_a_503_before_any_pool_exists(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(OpsError) as e:
        with db.connect():
            pass
    assert e.value.status == 503 and "no database configured" in e.value.message and db._pool is None


def test_building_the_pool_touches_no_network():
    pool = db.Pool("postgres://u:p@127.0.0.1:1/x", 2)
    assert pool.max_size == 2 and pool.idle == [] and not pool.closed
    assert db.pool_size() >= 1


# --- the pool itself (no database: psycopg.connect is faked) ------------------------------------------------------
class FakeConn:
    def __init__(self, n):
        self.n, self.closed, self.broken, self.queries = n, False, False, []
        self.info = type("Info", (), {"transaction_status": db.pq.TransactionStatus.IDLE})()

    def execute(self, q):
        self.queries.append(q)

    def close(self):
        self.closed = True


@pytest.fixture
def opened(monkeypatch):
    made = []

    def connect(url, **kw):
        made.append(FakeConn(len(made)))
        return made[-1]
    monkeypatch.setattr(db.psycopg, "connect", connect)
    return made


def test_an_idle_connection_is_reused(opened):
    p = db.Pool("postgres://x", 2)
    with p.connection() as a:
        pass
    with p.connection() as b:
        assert b is a
    assert len(opened) == 1


def test_a_broken_connection_or_one_left_in_a_transaction_is_closed_not_reused(opened):
    p = db.Pool("postgres://x", 2)
    with p.connection() as a:
        a.broken = True
    with p.connection() as b:
        b.info.transaction_status = db.pq.TransactionStatus.INTRANS
    with p.connection() as c:
        pass
    assert a.closed and b.closed and c is not a and c is not b and len(opened) == 3


def test_the_size_is_a_cap_and_a_full_pool_times_out(opened, monkeypatch):
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.05)
    p = db.Pool("postgres://x", 1)
    with p.connection():
        with pytest.raises(db.PoolTimeout):
            with p.connection():
                pass
    with p.connection():  # the slot came back
        pass


def test_a_failed_open_gives_the_slot_back(monkeypatch):
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.05)

    def refuse(url, **kw):
        raise psycopg.OperationalError("refused")
    monkeypatch.setattr(db.psycopg, "connect", refuse)
    p = db.Pool("postgres://x", 1)
    for _ in range(3):
        with pytest.raises(psycopg.OperationalError):
            with p.connection():
                pass


def test_an_old_idle_connection_is_checked_and_a_dead_one_replaced(opened, monkeypatch):
    p = db.Pool("postgres://x", 1)
    with p.connection() as a:
        pass
    p.idle[0] = (a, p.idle[0][1] - db.CHECK_AFTER_S - 1)

    def dead(q):
        raise psycopg.OperationalError("server closed the connection")
    a.execute = dead
    with p.connection() as b:
        assert b is not a and a.closed


def test_connect_maps_a_real_pool_timeout_and_returns_the_connection(opened, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgres://x")
    monkeypatch.setattr(config, "DB_POOL_MAX", 1)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.05)
    with db.connect():
        with pytest.raises(OpsError) as e:
            with db.connect():
                pass
        assert e.value.status == 503
    with pytest.raises(ValueError):
        with db.connect():
            raise ValueError("the caller's")
    with db.connect() as conn:  # nothing leaked
        assert conn is opened[0]
    monkeypatch.setattr(db, "_pool", None)


def test_a_full_pool_names_its_holders(opened, monkeypatch, capsys):
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.05)
    p = db.Pool("postgres://x", 1)
    with p.connection():
        with pytest.raises(db.PoolTimeout):
            with p.connection():
                pass
    err = capsys.readouterr().err
    assert "the pool is full" in err and "test_db.py" in err and "test_a_full_pool_names_its_holders" in err
    assert p.holders == {}


def test_a_connection_that_raises_on_return_still_gives_its_slot_back(opened, monkeypatch):
    monkeypatch.setattr(db, "POOL_WAIT_S", 0.05)
    p = db.Pool("postgres://x", 1)

    class Boom:
        @property
        def transaction_status(self):
            raise RuntimeError("the connection is gone")
    with p.connection() as a:
        a.info = Boom()
    assert a.closed
    with p.connection() as b:  # the slot came back
        assert b is not a


def test_connections_are_opened_with_keepalives(monkeypatch):
    seen = {}

    def connect(url, **kw):
        seen.update(kw)
        return FakeConn(0)
    monkeypatch.setattr(db.psycopg, "connect", connect)
    with db.Pool("postgres://x", 1).connection():
        pass
    assert seen["keepalives"] == 1 and seen["tcp_user_timeout"] > 0 and seen["autocommit"] is True


def test_the_host_override_replaces_only_the_host(monkeypatch):
    # ops connects to appdata-db by its annex-appdata-only alias (D100); user, password, port and database stay
    monkeypatch.setattr(config, "DATABASE_URL", "postgres://annex_ops:s3cret@appdata-db:5432/annex_ops")
    monkeypatch.setattr(config, "DATABASE_HOST", "appdata-db-platform")
    parts = psycopg.conninfo.conninfo_to_dict(db.dsn())
    assert parts == {"user": "annex_ops", "password": "s3cret", "host": "appdata-db-platform", "port": "5432",
                     "dbname": "annex_ops"}
    monkeypatch.setattr(config, "DATABASE_HOST", "")
    assert db.dsn() == "postgres://annex_ops:s3cret@appdata-db:5432/annex_ops"


def test_the_pool_is_made_with_the_overridden_host(fake, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_HOST", "appdata-db-platform")
    with db.connect():
        pass
    assert "host=appdata-db-platform" in fake[0][0]
