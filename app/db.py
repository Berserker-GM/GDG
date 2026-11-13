"""Engine and session management."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC timestamp; portable across SQLite and MySQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(database_url: str) -> str:
    """Pin plain Postgres URLs to psycopg2, the driver in requirements.txt.

    Heroku/Render-style ``postgres://`` is rejected by SQLAlchemy 2, and since SQLAlchemy 2.1 a bare
    ``postgresql://`` defaults to psycopg 3, which is not installed. An explicit driver is kept.
    """
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg2://" + database_url[len(prefix):]
    return database_url


def configure(database_url: str | None = None) -> Engine:
    """(Re)create the engine. Called lazily, and by tests to point at a temp database."""
    global _engine, _SessionLocal
    settings = get_settings()
    url = make_url(normalize_database_url(database_url or settings.database_url))
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    else:
        # Keep the pool small: hosted Postgres poolers in session mode (e.g. Supabase) cap total client
        # connections, and a deploy briefly runs the old and new instance side by side.
        kwargs["pool_recycle"] = 1800
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_timeout"] = settings.db_pool_timeout
    if _engine is not None:
        _engine.dispose()
    from app import cache

    cache.clear()  # cached responses belong to the previous database
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    engine = get_engine()
    Base.metadata.create_all(engine)
    if engine.url.get_backend_name() == "postgresql":
        # Only touch tables that still need it: ALTER TABLE requires ownership, so running it on
        # every startup would crash a deploy whose role can use, but does not own, the tables.
        tables = [t.name for t in Base.metadata.sorted_tables]
        with engine.connect() as conn:
            pending = conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = ANY(:tables) AND NOT c.relrowsecurity"
                ),
                {"tables": tables},
            ).scalars().all()
            for table in pending:
                conn.execute(text(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY'))
            conn.commit()


STARTUP_RETRY_DELAYS = (3, 6, 12, 24)


def init_db_with_retry(delays: tuple[float, ...] = STARTUP_RETRY_DELAYS, sleep: Callable[[float], None] = time.sleep) -> None:
    """``init_db`` with retries, so a deploy that overlaps the old instance (and finds the connection
    limit full, e.g. EMAXCONNSESSION) waits for connections to free up instead of exiting."""
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            init_db()
            return
        except Exception as exc:
            if delay is None:
                raise
            log.warning("Database not ready (attempt %s/%s): %s; retrying in %ss", attempt, len(delays) + 1, exc, delay)
            sleep(delay)


def new_session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
