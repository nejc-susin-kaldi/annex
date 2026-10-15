"""A tiny in-memory sliding-window limiter (per key), used for webhook events and for logging rejected requests."""
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, deque] = {}

    def allow(self, key: str, now: float) -> bool:
        """Count a request for `key` and say whether it is within the limit. A refused request isn't counted."""
        dq = self.hits.setdefault(key, deque())
        while dq and now - dq[0] >= self.window_s:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        if len(self.hits) > 5000:  # forget idle keys
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= self.window_s]:
                del self.hits[k]
        return True
