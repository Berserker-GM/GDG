from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import db
from app.collectors import ProfileRef, SkillsGoogleCollector
from app.importer import import_form
from app.models import Badge, Member, Run, Standing
from app.pipeline import RunInProgress, begin_run, execute_run, run_pipeline
from tests.helpers import COURSE, GAME, SKILL, UUIDS, badge_page, collectors_for, form_row, profile_url, write_form


def seed(tmp_path, n=3, teams=None):
    rows = [form_row(f"U{i}", f"Student {i}", profile_url(i), team=(teams or {}).get(i)) for i in range(n)]
    path = write_form(tmp_path / "form.xlsx", rows, with_team_column=teams is not None)
    with db.session_scope() as s:
        import_form(s, path)


def page(name, *badges):
    return badge_page(name, list(badges))


async def test_success_publishes_and_syncs_badges(settings, tmp_path):
    seed(tmp_path, n=2)
    pages = {
        UUIDS[0]: page("S0", ("1", "Skill A", "Oct 1, 2026", *SKILL), ("2", "Game", "Oct 2, 2026", *GAME)),
        UUIDS[1]: page("S1", ("3", "Course", "Oct 3, 2026", *COURSE)),
    }
    summary = await run_pipeline("test", collectors_for(pages, settings)())
    assert summary["status"] == "published"
    assert summary["mode"] == "solo"
    assert summary["attempted"] == 2 and summary["succeeded"] == 2

    with db.session_scope() as s:
        standings = s.scalars(select(Standing).order_by(Standing.rank)).all()
        assert [(st.name, st.score) for st in standings] == [("Student 0", 15), ("Student 1", 3)]
        first_seen = s.scalars(select(Badge).where(Badge.badge_id == "1")).one().first_seen_at

    # Second run: badge 2 removed, badge 4 added, badge 1 keeps first_seen_at
    pages[UUIDS[0]] = page("S0", ("1", "Skill A renamed", "Oct 1, 2026", *SKILL), ("4", "New", "Oct 5, 2026", *SKILL))
    await run_pipeline("test", collectors_for(pages, settings)())
    with db.session_scope() as s:
        member = s.scalars(select(Member).where(Member.usn == "U0")).one()
        ids = sorted(b.badge_id for b in member.badges)
        assert ids == ["1", "4"]
        b1 = next(b for b in member.badges if b.badge_id == "1")
        assert b1.title == "Skill A renamed"
        assert b1.first_seen_at == first_seen


async def test_failed_fetch_keeps_old_badges_and_marks_stale(settings, tmp_path):
    seed(tmp_path, n=3)
    ok_pages = {UUIDS[i]: page(f"S{i}", (f"{i}", "Skill", "Oct 1, 2026", *SKILL)) for i in range(3)}
    await run_pipeline("test", collectors_for(ok_pages, settings)())

    failing = dict(ok_pages)
    failing[UUIDS[0]] = 503
    summary = await run_pipeline("test", collectors_for(failing, settings)())
    assert summary["status"] == "published"  # 2/3 >= 0.6
    with db.session_scope() as s:
        m = s.scalars(select(Member).where(Member.usn == "U0")).one()
        assert m.is_stale is True
        assert m.link_status == "fetch_error"
        assert m.last_error == "HTTP 503"
        assert m.last_success_at is not None and m.last_attempt_at >= m.last_success_at
        assert [b.badge_id for b in m.badges] == ["0"]
        top = s.scalars(select(Standing).where(Standing.run_id == summary["run_id"], Standing.entity_id == m.id)).one()
        assert top.score == 10 and top.breakdown["stale"] is True


async def test_not_found_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    collector = SkillsGoogleCollector(settings, transport=httpx.MockTransport(handler))
    result = await collector.fetch(ProfileRef(None, profile_url(0)))
    await collector.aclose()
    assert result.status == "not_found" and len(calls) == 1


