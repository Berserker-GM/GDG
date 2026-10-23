"""FastAPI application. Public endpoints never expose emails or USNs."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import shutil
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import db
from app.collectors import Collector
from app.config import get_settings
from app.importer import FormImportError, import_form
from app.leaderboard import active_members, badge_detail, member_score, resolve_mode, sorted_badges
from app.models import RUN_PUBLISHED, RUN_RUNNING, Member, Run, Standing, Team
from app.pipeline import RunInProgress, begin_run, execute_run, is_running_in_process
from app.scoring import ScoringConfig, team_entry

log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.init_db()
    scheduler = None
    if settings.enable_scheduler:
        from app.scheduler import create_scheduler

        scheduler = create_scheduler(settings)
        scheduler.start()
        log.info("Scheduler started: cron=%r tz=%s", settings.refresh_cron, settings.timezone)
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def get_session() -> Iterator[Session]:
    session = db.new_session()
    try:
        yield session
    finally:
        session.close()


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(503, "Admin endpoints are disabled: ADMIN_TOKEN is not configured")
    if not x_admin_token:
        raise HTTPException(401, "Missing X-Admin-Token header")
    if not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(403, "Invalid admin token")


def _scoring() -> ScoringConfig:
    return ScoringConfig.load(get_settings().scoring_config)


def _run_summary(run: Run | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "mode": run.mode,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "published_at": iso(run.published_at),
        "attempted": run.attempted,
        "succeeded": run.succeeded,
        "failed": run.failed,
        "success_ratio": run.success_ratio,
        "entries": run.entries,
        "scoring_hash": run.scoring_hash,
        "notes": run.notes,
    }


def _standing_out(s: Standing) -> dict[str, Any]:
    b = s.breakdown or {}
    entry: dict[str, Any] = {
        "rank": s.rank,
        "entity_type": s.entity_type,
        "id": s.entity_id,
        "name": s.name,
        "score": s.score,
        "counted_badges": s.counted_badges,
        "breakdown": {
            "counts_by_category": b.get("counts_by_category", {}),
            "points_by_category": b.get("points_by_category", {}),
            "skill_badges": b.get("skill_badges", 0),
            "reached_final_score_at": b.get("reached_final_score_at"),
        },
    }
    if s.entity_type == "team":
        entry["breakdown"]["aggregation"] = b.get("aggregation")
        entry["breakdown"]["stale_members"] = b.get("stale_members", 0)
        entry["members"] = [
            {"member_id": m["member_id"], "name": m["name"], "score": m["score"], "stale": m["stale"]}
            for m in b.get("members", [])
        ]
    else:
        entry["department"] = b.get("department")
        entry["year"] = b.get("year")
        entry["stale"] = b.get("stale", False)
    return entry


def _run_with_standings(run: Run, limit: int) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "status": run.status,
        "published_at": iso(run.published_at),
        "mode": run.mode,
        "scoring_hash": run.scoring_hash,
        "entries": [_standing_out(s) for s in run.standings[:limit]],
    }


@dataclass
class PublishedScores:
    run_id: int | None
    teams: dict[int, tuple[int | None, float]] = field(default_factory=dict)  # id -> (rank, score)
    members: dict[int, tuple[int | None, float]] = field(default_factory=dict)


def _latest_published(session: Session) -> Run | None:
    return session.scalars(
        select(Run)
        .where(Run.status == RUN_PUBLISHED)
        .order_by(Run.published_at.desc(), Run.id.desc())
        .options(selectinload(Run.standings))
        .limit(1)
    ).first()


def create_app(collector_factory: Callable[[], dict[str, Collector]] | None = None) -> FastAPI:
    """``collector_factory`` lets tests inject collectors with a mocked HTTP transport."""
    settings = get_settings()
    app = FastAPI(title="Skill Lab Leaderboard", version="1.0.0", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health():
        ok = db.ping()
        body = {"status": "ok" if ok else "degraded", "database": "ok" if ok else "unreachable"}
        return JSONResponse(body, status_code=200 if ok else 503)

    # --------------------------------------------------------------- frontend
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/", response_class=HTMLResponse)
        def index():
            html_file = frontend_dir / "index.html"
            if html_file.exists():
                return html_file.read_text(encoding="utf-8")
            return HTMLResponse("<h1>Frontend index.html not found</h1>", status_code=404)

    # ---------------------------------------------------------------- public
    @app.get("/api/leaderboard")
    def leaderboard(limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)):
        run = _latest_published(session)
        if run is None:
            return {"run_id": None, "published_at": None, "mode": None, "entries": []}
        return _run_with_standings(run, limit)

    @app.get("/api/leaderboard/history")
    def history(limit: int = Query(30, ge=1, le=365), session: Session = Depends(get_session)):
        runs = session.scalars(select(Run).order_by(Run.id.desc()).limit(limit))
        return {"runs": [_run_summary(r) for r in runs]}

    @app.get("/api/leaderboard/status")
    def status(session: Session = Depends(get_session)):
        last_run = session.scalars(select(Run).order_by(Run.id.desc()).limit(1)).first()
        last_published = session.scalars(
            select(Run).where(Run.status == RUN_PUBLISHED).order_by(Run.id.desc()).limit(1)
        ).first()
        members = active_members(session)
        link_health = Counter(m.link_status for m in members)
        mode = resolve_mode(get_settings().leaderboard_mode, members)
        next_run = None
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            job = scheduler.get_job("daily-refresh")
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time.isoformat()
        body: dict[str, Any] = {
            "mode": mode,
            "running": is_running_in_process()
            or session.scalar(select(func.count()).select_from(Run).where(Run.status == RUN_RUNNING)) > 0,
            "last_run": _run_summary(last_run),
            "last_published_run": _run_summary(last_published),
            "next_scheduled_run": next_run,
            "scheduler_enabled": scheduler is not None,
            "active_members": len(members),
            "stale_members": sum(1 for m in members if m.is_stale),
            "link_health": dict(sorted(link_health.items())),
        }
        if mode == "team":
            body["unassigned_members"] = [
                {"member_id": m.id, "name": m.name} for m in members if m.team_id is None
            ]
        return body

    @app.get("/api/leaderboard/runs/{run_id}")
    def run_detail(run_id: int, limit: int = Query(500, ge=1, le=5000), session: Session = Depends(get_session)):
        run = session.scalars(
            select(Run).where(Run.id == run_id).options(selectinload(Run.standings))
        ).first()
        if run is None:
            raise HTTPException(404, "Run not found")
        return {**_run_with_standings(run, limit), "run": _run_summary(run), "scoring": run.scoring_snapshot}

    def _published_scores(session: Session) -> PublishedScores:
        """Scores from the latest published run. Only the top LEADERBOARD_SIZE entries are stored,
        so anyone outside it has no published score (null)."""
        run = _latest_published(session)
        scores = PublishedScores(run_id=run.id if run else None)
        for s in run.standings if run else []:
            if s.entity_type == "team":
                scores.teams[s.entity_id] = (s.rank, s.score)
                for m in (s.breakdown or {}).get("members", []):
                    scores.members[m["member_id"]] = (None, m["score"])
            else:
                scores.members[s.entity_id] = (s.rank, s.score)
        return scores

    def _member_out(m: Member, cfg: ScoringConfig, published: PublishedScores) -> dict[str, Any]:
        badges = [badge_detail(b, cfg) for b in sorted_badges(m)]
        rank, score = published.members.get(m.id, (None, None))
        return {
            "member_id": m.id,
            "name": m.name,
            "department": m.department,
            "year": m.year,
            "display_name": m.display_name,
            "league": m.league,
            "platform_points": m.platform_points,
            "member_since": m.member_since,
            "link_status": m.link_status,
            "stale": m.is_stale,
            "last_success_at": iso(m.last_success_at),
            "live_score": member_score(m, cfg).score,
            "published_score": score,
            "published_rank": rank,
            "counted_badges": [b for b in badges if b["counted"]],
            "uncounted_badges": [b for b in badges if not b["counted"]],
        }

    @app.get("/api/teams/{team_id}")
    def team_detail(team_id: int, session: Session = Depends(get_session)):
        team = session.scalars(
            select(Team)
            .where(Team.id == team_id)
            .options(selectinload(Team.members).selectinload(Member.badges))
        ).first()
        if team is None:
            raise HTTPException(404, "Team not found")
        cfg = _scoring()
        published = _published_scores(session)
        members = [m for m in team.members if m.active]
        rank, score = published.teams.get(team.id, (None, None))
        return {
            "team_id": team.id,
            "name": team.name,
            "live_score": team_entry(team.id, team.name, [member_score(m, cfg) for m in members], cfg).score,
            "published_score": score,
            "published_rank": rank,
            "published_run_id": published.run_id,
            "members": [
                _member_out(m, cfg, published) for m in sorted(members, key=lambda m: m.name.lower())
            ],
        }

    @app.get("/api/members/{member_id}")
    def member_detail(member_id: int, session: Session = Depends(get_session)):
        member = session.scalars(
            select(Member).where(Member.id == member_id).options(selectinload(Member.badges), selectinload(Member.team))
        ).first()
        if member is None or not member.active:
            raise HTTPException(404, "Member not found")
        published = _published_scores(session)
        out = _member_out(member, _scoring(), published)
        out["published_run_id"] = published.run_id
        out["team"] = {"team_id": member.team.id, "name": member.team.name} if member.team else None
        out["badges"] = sorted(
            out["counted_badges"] + out["uncounted_badges"],
            key=lambda b: (b["earned_date"] is None, b["earned_date"] or ""),
        )
        return out

    # ----------------------------------------------------------------- admin
    @app.post("/api/admin/refresh", status_code=202, dependencies=[Depends(require_admin)])
    async def admin_refresh():
        try:
            run_id = await begin_run("api")
        except RunInProgress as exc:
            raise HTTPException(409, str(exc)) from exc

        async def _go() -> None:
            collectors = collector_factory() if collector_factory else None
            try:
                await execute_run(run_id, collectors)
            except Exception:
                log.exception("Background run %s failed", run_id)
            finally:
                if collector_factory and collectors:
                    for collector in collectors.values():
                        await collector.aclose()

        task = asyncio.create_task(_go())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"run_id": run_id, "status": RUN_RUNNING}

    @app.post("/api/admin/import", dependencies=[Depends(require_admin)])
    def admin_import(form: UploadFile = File(...), teams: UploadFile | None = File(None)):
        tmpdir = Path(tempfile.mkdtemp(prefix="leaderboard-import-"))
        try:
            form_path = tmpdir / "form.xlsx"
            with form_path.open("wb") as fh:
                shutil.copyfileobj(form.file, fh)
            teams_path = None
            if teams is not None and teams.filename:
                suffix = Path(teams.filename).suffix.lower() or ".csv"
                teams_path = tmpdir / f"teams{suffix}"
                with teams_path.open("wb") as fh:
                    shutil.copyfileobj(teams.file, fh)
            try:
                with db.session_scope() as session:
                    report = import_form(session, form_path, teams_path)
            except FormImportError as exc:
                raise HTTPException(400, str(exc)) from exc
            return report.as_dict()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @app.get("/api/admin/link-report", dependencies=[Depends(require_admin)])
    def admin_link_report(session: Session = Depends(get_session)):
        return Response(
            content=link_report_csv(session),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="link-report.csv"'},
        )

    return app


def link_report_csv(session: Session) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["USN", "name", "email", "profile_url", "link_status", "last_error", "active", "flags"])
    for m in session.scalars(select(Member).order_by(Member.active.desc(), Member.usn)):
        writer.writerow(
            [
                m.usn,
                m.full_name,
                m.email or "",
                m.profile_url_raw or "",
                m.link_status,
                m.last_error or "",
                "yes" if m.active else "no",
                ";".join(m.flags or []),
            ]
        )
    return buf.getvalue()


app = create_app()
