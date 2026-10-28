"""Postgres access through one small connection pool per process (D84).

`with db.connect() as conn:` is unchanged for every caller: the connection is autocommit, and on leaving the block it goes
back to the pool, which drops it if it broke or was left in a transaction. The pool is our own, about forty lines with no
threads: a connection is opened in the caller's thread when none is idle, a semaphore caps how many exist, and one idle for
`CHECK_AFTER_S` is checked before it is handed out. (psycopg-pool's background workers got its bookkeeping stuck on the VM:
four idle connections, every caller timing out; and a one-shot `ctl` command waited for them at exit.) When all
connections are busy for `POOL_WAIT_S` the caller gets an OpsError 503, never a hang. Size: `OPS_DB_POOL_MAX` (ops 8; the
console and webhook receiver, separate containers running this same package, 2: compose sets it)."""
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import pq
from psycopg.conninfo import make_conninfo

from . import config
from .errors import OpsError

POOL_WAIT_S = 10    # how long a caller waits for a free connection
CHECK_AFTER_S = 30  # an idle connection older than this is checked before it is handed out
MAX_IDLE_S = 300    # and one idle longer than this is closed instead
# libpq: a server that vanished without closing (appdata-db recreated) must fail a query in about a minute, not hang it
CONNECT_OPTS = {"connect_timeout": 5, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
                "keepalives_count": 3, "tcp_user_timeout": 60000}


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
        self.holders: dict[int, tuple[str, float, str]] = {}  # id(conn) -> (thread, since, where it was taken)
        self.last_report = 0.0

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
            self._report()
            raise PoolTimeout(f"no connection free in {POOL_WAIT_S} s")
        conn = None
        try:
            conn = self._take_idle() or psycopg.connect(self.url, autocommit=True, **CONNECT_OPTS)
            with self.lock:
                self.holders[id(conn)] = (threading.current_thread().name, time.monotonic(), _caller())
            yield conn
        finally:
            try:  # whatever a broken connection raises here, the slot below is given back (a leak filled the pool once)
                if conn is not None:
                    with self.lock:
                        self.holders.pop(id(conn), None)
                    try:
                        reusable = (not self.closed and not conn.closed and not conn.broken
                                    and conn.info.transaction_status == pq.TransactionStatus.IDLE)
                    except Exception:  # noqa: BLE001
                        reusable = False
                    if reusable:
                        with self.lock:
                            self.idle.append((conn, time.monotonic()))
                    else:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                self.slots.release()

    def _report(self) -> None:
        """A caller waited POOL_WAIT_S for nothing: say who holds the connections (once a minute at most)."""
        now = time.monotonic()
        with self.lock:
            if now - self.last_report < 60:
                return
            self.last_report = now
            held = sorted(self.holders.values(), key=lambda h: h[1])
        lines = [f"  {name}: {now - since:.1f} s, taken at {where}" for name, since, where in held]
        print("db: the pool is full; holders:\n" + "\n".join(lines), file=sys.stderr, flush=True)

    def close(self, timeout: float = 0) -> None:
        self.closed = True
        with self.lock:
            idle, self.idle = self.idle, []
        for conn, _ in idle:
            conn.close()

    def get_stats(self) -> dict:
        with self.lock:
            return {"pool_max": self.max_size, "pool_available": len(self.idle)}


def _caller() -> str:
    """file:line of the first frame outside this module and contextlib: who asked for the connection."""
    f = sys._getframe(2)
    while f is not None and (f.f_code.co_filename == __file__ or f.f_code.co_filename.endswith("contextlib.py")):
        f = f.f_back
    return f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno} {f.f_code.co_name}" if f else "?"


_pool: Pool | None = None
_lock = threading.Lock()


def _make_pool(url: str, max_size: int) -> Pool:
    return Pool(url, max_size)


def dsn() -> str:
    """OPS_DATABASE_URL, with its host replaced by OPS_DATABASE_HOST when that is set (config.DATABASE_HOST)."""
    if not config.DATABASE_HOST:
        return config.DATABASE_URL
    return make_conninfo(config.DATABASE_URL, host=config.DATABASE_HOST)


def pool_size() -> int:
    return max(1, config.DB_POOL_MAX)


def _get_pool() -> Pool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = _make_pool(dsn(), pool_size())
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
