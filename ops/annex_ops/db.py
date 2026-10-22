"""Postgres access through one small connection pool per process (D84).

`with db.connect() as conn:` is unchanged for every caller: the connection is autocommit, and on leaving the block it goes
back to the pool, which rolls back anything left open and drops a connection that broke. The pool is opened on first use,
so importing this module (and the unit tests) never touches the network. Background threads and the web threadpool share it;
when all connections are busy for `POOL_WAIT_S` the caller gets an OpsError 503, never a hang. Size: `OPS_DB_POOL_MAX`
(ops 4; the console and webhook receiver, separate containers running this same package, 2: compose sets it)."""
import threading
from contextlib import ExitStack, contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from . import config
from .errors import OpsError

POOL_MIN = 1
POOL_WAIT_S = 10  # how long a caller waits for a free connection

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def _make_pool(url: str, max_size: int) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=min(POOL_MIN, max_size), max_size=max_size, open=False, timeout=POOL_WAIT_S,
                          max_idle=300, kwargs={"autocommit": True, "connect_timeout": 5},
                          check=ConnectionPool.check_connection,  # a connection the database restarted under is replaced, not handed out
                          name="annex-ops")
    pool.open(wait=False)
    return pool


def pool_size() -> int:
    return max(1, config.DB_POOL_MAX)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = _make_pool(config.DATABASE_URL, pool_size())
    return _pool


def stats() -> dict:
    """The pool's own numbers (size, idle, waiting...), for tests; {} before first use."""
    return _pool.get_stats() if _pool is not None else {}


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    if not config.DATABASE_URL:
        raise OpsError(503, "annex-ops has no database configured (OPS_DATABASE_URL). "
                            "The platform team runs scripts/bootstrap.sh and scripts/push.sh")
    with ExitStack() as stack:
        try:
            conn = stack.enter_context(_get_pool().connection())
        except PoolTimeout:
            raise OpsError(503, f"annex-ops is busy: all of its {pool_size()} database connections have been in use "
                                f"for {POOL_WAIT_S} s. Try again in a minute; if it keeps failing the platform team checks "
                                "annex-ops and appdata-db") from None
        except (PoolClosed, psycopg.Error):
            raise OpsError(503, "annex-ops can't reach its database right now. Try again in a minute; if it "
                                "keeps failing the platform team checks appdata-db") from None
        yield conn  # errors in the caller's block pass through untouched; leaving it returns the connection
