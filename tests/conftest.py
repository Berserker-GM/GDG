from __future__ import annotations

import pytest

from app import db
from app.config import get_settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    scoring = tmp_path / "scoring.yaml"
    scoring.write_text(
        "points: {skill_badge: 10, course_badge: 3, game_badge: 5, other: 0}\n", encoding="utf-8"
    )
    env = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "SCORING_CONFIG": str(scoring),
        "FETCH_DELAY_SECONDS": "0",
        "FETCH_BACKOFF_BASE": "0",
        "FETCH_CONCURRENCY": "3",
        "ADMIN_TOKEN": "secret-token",
        "ENABLE_SCHEDULER": "false",
        "LEADERBOARD_MODE": "auto",
        "MIN_SUCCESS_RATIO": "0.6",
        "CORS_ORIGINS": "",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    db.configure()
    db.init_db()
    yield get_settings()
    db.get_engine().dispose()
    get_settings.cache_clear()


@pytest.fixture
def session(settings):
    s = db.new_session()
    yield s
    s.close()
