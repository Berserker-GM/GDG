"""Scoring rules (from scoring.yaml) and ranking.

Syllabus: when ``syllabus`` is set, only badges for those courses count and each earns the points
listed for it (0 is allowed: the badge still counts as completed but adds no points). A badge is
matched by the ``/course_templates/<id>`` it links to, or by title when the profile gives no link.

Dates: a badge's ``earned_date`` is the calendar date printed on the profile, which Google shows in
US Eastern time (EDT/EST). It is compared as-is with ``program_start`` / ``program_end`` (both
inclusive); no timezone conversion is done, so what students see on their profile is what counts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Protocol

import yaml

CATEGORIES = ("skill_badge", "course_badge", "game_badge", "other")

REASON_BEFORE_START = "before_start"
REASON_AFTER_END = "after_end"
REASON_UNKNOWN_DATE = "unknown_date"
REASON_DEPRECATED = "deprecated"
REASON_ZERO_POINTS = "zero_points"
REASON_DUPLICATE = "duplicate"
REASON_NOT_IN_SYLLABUS = "not_in_syllabus"

TEMPLATE_RE = re.compile(r"/course_templates/(\d+)")


def template_id(path_or_url: str | None) -> str | None:
    match = TEMPLATE_RE.search(path_or_url or "")
    return match.group(1) if match else None


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", title or "").strip().lower()


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class SyllabusCourse:
    template_id: str
    title: str
    points: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyllabusCourse":
        tid = template_id(str(data.get("url") or "")) or str(data.get("template_id") or "").strip()
        if not tid:
            raise ValueError(f"syllabus course needs a url or template_id: {data!r}")
        points = int(data.get("points") or 0)
        if points < 0:
            raise ValueError("syllabus points must be >= 0")
        return cls(template_id=tid, title=str(data.get("title") or "").strip(), points=points)


@dataclass(frozen=True)
class ScoringConfig:
    program_start: date | None = None
    program_end: date | None = None
    target_badges: int | None = None
    points: dict[str, int] = field(
        default_factory=lambda: {"skill_badge": 10, "course_badge": 3, "game_badge": 5, "other": 0}
    )
    count_deprecated: bool = True
    badge_overrides: dict[str, int] = field(default_factory=dict)
    max_points_per_member: int | None = None
    team_aggregation: str = "sum"
    min_team_members: int = 1
    syllabus: tuple[SyllabusCourse, ...] = ()

    @property
    def completion_target(self) -> int | None:
        """Badge count that means "completed everything": target_badges, else the syllabus size."""
        if self.target_badges is not None:
            return self.target_badges
        return len(self.syllabus) or None

    def syllabus_match(self, badge: "BadgeLike") -> SyllabusCourse | None:
        tid = template_id(getattr(badge, "source_path", None))
        title = normalize_title(badge.title)
        for course in self.syllabus:
            if tid is not None:
                if course.template_id == tid:
                    return course
            elif course.title and normalize_title(course.title) == title:
                return course
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScoringConfig":
        data = data or {}
        defaults = cls()
        points = {**defaults.points, **{k: int(v) for k, v in (data.get("points") or {}).items()}}
        aggregation = str(data.get("team_aggregation") or "sum").lower()
        if aggregation not in {"sum", "average"}:
            raise ValueError("team_aggregation must be 'sum' or 'average'")
        cap = data.get("max_points_per_member")
        target = data.get("target_badges")
        return cls(
            program_start=_as_date(data.get("program_start")),
            program_end=_as_date(data.get("program_end")),
            target_badges=int(target) if target not in (None, "") else None,
            points=points,
            count_deprecated=bool(data.get("count_deprecated", True)),
            badge_overrides={str(k): int(v) for k, v in (data.get("badge_overrides") or {}).items()},
            max_points_per_member=int(cap) if cap not in (None, "") else None,
            team_aggregation=aggregation,
            min_team_members=int(data.get("min_team_members") or 1),
            syllabus=tuple(SyllabusCourse.from_dict(c) for c in (data.get("syllabus") or [])),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ScoringConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def snapshot(self) -> dict[str, Any]:
        return {
            "program_start": self.program_start.isoformat() if self.program_start else None,
            "program_end": self.program_end.isoformat() if self.program_end else None,
            "target_badges": self.target_badges,
            "points": dict(sorted(self.points.items())),
            "count_deprecated": self.count_deprecated,
            "badge_overrides": dict(sorted(self.badge_overrides.items())),
            "max_points_per_member": self.max_points_per_member,
            "team_aggregation": self.team_aggregation,
            "min_team_members": self.min_team_members,
            "syllabus": [
                {"template_id": c.template_id, "title": c.title, "points": c.points} for c in self.syllabus
            ],
        }

    def hash(self) -> str:
        payload = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BadgeLike(Protocol):
    badge_id: str
    title: str
    category: str
    deprecated: bool
    earned_date: date | None
    # Optional: source_path ("/course_templates/<id>") is used for syllabus matching when present.


@dataclass
class BadgeVerdict:
    counted: bool
    points: int
    reason: str | None


def evaluate_badge(badge: BadgeLike, cfg: ScoringConfig) -> BadgeVerdict:
    if cfg.program_start or cfg.program_end:
        if badge.earned_date is None:
            return BadgeVerdict(False, 0, REASON_UNKNOWN_DATE)
        if cfg.program_start and badge.earned_date < cfg.program_start:
            return BadgeVerdict(False, 0, REASON_BEFORE_START)
        if cfg.program_end and badge.earned_date > cfg.program_end:
            return BadgeVerdict(False, 0, REASON_AFTER_END)
    if badge.deprecated and not cfg.count_deprecated:
        return BadgeVerdict(False, 0, REASON_DEPRECATED)
    if cfg.syllabus:
        course = cfg.syllabus_match(badge)
        if course is None:
            return BadgeVerdict(False, 0, REASON_NOT_IN_SYLLABUS)
        return BadgeVerdict(True, course.points, None)
    if badge.title in cfg.badge_overrides:
        points = cfg.badge_overrides[badge.title]
    else:
        points = cfg.points.get(badge.category, cfg.points.get("other", 0))
    if points <= 0:
        return BadgeVerdict(False, 0, REASON_ZERO_POINTS)
    return BadgeVerdict(True, points, None)


@dataclass
class MemberScore:
    member_id: int
    name: str
    score: int
    counted_badges: int
    skill_badges: int
    reached_at: date | None
    counts_by_category: dict[str, int]
    points_by_category: dict[str, int]
    stale: bool = False
    working_link: bool = True
    capped: bool = False


def score_member(
    member_id: int,
    name: str,
    badges: Iterable[BadgeLike],
    cfg: ScoringConfig,
    stale: bool = False,
    working_link: bool = True,
) -> MemberScore:
    counts = {c: 0 for c in CATEGORIES}
    points = {c: 0 for c in CATEGORIES}
    counted: list[tuple[date | None, int]] = []
    seen: set[str] = set()
    seen_courses: set[str] = set()
    skill = 0
    for badge in badges:
        if badge.badge_id in seen:
            continue
        seen.add(badge.badge_id)
        verdict = evaluate_badge(badge, cfg)
        if not verdict.counted:
            continue
        if cfg.syllabus:
            # The same syllabus course earned twice (two badge ids) counts once.
            course = cfg.syllabus_match(badge)
            if course.template_id in seen_courses:
                continue
            seen_courses.add(course.template_id)
        category = badge.category if badge.category in counts else "other"
        counts[category] += 1
        points[category] += verdict.points
        if category == "skill_badge":
            skill += 1
        counted.append((badge.earned_date, verdict.points))

    total = sum(p for _, p in counted)
    capped = cfg.max_points_per_member is not None and total > cfg.max_points_per_member
    final = min(total, cfg.max_points_per_member) if capped else total

    # "Reached final score" = date the running total first hit the final score. Uncapped, that is the
    # last counted badge (a 0-point syllabus badge still has to be earned to complete the set).
    reached_at: date | None = None
    if counted:
        running = 0
        ordered = sorted(counted, key=lambda x: (x[0] is None, x[0] or date.max))
        for i, (earned, pts) in enumerate(ordered):
            running += pts
            if running >= final and (capped or i == len(ordered) - 1):
                reached_at = earned
                break

    return MemberScore(
        member_id=member_id,
        name=name,
        score=final,
        counted_badges=len(counted),
        skill_badges=skill,
        reached_at=reached_at,
        counts_by_category=counts,
        points_by_category=points,
        stale=stale,
        working_link=working_link,
        capped=capped,
    )


@dataclass
class Entry:
    entity_type: str  # "team" | "member"
    entity_id: int
    name: str
    score: float
    counted_badges: int
    skill_badges: int
    reached_at: date | None
    breakdown: dict[str, Any]


def member_entry(ms: MemberScore, extra: dict[str, Any] | None = None) -> Entry:
    return Entry(
        entity_type="member",
        entity_id=ms.member_id,
        name=ms.name,
        score=ms.score,
        counted_badges=ms.counted_badges,
        skill_badges=ms.skill_badges,
        reached_at=ms.reached_at,
        breakdown={
            "counts_by_category": ms.counts_by_category,
            "points_by_category": ms.points_by_category,
            "skill_badges": ms.skill_badges,
            "reached_final_score_at": ms.reached_at.isoformat() if ms.reached_at else None,
            "capped": ms.capped,
            "stale": ms.stale,
            **(extra or {}),
        },
    )


def team_entry(team_id: int, team_name: str, members: list[MemberScore], cfg: ScoringConfig) -> Entry:
    working = [m for m in members if m.working_link]
    if cfg.team_aggregation == "average":
        score = round(sum(m.score for m in working) / len(working), 2) if working else 0.0
    else:
        score = float(sum(m.score for m in members))
    counts = {c: sum(m.counts_by_category[c] for m in members) for c in CATEGORIES}
    points = {c: sum(m.points_by_category[c] for m in members) for c in CATEGORIES}
    reached = [m.reached_at for m in members if m.score > 0 and m.reached_at is not None]
    reached_at = max(reached) if reached else None
    skill = sum(m.skill_badges for m in members)
    return Entry(
        entity_type="team",
        entity_id=team_id,
        name=team_name,
        score=score,
        counted_badges=sum(m.counted_badges for m in members),
        skill_badges=skill,
        reached_at=reached_at,
        breakdown={
            "aggregation": cfg.team_aggregation,
            "counts_by_category": counts,
            "points_by_category": points,
            "skill_badges": skill,
            "reached_final_score_at": reached_at.isoformat() if reached_at else None,
            "member_count": len(members),
            "working_link_members": len(working),
            "stale_members": sum(1 for m in members if m.stale),
            "members": [
                {
                    "member_id": m.member_id,
                    "name": m.name,
                    "score": m.score,
                    "counted_badges": m.counted_badges,
                    "stale": m.stale,
                    "working_link": m.working_link,
                }
                for m in sorted(members, key=lambda m: (-m.score, m.name.lower()))
            ],
        },
    )


def rank_key(entry: Entry) -> tuple:
    """Tie-breakers: score desc, skill badges desc, earlier reached-final-score, name."""
    return (-entry.score, -entry.skill_badges, entry.reached_at or date.max, entry.name.lower(), entry.entity_id)


def rank_entries(entries: Iterable[Entry], cfg: ScoringConfig | None = None) -> list[tuple[int, Entry]]:
    """Rank entries according to the campaign rules:
    - Participants who completed all badges (either reaching cfg.target_badges, or the highest
      score group if cfg.target_badges is not set):
      - The first participant to complete all badges (earliest reached_at) is placed on top.
      - After that, all other participants who completed are ranked in alphabetical order by name.
    - All remaining participants are ranked below by score desc, skill badges desc, and name alphabetically.
    """
    entries_list = list(entries)
    if not entries_list:
        return []

    target = cfg.completion_target if cfg else None
    if target is not None:
        completed = [e for e in entries_list if e.counted_badges >= target and e.score > 0]
    else:
        max_score = max((e.score for e in entries_list), default=0)
        completed = [e for e in entries_list if e.score == max_score and max_score > 0]

    if completed:
        first_winner = min(
            completed,
            key=lambda e: (e.reached_at or date.max, -e.skill_badges, e.name.lower(), e.entity_id),
        )
        other_completed = sorted(
            [e for e in completed if (e.entity_id != first_winner.entity_id or e.name != first_winner.name)],
            key=lambda e: (e.name.lower(), e.entity_id),
        )
        completed_ranked = [first_winner] + other_completed
        completed_keys = {(e.entity_type, e.entity_id) for e in completed}
        remaining = [e for e in entries_list if (e.entity_type, e.entity_id) not in completed_keys]
    else:
        completed_ranked = []
        remaining = entries_list

    remaining_ranked = sorted(
        remaining,
        key=lambda e: (-e.score, -e.skill_badges, e.name.lower(), e.entity_id),
    )

    final_list = completed_ranked + remaining_ranked
    return [(i, e) for i, e in enumerate(final_list, start=1)]