async def test_retries_on_429_then_succeeds(settings):
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(500),
                 httpx.Response(200, text=page("X"))]

    def handler(request):
        return responses.pop(0)

    collector = SkillsGoogleCollector(settings, transport=httpx.MockTransport(handler))
    result = await collector.fetch(ProfileRef(None, profile_url(0)))
    await collector.aclose()
    assert result.ok and result.attempts == 3 and result.badges == []


async def test_retries_exhausted_on_timeouts(settings):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("timed out")

    collector = SkillsGoogleCollector(settings, transport=httpx.MockTransport(handler))
    result = await collector.fetch(ProfileRef(None, profile_url(0)))
    await collector.aclose()
    assert result.status == "fetch_error" and len(calls) == 4  # 1 + 3 retries


async def test_parse_error_keeps_badges(settings, tmp_path):
    seed(tmp_path, n=1)
    await run_pipeline("test", collectors_for({UUIDS[0]: page("S", ("9", "Skill", "Oct 1, 2026", *SKILL))}, settings)())
    summary = await run_pipeline("test", collectors_for({UUIDS[0]: "<html><body>Sign in</body></html>"}, settings)())
    assert summary["status"] == "rejected"
    with db.session_scope() as s:
        m = s.scalars(select(Member)).one()
        assert m.link_status == "parse_error" and m.is_stale and len(m.badges) == 1


async def test_unrecognised_empty_page_keeps_badges_and_rejects(settings, tmp_path):
    seed(tmp_path, n=2)
    good = {UUIDS[i]: page(f"S{i}", (f"{i}", "Skill", "Oct 1, 2026", *SKILL)) for i in range(2)}
    first = await run_pipeline("test", collectors_for(good, settings)())

    # Simulated redesign: name still renders, badges vanish, no "hasn't earned" text.
    redesigned = "<html><body><h1>Student</h1><div class='new-badge-grid'></div></body></html>"
    second = await run_pipeline("test", collectors_for({u: redesigned for u in good}, settings)())
    assert second["status"] == "rejected" and second["succeeded"] == 0
    with db.session_scope() as s:
        for m in s.scalars(select(Member)):
            assert m.link_status == "parse_error" and m.is_stale and len(m.badges) == 1
            assert "markup may have changed" in m.last_error
    assert first["status"] == "published"


OTHER = ("/focuses/123", "A lab.")


async def test_mostly_other_badges_rejects_run(settings, tmp_path):
    seed(tmp_path, n=2)
    good = {UUIDS[i]: page(f"S{i}", (f"{i}", "Skill", "Oct 1, 2026", *SKILL)) for i in range(2)}
    first = await run_pipeline("test", collectors_for(good, settings)())

    # 3 of 5 badges (60%) are "other" even though every fetch succeeds.
    mostly_other = {
        UUIDS[0]: page("S0", ("1", "Skill", "Oct 1, 2026", *SKILL), ("901", "Lab A", "Oct 2, 2026", *OTHER),
                       ("902", "Lab B", "Oct 2, 2026", *OTHER)),
        UUIDS[1]: page("S1", ("2", "Game", "Oct 1, 2026", *GAME), ("903", "Lab C", "Oct 2, 2026", *OTHER)),
    }
    summary = await run_pipeline("test", collectors_for(mostly_other, settings)())
    assert summary["success_ratio"] == 1.0
    assert summary["badges_seen"] == 5 and summary["other_badges"] == 3
    assert summary["status"] == "rejected"
    assert "3 of 5 badges (60%) were classified 'other'" in summary["notes"]
    assert "MAX_OTHER_BADGE_RATIO" in summary["notes"]

    from fastapi.testclient import TestClient

    from app.api import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/leaderboard").json()["run_id"] == first["run_id"]


async def test_half_other_badges_still_publishes(settings, tmp_path):
    seed(tmp_path, n=1)
    pages = {UUIDS[0]: page("S0", ("1", "Skill", "Oct 1, 2026", *SKILL), ("904", "Lab", "Oct 2, 2026", *OTHER))}
    summary = await run_pipeline("test", collectors_for(pages, settings)())
    assert summary["other_badges"] == 1 and summary["badges_seen"] == 2
    assert summary["status"] == "published"  # exactly 50% is not "more than 50%"


