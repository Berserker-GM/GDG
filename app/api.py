"""FastAPI application. Public endpoints never expose emails or USNs."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only, selectinload

from app import cache, db
from app.collectors import Collector
from app.config import get_settings
from app.importer import FormImportError, import_form
from app.leaderboard import badge_detail, member_score, resolve_mode_db, sorted_badges
from app.models import RUN_PUBLISHED, RUN_RUNNING, Badge, Member, Run, Standing, Team
from app.pipeline import RunInProgress, begin_run, execute_run, is_running_in_process
from app.scoring import ScoringConfig, team_entry

log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await asyncio.to_thread(db.init_db_with_retry)
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


# Columns _run_summary() reads; scoring_snapshot (a large JSON blob) is left out.
RUN_SUMMARY_COLUMNS = (
    Run.id,
    Run.trigger,
    Run.status,
    Run.mode,
    Run.started_at,
    Run.finished_at,
    Run.published_at,
    Run.attempted,
    Run.succeeded,
    Run.failed,
    Run.success_ratio,
    Run.entries,
    Run.scoring_hash,
    Run.notes,
)


def _status_body(session: Session, scheduler: Any) -> dict[str, Any]:
    """Status from aggregate queries: no member rows, no badges, no scoring snapshots."""
    summary = load_only(*RUN_SUMMARY_COLUMNS)
    last_run = session.scalars(select(Run).options(summary).order_by(Run.id.desc()).limit(1)).first()
    last_published = session.scalars(
        select(Run).options(summary).where(Run.status == RUN_PUBLISHED).order_by(Run.id.desc()).limit(1)
    ).first()
    active = Member.active.is_(True)
    active_count = session.scalar(select(func.count()).select_from(Member).where(active)) or 0
    stale_count = (
        session.scalar(select(func.count()).select_from(Member).where(active, Member.is_stale.is_(True))) or 0
    )
    link_health = {
        status: count
        for status, count in session.execute(
            select(Member.link_status, func.count()).where(active).group_by(Member.link_status)
        )
    }
    mode = resolve_mode_db(get_settings().leaderboard_mode, session)
    next_run = None
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
        "active_members": active_count,
        "stale_members": stale_count,
        "link_health": dict(sorted(link_health.items())),
    }
    if mode == "team":
        rows = session.execute(
            select(Member.id, Member.full_name, Member.display_name)
            .where(active, Member.team_id.is_(None))
            .order_by(Member.id)
        )
        body["unassigned_members"] = [
            {"member_id": mid, "name": full or display or f"Member {mid}"} for mid, full, display in rows
        ]
    return body


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


PUBLIC_CACHE_CONTROL = "public, max-age=300"


def _latest_published(session: Session, with_standings: bool = False) -> Run | None:
    query = select(Run).where(Run.status == RUN_PUBLISHED).order_by(Run.published_at.desc(), Run.id.desc()).limit(1)
    if with_standings:
        query = query.options(selectinload(Run.standings))
    return session.scalars(query).first()


def _latest_published_id(session: Session) -> int | None:
    """Cheap: reads one id, no standings."""
    return session.scalar(
        select(Run.id).where(Run.status == RUN_PUBLISHED).order_by(Run.published_at.desc(), Run.id.desc()).limit(1)
    )


def _published_member_ids(standings: list[Standing]) -> list[int]:
    ids: list[int] = []
    for s in standings:
        if s.entity_type == "member":
            ids.append(s.entity_id)
        else:
            ids.extend(m["member_id"] for m in (s.breakdown or {}).get("members", []))
    return ids


def _leaderboard_badges(session: Session, run_id: int, cfg: ScoringConfig) -> dict[str, Any]:
    """Badges of every member in a published run: one query for the standings, one for all their badges."""
    standings = session.scalars(select(Standing).where(Standing.run_id == run_id)).all()
    ids = _published_member_ids(standings)
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    if ids:
        for b in session.scalars(select(Badge).where(Badge.member_id.in_(ids))):
            d = badge_detail(b, cfg)
            out[b.member_id].append(
                {"title": d["title"], "image_url": d["image_url"], "earned_date": d["earned_date"], "counted": d["counted"]}
            )
    for badges in out.values():
        # Same order as /api/members/{id}: counted first, then by earned date.
        badges.sort(key=lambda b: (not b["counted"], b["earned_date"] is None, b["earned_date"] or ""))
    return {"run_id": run_id, "members": {str(k): v for k, v in out.items()}}


def _team_standing(session: Session, run_id: int | None, team_id: int) -> Standing | None:
    if run_id is None:
        return None
    return session.scalars(
        select(Standing).where(Standing.run_id == run_id, Standing.entity_type == "team", Standing.entity_id == team_id)
    ).first()


def _member_standing(session: Session, run_id: int | None, member: Member) -> tuple[int | None, float | None]:
    """(rank, score) for one member in a published run, without loading the other standings."""
    if run_id is None:
        return None, None
    row = session.scalars(
        select(Standing).where(
            Standing.run_id == run_id, Standing.entity_type == "member", Standing.entity_id == member.id
        )
    ).first()
    if row is not None:
        return row.rank, row.score
    if member.team_id is not None:
        team_row = _team_standing(session, run_id, member.team_id)
        if team_row is not None:
            for m in (team_row.breakdown or {}).get("members", []):
                if m["member_id"] == member.id:
                    return None, m["score"]
    return None, None


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
    def leaderboard(
        response: Response, limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)
    ):
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
        run_id = _latest_published_id(session)
        if run_id is None:
            return {"run_id": None, "published_at": None, "mode": None, "total_participants": 0, "entries": []}

        def build() -> dict[str, Any]:
            run = session.scalars(select(Run).where(Run.id == run_id).options(selectinload(Run.standings))).one()
            res = _run_with_standings(run, 500)
            res["total_participants"] = (
                session.scalar(select(func.count()).select_from(Member).where(Member.active.is_(True))) or 0
            )
            return res

        full = cache.get_or_build("leaderboard", run_id, build)
        return {**full, "entries": full["entries"][:limit]}

    @app.get("/api/leaderboard/badges")
    def leaderboard_badges(response: Response, session: Session = Depends(get_session)):
        """Badges for everyone on the published board in one response, instead of one request per member."""
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
        run_id = _latest_published_id(session)
        if run_id is None:
            return {"run_id": None, "members": {}}
        return cache.get_or_build(
            "leaderboard_badges", run_id, lambda: _leaderboard_badges(session, run_id, _scoring())
        )

    @app.get("/api/leaderboard/history")
    def history(limit: int = Query(30, ge=1, le=365), session: Session = Depends(get_session)):
        runs = session.scalars(
            select(Run).options(load_only(*RUN_SUMMARY_COLUMNS)).order_by(Run.id.desc()).limit(limit)
        )
        return {"runs": [_run_summary(r) for r in runs]}

    @app.get("/api/leaderboard/status")
    def status(response: Response, session: Session = Depends(get_session)):
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
        # Every page view hits this; serve from memory and rebuild at most once a minute.
        scheduler = getattr(app.state, "scheduler", None)
        return cache.get_status(lambda: _status_body(session, scheduler))

    @app.get("/api/leaderboard/runs/{run_id}")
    def run_detail(run_id: int, limit: int = Query(500, ge=1, le=5000), session: Session = Depends(get_session)):
        run = session.scalars(
            select(Run).where(Run.id == run_id).options(selectinload(Run.standings))
        ).first()
        if run is None:
            raise HTTPException(404, "Run not found")
        return {**_run_with_standings(run, limit), "run": _run_summary(run), "scoring": run.scoring_snapshot}

    def _member_out(m: Member, cfg: ScoringConfig, published: tuple[int | None, float | None]) -> dict[str, Any]:
        badges = [badge_detail(b, cfg) for b in sorted_badges(m)]
        rank, score = published
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
        run_id = _latest_published_id(session)
        team_row = _team_standing(session, run_id, team.id)
        rank, score = (team_row.rank, team_row.score) if team_row is not None else (None, None)
        member_scores = {
            m["member_id"]: m["score"] for m in ((team_row.breakdown or {}) if team_row else {}).get("members", [])
        }
        members = [m for m in team.members if m.active]
        return {
            "team_id": team.id,
            "name": team.name,
            "live_score": team_entry(team.id, team.name, [member_score(m, cfg) for m in members], cfg).score,
            "published_score": score,
            "published_rank": rank,
            "published_run_id": run_id,
            "members": [
                _member_out(m, cfg, (None, member_scores.get(m.id))) for m in sorted(members, key=lambda m: m.name.lower())
            ],
        }

    @app.get("/api/members/{member_id}")
    def member_detail(member_id: int, session: Session = Depends(get_session)):
        member = session.scalars(
            select(Member).where(Member.id == member_id).options(selectinload(Member.badges), selectinload(Member.team))
        ).first()
        if member is None or not member.active:
            raise HTTPException(404, "Member not found")
        run_id = _latest_published_id(session)
        out = _member_out(member, _scoring(), _member_standing(session, run_id, member))
        out["published_run_id"] = run_id
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
                cache.clear()  # total_participants may have changed
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
