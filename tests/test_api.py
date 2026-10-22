import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.api import create_app
from app.importer import import_form
from app.models import Member, Run, Team
from app.pipeline import run_pipeline
from tests.helpers import COURSE, GAME, SKILL, UUIDS, badge_page, collectors_for, form_row, profile_url, write_form

ADMIN = {"X-Admin-Token": "secret-token"}


def _pages(n):
    return {
        UUIDS[i]: badge_page(
            f"Display {i}",
            [(f"{i}1", "Skill", "Oct 1, 2026", *SKILL), (f"{i}2", "Course", "Oct 2, 2026", *COURSE)][: 1 + i % 2]
            + [(f"{i}3", "[Deprecated] Old", "Oct 3, 2026", *GAME)],
        )
        for i in range(n)
    }


@pytest.fixture
def seeded(settings, tmp_path):
    rows = [
        form_row(f"1BY23CS0{i:02d}", f"Student {i}", profile_url(i), team="Alpha" if i < 2 else "Beta")
        for i in range(4)
    ]
    form = write_form(tmp_path / "form.xlsx", rows, with_team_column=True)
    with db.session_scope() as s:
        import_form(s, form)
    pages = _pages(4)
    import asyncio

    asyncio.run(run_pipeline("test", collectors_for(pages, settings)()))
    return {"rows": rows, "pages": pages, "form": form}


@pytest.fixture
def client(settings, seeded):
    with TestClient(create_app(collectors_for(seeded["pages"], settings))) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_leaderboard_shape(client):
    body = client.get("/api/leaderboard?limit=50").json()
    assert body["mode"] == "team"
    assert body["run_id"] and body["published_at"]
    entries = body["entries"]
    assert [e["rank"] for e in entries] == [1, 2]
    top = entries[0]
    assert set(top) >= {"rank", "name", "score", "counted_badges", "breakdown", "members", "entity_type", "id"}
    assert set(top["breakdown"]["counts_by_category"]) == {"skill_badge", "course_badge", "game_badge", "other"}
    assert all(set(m) == {"member_id", "name", "score", "stale"} for m in top["members"])
    assert client.get("/api/leaderboard?limit=1").json()["entries"].__len__() == 1


def test_team_and_member_detail(client, settings):
    with db.session_scope() as s:
        team_id = s.scalars(select(Team).where(Team.name == "Alpha")).one().id
        member_id = s.scalars(select(Member).where(Member.usn == "1BY23CS001")).one().id
    team = client.get(f"/api/teams/{team_id}").json()
    assert team["name"] == "Alpha" and len(team["members"]) == 2
    member = client.get(f"/api/members/{member_id}").json()
    assert member["display_name"] == "Display 1"
    assert member["league"] == "Bronze League" and member["platform_points"] == 100
    assert member["link_status"] == "ok" and member["last_success_at"]
    assert {b["counted"] for b in member["badges"]} == {True}
    assert client.get("/api/members/99999").status_code == 404
    assert client.get("/api/teams/99999").status_code == 404


def test_live_and_published_scores(client, settings):
    from pathlib import Path

    with db.session_scope() as s:
        team_id = s.scalars(select(Team).where(Team.name == "Alpha")).one().id
        member_id = s.scalars(select(Member).where(Member.usn == "1BY23CS001")).one().id
    run_id = client.get("/api/leaderboard").json()["run_id"]

    # Member 1 has a skill (10), a course (3) and a deprecated game badge (5)
    member = client.get(f"/api/members/{member_id}").json()
    assert "score" not in member
    assert member["live_score"] == member["published_score"] == 18
    assert member["published_run_id"] == run_id
    assert member["published_rank"] is None  # team mode: members are not ranked individually

    team = client.get(f"/api/teams/{team_id}").json()
    standing = next(e for e in client.get("/api/leaderboard").json()["entries"] if e["id"] == team_id)
    assert team["live_score"] == team["published_score"] == standing["score"]
    assert team["published_rank"] == standing["rank"] and team["published_run_id"] == run_id
    assert all(m["live_score"] == m["published_score"] for m in team["members"])

    # Scoring change: live moves, published stays until the next published run.
    Path(settings.scoring_config).write_text("points: {skill_badge: 100}\n", encoding="utf-8")
    member = client.get(f"/api/members/{member_id}").json()
    assert member["live_score"] == 108 and member["published_score"] == 18
    team = client.get(f"/api/teams/{team_id}").json()
    assert team["live_score"] > team["published_score"]


