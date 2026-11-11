"""In-memory cache for public responses that only change when a run publishes.

Entries are keyed by name and tagged with the published run id they were built from; a lookup with
a different run id is a miss. ``clear()`` is called when a run publishes and when the database is
(re)configured, so a new database never serves another one's cached data.
"""

from __future__ import annotations

import threading
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


def clear() -> None:
    with _lock:
        _entries.clear()
