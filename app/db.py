"""Engine and session management."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC timestamp; portable across SQLite and MySQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(database_url: str) -> str:
    """Heroku/Render-style ``postgres://`` URLs are rejected by SQLAlchemy 2; use ``postgresql://``."""
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


def configure(database_url: str | None = None) -> Engine:
    """(Re)create the engine. Called lazily, and by tests to point at a temp database."""
    global _engine, _SessionLocal
    url = make_url(normalize_database_url(database_url or get_settings().database_url))
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs["pool_recycle"] = 1800
    if _engine is not None:
        _engine.dispose()
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
        with engine.connect() as conn:
            for table in ("teams", "members", "badges", "runs", "standings"):
                conn.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;"))
            conn.commit()


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