async def test_low_success_ratio_rejected_previous_still_served(settings, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import create_app

    seed(tmp_path, n=3)
    good = {UUIDS[i]: page(f"S{i}", (f"{i}", "Skill", "Oct 1, 2026", *SKILL)) for i in range(3)}
    first = await run_pipeline("test", collectors_for(good, settings)())
    assert first["status"] == "published"

    bad = {UUIDS[0]: good[UUIDS[0]], UUIDS[1]: 500, UUIDS[2]: 500}
    second = await run_pipeline("test", collectors_for(bad, settings)())
    assert second["status"] == "rejected"
    assert second["success_ratio"] == pytest.approx(1 / 3, abs=1e-3)

    with TestClient(create_app()) as client:
        body = client.get("/api/leaderboard").json()
        assert body["run_id"] == first["run_id"]
        history = client.get("/api/leaderboard/history").json()["runs"]
        assert [r["status"] for r in history[:2]] == ["rejected", "published"]


async def test_in_process_lock_prevents_concurrent_runs(settings, tmp_path):
    seed(tmp_path, n=1)
    run_id = await begin_run("first")
    with pytest.raises(RunInProgress):
        await begin_run("second")
    await execute_run(run_id, collectors_for({UUIDS[0]: page("S")}, settings)())
    # Lock released afterwards
    again = await run_pipeline("third", collectors_for({UUIDS[0]: page("S")}, settings)())
    assert again["status"] == "published"


async def test_db_lock_blocks_recent_and_clears_abandoned(settings, tmp_path):
    seed(tmp_path, n=1)
    with db.session_scope() as s:
        s.add(Run(trigger="other-process", status="running", started_at=db.utcnow() - timedelta(minutes=10)))
    with pytest.raises(RunInProgress):
        await begin_run("blocked")

    with db.session_scope() as s:
        run = s.scalars(select(Run)).one()
        run.started_at = db.utcnow() - timedelta(hours=3)
    summary = await run_pipeline("after", collectors_for({UUIDS[0]: page("S")}, settings)())
    assert summary["status"] == "published"
    with db.session_scope() as s:
        old = s.scalars(select(Run).where(Run.trigger == "other-process")).one()
        assert old.status == "failed" and "Abandoned" in old.notes


async def test_team_mode_and_scoring_snapshot(settings, tmp_path):
    seed(tmp_path, n=3, teams={0: "Alpha", 1: "Alpha", 2: "Beta"})
    pages = {
        UUIDS[0]: page("S0", ("1", "Skill", "Oct 1, 2026", *SKILL)),
        UUIDS[1]: page("S1", ("2", "Course", "Oct 1, 2026", *COURSE)),
        UUIDS[2]: page("S2", ("3", "Skill", "Oct 1, 2026", *SKILL), ("4", "Game", "Oct 2, 2026", *GAME)),
    }
    summary = await run_pipeline("test", collectors_for(pages, settings)())
    assert summary["mode"] == "team"
    with db.session_scope() as s:
        standings = s.scalars(select(Standing).order_by(Standing.rank)).all()
        assert [(st.name, st.score) for st in standings] == [("Beta", 15), ("Alpha", 13)]
        assert len(standings[1].breakdown["members"]) == 2
        run = s.get(Run, summary["run_id"])
        assert len(run.scoring_hash) == 64
        assert run.scoring_snapshot["points"]["skill_badge"] == 10


async def test_unsupported_links_are_not_fetched(settings, tmp_path):
    rows = [form_row("A", "A", profile_url(0)), form_row("B", "B", "https://me.developers.google.com/u/1")]
    with db.session_scope() as s:
        import_form(s, write_form(tmp_path / "f.xlsx", rows))
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=page("A"))

    from app.collectors import build_collectors

    summary = await run_pipeline("test", build_collectors(settings, transport=httpx.MockTransport(handler)))
    assert summary["attempted"] == 1
    assert requested == [profile_url(0)]
