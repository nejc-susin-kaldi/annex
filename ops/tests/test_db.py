"""The connection pool wrapper (D84): the call signature, the error mapping and the sizes. No database: the pool is faked."""
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout

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


def test_the_real_pool_is_built_closed_with_the_wanted_settings():
    pool = db.ConnectionPool("postgres://u:p@127.0.0.1:1/x", min_size=1, max_size=2, open=False, timeout=db.POOL_WAIT_S)
    assert pool.max_size == 2 and pool.closed and pool.timeout == db.POOL_WAIT_S  # constructing touches no network
    assert db.pool_size() >= 1
