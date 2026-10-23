import pytest
from sqlalchemy.engine import make_url

from app import db


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgres://u:p@host:5432/app", "postgresql://u:p@host:5432/app"),
        ("postgresql://u:p@host:5432/app", "postgresql://u:p@host:5432/app"),
        ("postgresql+psycopg2://u:p@host/app", "postgresql+psycopg2://u:p@host/app"),
        ("sqlite:///data/leaderboard.db", "sqlite:///data/leaderboard.db"),
        ("mysql+pymysql://u:p@host/postgres://x", "mysql+pymysql://u:p@host/postgres://x"),
    ],
)
def test_normalize_database_url(raw, expected):
    assert db.normalize_database_url(raw) == expected


def test_configure_accepts_postgres_scheme(monkeypatch):
    captured = []

    def fake_create_engine(url, **kwargs):
        captured.append(url)
        return _FakeEngine()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    db.configure("postgres://user:pw@db.example:5432/leaderboard")
    url = make_url(captured[0])
    assert url.drivername == "postgresql"
    assert (url.host, url.port, url.database) == ("db.example", 5432, "leaderboard")


class _FakeEngine:
    def dispose(self) -> None:
        pass
