import time
from collections import deque


class WindowLimiter:
    """Single-process limit. Keys come only from the finite trusted identity mapping."""
    def __init__(self, limit: int):
        self.limit = limit
        self._windows: dict[tuple[str, str], deque[float]] = {}

    def take(self, tenant_id: str, user_id: str) -> bool:
        # Called from the async request loop without await; no thread-shared use.
        now = time.monotonic()
        window = self._windows.setdefault((tenant_id, user_id), deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True
