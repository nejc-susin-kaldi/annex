"""A small Docker Engine API client over HTTP, for the read-only socket proxy (socket-ro). Standard library only."""
import http.client
import json
import re
import urllib.parse
from datetime import datetime, timezone

from . import config


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"docker api {status}: {message}")


def _conn(timeout: float | None) -> http.client.HTTPConnection:
    host, _, port = config.DOCKER_API.partition(":")
    return http.client.HTTPConnection(host, int(port or 2375), timeout=timeout)


def _get(path: str, timeout: float | None):
    conn = _conn(timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read(300).decode(errors="replace")
        conn.close()
        raise DockerError(resp.status, body.strip())
    return conn, resp


def get_json(path: str, timeout: float = 10):
    conn, resp = _get(path, timeout)
    try:
        return json.load(resp)
    finally:
        conn.close()


def containers() -> list[dict]:
    """All Annex containers (running or not), from /containers/json."""
    flt = urllib.parse.quote(json.dumps({"label": ["com.kaldi.annex=true"]}))
    return get_json(f"/containers/json?all=1&filters={flt}")


def inspect(name_or_id: str) -> dict:
    return get_json(f"/containers/{urllib.parse.quote(name_or_id)}/json")


def stream_events(since: float, filters: dict, timeout: float = 120):
    """Docker events as dicts, until the stream ends or goes quiet for `timeout` seconds (the caller reconnects)."""
    flt = urllib.parse.quote(json.dumps(filters))
    conn, resp = _get(f"/events?since={int(since)}&filters={flt}", timeout)
    try:
        while True:
            line = resp.readline()
            if not line:
                return
            if line.strip():
                yield json.loads(line)
    finally:
        conn.close()


# --- logs ----------------------------------------------------------------------
def frames(read):
    """Demultiplex Docker's log stream for containers without a TTY: 8-byte header (stream type, 3 zero
    bytes, big-endian payload size) then the payload. `read(n)` returns up to n bytes, fewer only at the end."""
    while True:
        head = read(8)
        if len(head) < 8:
            return
        size = int.from_bytes(head[4:8], "big")
        data = read(size) if size else b""
        if len(data) < size:
            return
        yield ("stderr" if head[0] == 2 else "stdout"), data


def lines(frame_iter):
    """(stream, line) for every complete line; text is buffered per stream across frames."""
    buf = {"stdout": b"", "stderr": b""}
    for stream, data in frame_iter:
        buf[stream] += data
        *done, buf[stream] = buf[stream].split(b"\n")
        for ln in done:
            yield stream, ln.decode(errors="replace").rstrip("\r")
    for stream, rest in buf.items():
        if rest:
            yield stream, rest.decode(errors="replace")


_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z$")


def parse_ts(text: str) -> float | None:
    """Docker's RFC3339 timestamp (nanoseconds, Z) -> epoch seconds."""
    m = _TS.match(text)
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    base = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc).timestamp()
    return base + (float("0." + frac) if frac else 0.0)


def split_timestamp(line: str) -> tuple[float | None, str]:
    stamp, _, rest = line.partition(" ")
    ts = parse_ts(stamp)
    return (ts, rest) if ts is not None else (None, line)


def stream_logs(container: str, since: float, follow: bool = True, timeout: float = 120):
    """(timestamp, stream, line) for a container's log lines from `since`, following new ones. Ends when the
    container stops, the stream is quiet for `timeout` seconds, or the connection drops."""
    q = urllib.parse.urlencode({"stdout": 1, "stderr": 1, "timestamps": 1, "follow": int(follow), "since": f"{since:.6f}"})
    conn, resp = _get(f"/containers/{urllib.parse.quote(container)}/logs?{q}", timeout)
    try:
        for stream, line in lines(frames(resp.read)):
            ts, text = split_timestamp(line)
            if ts is not None:
                yield ts, stream, text
    finally:
        conn.close()


def tail_logs(container: str, n: int = 20) -> list[str]:
    """The last n log lines (stdout and stderr, no timestamps), for alert messages. [] if unavailable."""
    try:
        conn, resp = _get(f"/containers/{urllib.parse.quote(container)}/logs?stdout=1&stderr=1&tail={int(n)}", 10)
    except (DockerError, OSError):
        return []
    try:
        return [ln for _, ln in lines(frames(resp.read))]
    except OSError:
        return []
    finally:
        conn.close()
