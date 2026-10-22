"""Build leaderboard entries from the database (shared by the pipeline and the API)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import NOT_FOUND, PLATFORM_SKILLS, UNFETCHABLE_STATUSES, Badge, Member, Team
from app.scoring import (
    Entry,
    MemberScore,
    ScoringConfig,
    evaluate_badge,
    member_entry,
    score_member,
    team_entry,
)

SUPPORTED_PLATFORMS = {PLATFORM_SKILLS}


def has_supported_link(member: Member) -> bool:
    return member.platform in SUPPORTED_PLATFORMS and member.link_status not in UNFETCHABLE_STATUSES


def has_working_link(member: Member) -> bool:
    return has_supported_link(member) and member.link_status != NOT_FOUND


def active_members(session: Session) -> list[Member]:
    stmt = (
        select(Member)
        .where(Member.active.is_(True))
        .options(selectinload(Member.badges), selectinload(Member.team))
        .order_by(Member.id)
    )
    return list(session.scalars(stmt))


def resolve_mode(setting: str, members: list[Member]) -> str:
    if setting in {"team", "solo"}:
        return setting
    return "team" if any(m.team_id is not None for m in members) else "solo"


def member_score(member: Member, cfg: ScoringConfig) -> MemberScore:
    return score_member(
        member.id,
        member.name,
        member.badges,
        cfg,
        stale=member.is_stale,
        working_link=has_working_link(member),
    )


def compute_entries(session: Session, cfg: ScoringConfig, mode_setting: str) -> tuple[str, list[Entry]]:
    members = active_members(session)
    mode = resolve_mode(mode_setting, members)
    if mode == "solo":
        entries = [
            member_entry(
                member_score(m, cfg),
                {"department": m.department, "year": m.year, "link_status": m.link_status},
            )
            for m in members
            if has_supported_link(m)
        ]
        return mode, entries

    by_team: dict[int, list[Member]] = defaultdict(list)
    for m in members:
        if m.team_id is not None:
            by_team[m.team_id].append(m)
    teams = {t.id: t for t in session.scalars(select(Team).where(Team.id.in_(by_team.keys())))}
    entries = []
    for team_id, team_members in by_team.items():
        if len(team_members) < cfg.min_team_members:
            continue
        scores = [member_score(m, cfg) for m in team_members]
        entries.append(team_entry(team_id, teams[team_id].name, scores, cfg))
    return mode, entries


def badge_detail(badge: Badge, cfg: ScoringConfig) -> dict[str, Any]:
    verdict = evaluate_badge(badge, cfg)
    return {
        "badge_id": badge.badge_id,
        "title": badge.title,
        "category": badge.category,
        "level": badge.level,
        "deprecated": badge.deprecated,
        "earned_date": badge.earned_date.isoformat() if badge.earned_date else None,
        "earned_raw": badge.earned_raw,
        "badge_url": badge.badge_url,
        "image_url": badge.image_url,
        "first_seen_at": badge.first_seen_at.isoformat() if badge.first_seen_at else None,
        "counted": verdict.counted,
        "points": verdict.points,
        "reason": verdict.reason,
    }


def sorted_badges(member: Member) -> list[Badge]:
    return sorted(member.badges, key=lambda b: (b.earned_date is None, b.earned_date), reverse=False)
