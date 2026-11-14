"""In-memory cache for public responses that only change when a run publishes.

Entries are keyed by name and tagged with the published run id they were built from; a lookup with
a different run id is a miss. ``clear()`` is called when a run publishes and when the database is
(re)configured, so a new database never serves another one's cached data.

The status body is cached separately for 60 seconds (``get_status``); ``clear_status()`` drops it
when a run starts or finishes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_entries: dict[str, tuple[int, Any]] = {}


def get_or_build(name: str, run_id: int, build: Callable[[], Any]) -> Any:
    with _lock:
        hit = _entries.get(name)
        if hit is not None and hit[0] == run_id:
            return hit[1]
    value = build()  # outside the lock: building runs DB queries
    with _lock:
        _entries[name] = (run_id, value)
    return value


STATUS_TTL_SECONDS = 60.0

_status: tuple[float, Any] | None = None  # (expires_at monotonic, body)


def get_status(build: Callable[[], Any], ttl: float = STATUS_TTL_SECONDS) -> Any:
    """Time-based cache for /api/leaderboard/status: rebuilt at most once per ``ttl`` seconds."""
    global _status
    now = time.monotonic()
    with _lock:
        if _status is not None and _status[0] > now:
            return _status[1]
    value = build()
    with _lock:
        _status = (time.monotonic() + ttl, value)
    return value


def clear_status() -> None:
    """Called when a run starts or finishes, so ``running`` flips without waiting for the TTL."""
    global _status
    with _lock:
        _status = None


def clear() -> None:
    global _status
    with _lock:
        _entries.clear()
        _status = None
