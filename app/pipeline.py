"""One leaderboard run: lock -> collect -> score -> validate -> publish."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.collectors import Collector, ProfileRef, ProfileResult, build_collectors
from app.config import Settings, get_settings
from app.db import session_scope, utcnow
from app.leaderboard import compute_entries, has_supported_link
from app.models import (
    OK,
    RUN_FAILED,
    RUN_PUBLISHED,
    RUN_REJECTED,
    RUN_RUNNING,
    Badge,
    Member,
    Run,
    Standing,
)
from app.scoring import ScoringConfig, rank_entries

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


class RunInProgress(Exception):
    pass


def is_running_in_process() -> bool:
    return _lock.locked()


async def begin_run(trigger: str, settings: Settings | None = None) -> int:
    """Acquire the in-process and DB locks and create the run row. Raises RunInProgress."""
    settings = settings or get_settings()
    if _lock.locked():
        raise RunInProgress("A run is already in progress in this process")
    await _lock.acquire()
    try:
        with session_scope() as session:
            cutoff = utcnow() - timedelta(hours=settings.run_lock_timeout_hours)
            for run in session.scalars(select(Run).where(Run.status == RUN_RUNNING)):
                if run.started_at >= cutoff:
                    raise RunInProgress(f"Run {run.id} has been running since {run.started_at:%Y-%m-%d %H:%M} UTC")
                run.status = RUN_FAILED
                run.finished_at = utcnow()
                run.notes = (run.notes or "") + "Abandoned: exceeded run lock timeout."
            run = Run(trigger=trigger, status=RUN_RUNNING, started_at=utcnow())
            session.add(run)
            session.flush()
            return run.id
    except BaseException:
        _lock.release()
        raise


def _apply_badges(session, member: Member, result: ProfileResult) -> None:
    now = utcnow()
    existing = {b.badge_id: b for b in member.badges}
    incoming_ids = set()
    for parsed in result.badges:
        incoming_ids.add(parsed.badge_id)
        badge = existing.get(parsed.badge_id)
        if badge is None:
            badge = Badge(member_id=member.id, badge_id=parsed.badge_id, first_seen_at=now)
            session.add(badge)
            member.badges.append(badge)
        badge.badge_url = parsed.badge_url
        badge.image_url = parsed.image_url
        badge.title = parsed.title
        badge.earned_raw = parsed.earned_raw
        badge.earned_date = parsed.earned_date
        badge.description = parsed.description
        badge.source_path = parsed.source_path
        badge.category = parsed.category
        badge.deprecated = parsed.deprecated
        badge.level = parsed.level
        badge.last_seen_at = now
    for badge_id, badge in existing.items():
        if badge_id not in incoming_ids:
            member.badges.remove(badge)  # delete-orphan


def apply_result(member_id: int, result: ProfileResult) -> None:
    """Persist one fetch result. A failure never touches stored badges; it only marks them stale."""
    with session_scope() as session:
        member = session.scalars(
            select(Member).where(Member.id == member_id).options(selectinload(Member.badges))
        ).one()
        member.last_attempt_at = result.fetched_at or utcnow()
        if result.ok:
            member.link_status = OK
            member.last_success_at = member.last_attempt_at
            member.last_error = None
            member.is_stale = False
            member.display_name = result.display_name
            member.member_since = result.member_since
            member.league = result.league
            member.platform_points = result.platform_points
            member.avatar_url = result.avatar_url
            _apply_badges(session, member, result)
        else:
            member.link_status = result.status
            member.last_error = (result.error or result.status)[:2000]
            member.is_stale = True


def eligible_profiles(collectors: dict[str, Collector]) -> list[ProfileRef]:
    with session_scope() as session:
        members = session.scalars(select(Member).where(Member.active.is_(True)).order_by(Member.id))
        return [
            ProfileRef(member_id=m.id, url=m.profile_url, profile_id=m.profile_id, platform=m.platform)
            for m in members
            if has_supported_link(m) and m.platform in collectors and m.profile_url
        ]


async def execute_run(
    run_id: int,
    collectors: dict[str, Collector] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the pipeline for a run created by ``begin_run``; always releases the lock."""
    settings = settings or get_settings()
    owned = collectors is None
    try:
        if collectors is None:
            collectors = build_collectors(settings)
        cfg = ScoringConfig.load(settings.scoring_config)
        with session_scope() as session:
            run = session.get(Run, run_id)
            run.scoring_hash = cfg.hash()
            run.scoring_snapshot = cfg.snapshot()

        refs = eligible_profiles(collectors)
        succeeded = 0
        badges_seen = 0
        other_badges = 0
        failures: dict[str, int] = {}

        async def collect(ref: ProfileRef) -> None:
            nonlocal succeeded, badges_seen, other_badges
            result = await collectors[ref.platform].fetch(ref)
            apply_result(ref.member_id, result)
            if result.ok:
                succeeded += 1
                badges_seen += len(result.badges)
                other_badges += sum(1 for b in result.badges if b.category == "other")
            else:
                failures[result.status] = failures.get(result.status, 0) + 1

        await asyncio.gather(*(collect(ref) for ref in refs))
        attempted = len(refs)
        ratio = succeeded / attempted if attempted else 0.0
        other_ratio = other_badges / badges_seen if badges_seen else 0.0

        with session_scope() as session:
            mode, entries = compute_entries(session, cfg, settings.leaderboard_mode)
            ranked = rank_entries(entries)[: settings.leaderboard_size]
            run = session.get(Run, run_id)
            run.mode = mode
            run.attempted = attempted
            run.succeeded = succeeded
            run.failed = attempted - succeeded
            run.success_ratio = round(ratio, 4)
            run.entries = len(ranked)
            for rank, entry in ranked:
                session.add(
                    Standing(
                        run_id=run_id,
                        rank=rank,
                        entity_type=entry.entity_type,
                        entity_id=entry.entity_id,
                        name=entry.name,
                        score=entry.score,
                        counted_badges=entry.counted_badges,
                        breakdown=entry.breakdown,
                    )
                )
            notes = []
            if failures:
                notes.append("Failures: " + ", ".join(f"{k}={v}" for k, v in sorted(failures.items())))
            rejections = []
            if not attempted:
                rejections.append("No eligible profiles to fetch.")
            elif ratio < settings.min_success_ratio:
                rejections.append(
                    f"Success ratio {ratio:.2f} below MIN_SUCCESS_RATIO {settings.min_success_ratio:.2f}."
                )
            if other_ratio > settings.max_other_badge_ratio:
                rejections.append(
                    f"{other_badges} of {badges_seen} badges ({other_ratio:.0%}) were classified 'other', above "
                    f"MAX_OTHER_BADGE_RATIO {settings.max_other_badge_ratio:.0%}; badge classification markup "
                    "may have changed."
                )
            if rejections:
                run.status = RUN_REJECTED
                notes.extend(rejections)
                notes.append("Previous published leaderboard kept.")
            else:
                run.status = RUN_PUBLISHED
                run.published_at = utcnow()
            run.notes = " ".join(notes) or None
            run.finished_at = utcnow()
            summary = {
                "run_id": run_id,
                "status": run.status,
                "mode": mode,
                "attempted": attempted,
                "succeeded": succeeded,
                "failed": attempted - succeeded,
                "success_ratio": run.success_ratio,
                "badges_seen": badges_seen,
                "other_badges": other_badges,
                "entries": len(ranked),
                "scoring_hash": run.scoring_hash,
                "notes": run.notes,
            }
        log.info(
            "Run %(run_id)s %(status)s: mode=%(mode)s attempted=%(attempted)s succeeded=%(succeeded)s "
            "failed=%(failed)s ratio=%(success_ratio)s entries=%(entries)s",
            summary,
        )
        return summary
    except Exception as exc:
        log.exception("Run %s failed", run_id)
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = RUN_FAILED
                run.finished_at = utcnow()
                run.notes = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        if owned and collectors:
            for collector in collectors.values():
                await collector.aclose()
        _lock.release()


async def run_pipeline(
    trigger: str = "manual",
    collectors: dict[str, Collector] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    run_id = await begin_run(trigger, settings)
    return await execute_run(run_id, collectors, settings)
