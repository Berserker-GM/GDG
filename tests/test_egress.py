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



# ------------------------------------------------------------------ status endpoint egress


def _status_the_old_way(settings_mode="auto"):
    """What /api/leaderboard/status computed before: load every member with badges, count in Python."""
    from collections import Counter

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.leaderboard import resolve_mode
    from app.models import Member

    with db.session_scope() as s:
        members = list(
            s.scalars(select(Member).where(Member.active.is_(True)).options(selectinload(Member.badges)).order_by(Member.id))
        )
        mode = resolve_mode(settings_mode, members)
        out = {
            "mode": mode,
            "active_members": len(members),
            "stale_members": sum(1 for m in members if m.is_stale),
            "link_health": dict(sorted(Counter(m.link_status for m in members).items())),
        }
        if mode == "team":
            out["unassigned_members"] = [{"member_id": m.id, "name": m.name} for m in members if m.team_id is None]
        return out


@contextmanager
def forbid_table(table):
    def before(conn, cursor, statement, params, context, executemany):
        assert not re.search(rf"\b{table}\b", statement), f"{table} queried: {statement}"

    engine = db.get_engine()
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _check_status_fields(body, expected):
    for key, value in expected.items():
        assert body[key] == value, key
    assert set(body) >= {"running", "last_run", "last_published_run", "next_scheduled_run", "scheduler_enabled"}


def test_status_same_values_without_loading_badges(solo):
    from app.models import Member

    with db.session_scope() as s:  # make the counts non-trivial: one stale member
        s.get(Member, 1).is_stale = True
    cache.clear()
    expected = _status_the_old_way()
    with forbid_table("badges"), count_queries() as q:
        body = solo.get("/api/leaderboard/status").json()
    _check_status_fields(body, expected)
    assert body["stale_members"] == 1 and body["mode"] == "solo" and "unassigned_members" not in body
    assert body["last_run"]["status"] == "published" and body["last_published_run"]["run_id"] == 1
    assert not any("scoring_snapshot" in s for s in q)  # run rows are read without the JSON blob
    assert not any(re.search(r"SELECT members\.id AS members_id, members\.usn", s) for s in q)  # no full member rows


def test_status_team_mode_unassigned_without_loading_badges(settings, tmp_path):
    rows = [
        form_row("T1", "Alpha One", profile_url(0), team="Red"),
        form_row("T2", "Beta Two", profile_url(1), team="Red"),
        form_row("T3", "Gamma Three", profile_url(2)),
    ]
    with db.session_scope() as s:
        import_form(s, write_form(tmp_path / "form.xlsx", rows, with_team_column=True))
    asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    cache.clear()
    expected = _status_the_old_way()
    assert expected["mode"] == "team" and len(expected["unassigned_members"]) == 1
    with TestClient(create_app()) as c, forbid_table("badges"):
        body = c.get("/api/leaderboard/status").json()
    _check_status_fields(body, expected)
    assert body["unassigned_members"][0]["name"] == "Gamma Three"


def test_status_cached_for_60s_and_cleared_on_publish(solo, settings, monkeypatch):
    cache.clear()
    first = solo.get("/api/leaderboard/status").json()
    with count_queries() as q:
        again = solo.get("/api/leaderboard/status").json()
    assert again == first and q == []  # served from memory, no DB at all

    # Expires after the TTL.
    import time as _time

    real = _time.monotonic
    monkeypatch.setattr(cache.time, "monotonic", lambda: real() + 61)
    with count_queries() as q:
        solo.get("/api/leaderboard/status")
    assert _from(q, "members")
    monkeypatch.setattr(cache.time, "monotonic", real)

    # A publish clears it: the new run shows up immediately.
    solo.get("/api/leaderboard/status")
    asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    body = solo.get("/api/leaderboard/status").json()
    assert body["last_published_run"]["run_id"] == first["last_published_run"]["run_id"] + 1


def test_status_cleared_when_run_starts(solo, settings):
    from app.pipeline import _lock, begin_run

    cache.clear()
    assert solo.get("/api/leaderboard/status").json()["running"] is False
    run_id = asyncio.run(begin_run("test"))
    try:
        body = solo.get("/api/leaderboard/status").json()
        assert body["running"] is True and body["last_run"]["run_id"] == run_id
    finally:
        from app.models import RUN_FAILED, Run

        with db.session_scope() as s:
            s.get(Run, run_id).status = RUN_FAILED
        _lock.release()


def test_badges_endpoint_never_loads_description(solo):
    cache.clear()
    with count_queries() as q:
        body = solo.get("/api/leaderboard/badges").json()
    assert body["members"]
    assert not any("description" in s for s in _from(q, "badges"))


def test_refresh_pipeline_loads_only_scoring_columns(settings, solo):
    with count_queries() as q:
        asyncio.run(run_pipeline("test", collectors_for(_pages(3), settings)()))
    from app.leaderboard import SCORING_BADGE_COLUMNS  # noqa: F401  (documented column list)

    # The pipeline still writes descriptions (apply_result), but the scoring read must not select it.
    scoring_reads = [s for s in _from(q, "badges") if "badges.member_id IN" in s and "image_url" not in s]
    assert scoring_reads, "scoring should load badges with load_only"
    assert not any("description" in s for s in scoring_reads)