async def test_published_score_null_outside_top_n(settings, tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("LEADERBOARD_SIZE", "2")
    get_settings.cache_clear()
    rows = [form_row(f"U{i}", f"S{i}", profile_url(i)) for i in range(3)]
    with db.session_scope() as s:
        import_form(s, write_form(tmp_path / "f.xlsx", rows))
    pages = {UUIDS[i]: badge_page(f"S{i}", [(f"{j}{i}", "Skill", "Oct 1, 2026", *SKILL) for j in range(3 - i)])
             for i in range(3)}

    with TestClient(create_app()) as client:
        with db.session_scope() as s:
            ids = {m.usn: m.id for m in s.scalars(select(Member))}
        # Nothing published yet
        before = client.get(f"/api/members/{ids['U0']}").json()
        assert before["published_score"] is None and before["published_run_id"] is None

        await run_pipeline("test", collectors_for(pages, get_settings())())
        top = client.get(f"/api/members/{ids['U0']}").json()
        assert (top["published_rank"], top["published_score"], top["live_score"]) == (1, 30, 30)
        outside = client.get(f"/api/members/{ids['U2']}").json()
        assert outside["published_score"] is None and outside["published_rank"] is None
        assert outside["live_score"] == 10


def test_uncounted_reasons(client, settings, tmp_path):
    from pathlib import Path

    Path(settings.scoring_config).write_text(
        "count_deprecated: false\nprogram_start: '2026-10-02'\n", encoding="utf-8"
    )
    with db.session_scope() as s:
        member_id = s.scalars(select(Member).where(Member.usn == "1BY23CS001")).one().id
    body = client.get(f"/api/members/{member_id}").json()
    reasons = {b["title"]: b["reason"] for b in body["uncounted_badges"]}
    assert reasons == {"Skill": "before_start", "[Deprecated] Old": "deprecated"}
    assert [b["title"] for b in body["counted_badges"]] == ["Course"]


def test_status_history_and_run(client):
    status = client.get("/api/leaderboard/status").json()
    assert status["last_published_run"]["status"] == "published"
    assert status["link_health"] == {"ok": 4}
    assert status["mode"] == "team" and status["unassigned_members"] == []
    history = client.get("/api/leaderboard/history").json()["runs"]
    run_id = history[0]["run_id"]
    run = client.get(f"/api/leaderboard/runs/{run_id}").json()
    assert run["run_id"] == run_id and run["entries"] and run["scoring"]["points"]["skill_badge"] == 10
    assert client.get("/api/leaderboard/runs/999").status_code == 404


def test_no_pii_in_public_responses(client, seeded):
    with db.session_scope() as s:
        members = s.scalars(select(Member)).all()
        member_ids = [m.id for m in members]
        team_ids = [t.id for t in s.scalars(select(Team))]
        secrets = [m.usn for m in members] + [m.email for m in members] + [m.college_email for m in members]
    urls = ["/health", "/api/leaderboard", "/api/leaderboard/history", "/api/leaderboard/status"]
    urls += [f"/api/members/{i}" for i in member_ids] + [f"/api/teams/{i}" for i in team_ids]
    run_id = client.get("/api/leaderboard").json()["run_id"]
    urls.append(f"/api/leaderboard/runs/{run_id}")
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        text = response.text.lower()
        assert "@" not in text, url
        for secret in secrets:
            assert secret.lower() not in text, (url, secret)
        assert "usn" not in text and "email" not in text, url


@pytest.mark.parametrize("path,method", [
    ("/api/admin/refresh", "post"), ("/api/admin/link-report", "get"), ("/api/admin/import", "post"),
])
def test_admin_auth(client, path, method):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Admin-Token": "wrong"}).status_code == 403


def test_admin_disabled_without_token(settings, seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert c.get("/api/admin/link-report", headers=ADMIN).status_code == 503


def test_link_report_csv(client):
    response = client.get("/api/admin/link-report", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("USN,name,email,profile_url,link_status,last_error")
    assert len(lines) == 5 and "1BY23CS000" in response.text


def test_admin_import(client, seeded, tmp_path):
    teams = tmp_path / "teams.csv"
    teams.write_text("USN,Team\n1BY23CS003,Gamma\n", encoding="utf-8")
    with open(seeded["form"], "rb") as form, open(teams, "rb") as tf:
        response = client.post(
            "/api/admin/import", headers=ADMIN,
            files={"form": ("form.xlsx", form), "teams": ("teams.csv", tf)},
        )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["unique_students"] == 4 and report["team_source"] == "file"


def test_admin_refresh_runs_and_conflicts(client):
    with db.session_scope() as s:
        s.add(Run(trigger="elsewhere", status="running", started_at=db.utcnow()))
    assert client.post("/api/admin/refresh", headers=ADMIN).status_code == 409
    with db.session_scope() as s:
        for run in s.scalars(select(Run).where(Run.trigger == "elsewhere")):
            run.status = "failed"

    response = client.post("/api/admin/refresh", headers=ADMIN)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/leaderboard/runs/{run_id}").json()["status"]
        if status != "running":
            break
        time.sleep(0.05)
    assert status == "published"
    assert client.get("/api/leaderboard").json()["run_id"] == run_id
