"""Postgres access through one small connection pool per process (D84).

`with db.connect() as conn:` is unchanged for every caller: the connection is autocommit, and on leaving the block it goes
back to the pool, which drops it if it broke or was left in a transaction. The pool is our own, about forty lines with no
threads: a connection is opened in the caller's thread when none is idle, a semaphore caps how many exist, and one idle for
`CHECK_AFTER_S` is checked before it is handed out. (psycopg-pool's background workers got its bookkeeping stuck on the VM:
four idle connections, every caller timing out; and a one-shot `ctl` command waited for them at exit.) When all
connections are busy for `POOL_WAIT_S` the caller gets an OpsError 503, never a hang. Size: `OPS_DB_POOL_MAX` (ops 4; the
console and webhook receiver, separate containers running this same package, 2: compose sets it)."""
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import pq

from . import config
from .errors import OpsError

POOL_WAIT_S = 10    # how long a caller waits for a free connection
CHECK_AFTER_S = 30  # an idle connection older than this is checked before it is handed out
MAX_IDLE_S = 300    # and one idle longer than this is closed instead


class PoolTimeout(Exception):
    pass


class PoolClosed(Exception):
    pass


class Pool:
    def __init__(self, url: str, max_size: int) -> None:
        self.url, self.max_size = url, max_size
        self.slots = threading.BoundedSemaphore(max_size)  # one per connection that exists or is being opened
        self.idle: list[tuple[psycopg.Connection, float]] = []  # (connection, when it came back), newest last
        self.lock = threading.Lock()
        self.closed = False

    def _take_idle(self) -> psycopg.Connection | None:
        while True:
            with self.lock:
                if not self.idle:
                    return None
                conn, since = self.idle.pop()
            age = time.monotonic() - since
            if conn.closed or conn.broken or age > MAX_IDLE_S:
                conn.close()
                continue
            if age > CHECK_AFTER_S:
                try:
                    conn.execute("SELECT 1")
                except psycopg.Error:
                    conn.close()
                    continue
            return conn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        if self.closed:
            raise PoolClosed("the pool is closed")
        if not self.slots.acquire(timeout=POOL_WAIT_S):
            raise PoolTimeout(f"no connection free in {POOL_WAIT_S} s")
        conn = None
        try:
            conn = self._take_idle() or psycopg.connect(self.url, autocommit=True, connect_timeout=5)
            yield conn
        finally:
            if conn is not None:
                reusable = (not self.closed and not conn.closed and not conn.broken
                            and conn.info.transaction_status == pq.TransactionStatus.IDLE)
                if reusable:
                    with self.lock:
                        self.idle.append((conn, time.monotonic()))
                else:
                    conn.close()
            self.slots.release()

    def close(self, timeout: float = 0) -> None:
        self.closed = True
        with self.lock:
            idle, self.idle = self.idle, []
        for conn, _ in idle:
            conn.close()

    def get_stats(self) -> dict:
        with self.lock:
            return {"pool_max": self.max_size, "pool_available": len(self.idle)}


_pool: Pool | None = None
_lock = threading.Lock()


def _make_pool(url: str, max_size: int) -> Pool:
    return Pool(url, max_size)


def pool_size() -> int:
    return max(1, config.DB_POOL_MAX)


def _get_pool() -> Pool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = _make_pool(config.DATABASE_URL, pool_size())
    return _pool


def stats() -> dict:
    """The pool's own numbers (size, idle), for tests; {} before first use."""
    return _pool.get_stats() if _pool is not None else {}


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    if not config.DATABASE_URL:
        raise OpsError(503, "annex-ops has no database configured (OPS_DATABASE_URL). "
                            "The platform team runs scripts/bootstrap.sh and scripts/push.sh")
    try:
        cm = _get_pool().connection()
        conn = cm.__enter__()
    except PoolTimeout:
        raise OpsError(503, f"annex-ops is busy: all of its {pool_size()} database connections have been in use "
                            f"for {POOL_WAIT_S} s. Try again in a minute; if it keeps failing the platform team checks "
                            "annex-ops and appdata-db") from None
    except (PoolClosed, psycopg.Error):
        raise OpsError(503, "annex-ops can't reach its database right now. Try again in a minute; if it "
                            "keeps failing the platform team checks appdata-db") from None
    try:
        yield conn  # errors in the caller's block pass through untouched
    finally:
        cm.__exit__(None, None, None)  # the pool's own finally: the connection goes back (or is closed)
