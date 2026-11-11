"""One page view must cost a handful of small queries: cached public responses, one badges query,
and per-member lookups that don't load every standing."""

import asyncio
import re
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import cache, db
from app.api import create_app
from app.config import get_settings
from app.importer import import_form
from app.pipeline import run_pipeline
from tests.helpers import COURSE, SKILL, UUIDS, badge_page, collectors_for, form_row, profile_url, write_form


def _pages(n):
    return {
        UUIDS[i]: badge_page(
            f"S{i}",
            [(f"{i}a", "Skill A", "Oct 1, 2026", *SKILL), (f"{i}b", "Course", "Oct 2, 2026", *COURSE)][: 1 + i % 2],
        )
        for i in range(n)
    }


@pytest.fixture
def solo(settings, tmp_path):
    rows = [form_row(f"U{i}", f"Student {i}", profile_url(i)) for i in range(3)]
    with db.session_scope() as s:
        import_form(s, write_form(tmp_path / "form.xlsx", rows))
    asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    with TestClient(create_app()) as c:
        yield c


@contextmanager
def count_queries():
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db.get_engine()
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _from(statements, table):
    return [s for s in statements if re.search(rf"\bFROM {table}\b", s)]


def test_badges_endpoint_one_badge_query(solo):
    cache.clear()
    with count_queries() as q:
        res = solo.get("/api/leaderboard/badges")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"
    body = res.json()
    board = solo.get("/api/leaderboard").json()
    assert body["run_id"] == board["run_id"]
    assert set(body["members"]) == {str(e["id"]) for e in board["entries"]}
    for e in board["entries"]:
        badges = body["members"][str(e["id"])]
        assert badges and all(set(b) == {"title", "image_url", "earned_date", "counted"} for b in badges)
    assert len(_from(q, "badges")) == 1  # one query for everyone's badges, not one per member

    # Same badges, same order, as the per-member endpoint the page used to call ~100 times.
    for e in board["entries"]:
        detail = solo.get(f"/api/members/{e['id']}").json()
        expected = [
            {k: b[k] for k in ("title", "image_url", "earned_date", "counted")}
            for b in detail["counted_badges"] + detail["uncounted_badges"]
        ]
        assert body["members"][str(e["id"])] == expected


def test_leaderboard_cached_until_new_run_publishes(solo, settings):
    cache.clear()
    first = solo.get("/api/leaderboard")
    assert first.headers["cache-control"] == "public, max-age=300"
    assert solo.get("/api/leaderboard/status").headers["cache-control"] == "public, max-age=300"

    with count_queries() as q:
        again = solo.get("/api/leaderboard?limit=2")
    assert again.json()["entries"] == first.json()["entries"][:2]
    assert _from(q, "standings") == []  # served from memory
    assert len(_from(q, "runs")) == 1  # only the cheap "latest published run id" check

    asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    with count_queries() as q:
        rebuilt = solo.get("/api/leaderboard").json()
    assert rebuilt["run_id"] == first.json()["run_id"] + 1
    assert _from(q, "standings")  # rebuilt from the new run


def test_cache_rebuilds_when_run_id_changes_without_clear(solo, settings):
    solo.get("/api/leaderboard")
    from app import pipeline

    original = cache.clear
    pipeline.cache.clear = lambda: None  # even if the publish hook were missed, the run id check catches it
    try:
        asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    finally:
        pipeline.cache.clear = original
    assert solo.get("/api/leaderboard").json()["run_id"] == 2
    assert solo.get("/api/leaderboard/badges").json()["run_id"] == 2


def test_member_detail_matches_leaderboard_without_loading_all_standings(solo):
    board = solo.get("/api/leaderboard").json()
    for e in board["entries"]:
        with count_queries() as q:
            m = solo.get(f"/api/members/{e['id']}").json()
        assert (m["published_rank"], m["published_score"], m["published_run_id"]) == (
            e["rank"],
            e["score"],
            board["run_id"],
        )
        standings_queries = _from(q, "standings")
        assert len(standings_queries) == 1 and "entity_id" in standings_queries[0]


def test_pool_settings_applied_to_postgres_engine(settings, monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "12")
    get_settings.cache_clear()
    try:
        engine = db.configure("postgresql://user:pw@db.example.invalid:5432/app")  # no connection is made
        assert engine.dialect.driver == "psycopg2"
        assert engine.pool.size() == 4
        assert engine.pool._max_overflow == 1
        assert engine.pool._timeout == 12
    finally:
        get_settings.cache_clear()
        db.configure(settings.database_url)


def test_pool_defaults(settings):
    s = get_settings()
    assert (s.db_pool_size, s.db_max_overflow, s.db_pool_timeout) == (3, 2, 30.0)


def test_startup_retries_then_succeeds(monkeypatch):
    calls, slept = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("EMAXCONNSESSION")

    monkeypatch.setattr(db, "init_db", flaky)
    db.init_db_with_retry(sleep=slept.append)
    assert len(calls) == 3 and slept == [3, 6]


def test_startup_gives_up_after_five_attempts(monkeypatch):
    slept = []
    monkeypatch.setattr(db, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        db.init_db_with_retry(sleep=slept.append)
    assert slept == [3, 6, 12, 24]

