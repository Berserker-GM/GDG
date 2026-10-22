"""ORM models. Kept declarative and self-contained so Alembic can be added later."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

# Link statuses set at import time
MISSING_URL = "missing_url"
INVALID_URL = "invalid_url"
UNSUPPORTED_PLATFORM = "unsupported_platform"
PENDING = "pending"
# Link statuses set by the collector
OK = "ok"
NOT_FOUND = "not_found"
FETCH_ERROR = "fetch_error"
PARSE_ERROR = "parse_error"

IMPORT_STATUSES = {MISSING_URL, INVALID_URL, UNSUPPORTED_PLATFORM, PENDING}
UNFETCHABLE_STATUSES = {MISSING_URL, INVALID_URL, UNSUPPORTED_PLATFORM}

PLATFORM_SKILLS = "skills_google"
PLATFORM_DEVELOPERS = "google_developers"

RUN_RUNNING = "running"
RUN_PUBLISHED = "published"
RUN_REJECTED = "rejected"
RUN_FAILED = "failed"


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    members: Mapped[list["Member"]] = relationship(back_populates="team")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usn: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str | None] = mapped_column(String(320))
    college_email: Mapped[str | None] = mapped_column(String(320))
    year: Mapped[str | None] = mapped_column(String(16))
    department: Mapped[str | None] = mapped_column(String(64))
    registration_answer: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    team: Mapped[Team | None] = relationship(back_populates="members")

    # Link
    profile_url_raw: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(String(500))
    profile_id: Mapped[str | None] = mapped_column(String(64), index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    link_status: Mapped[str] = mapped_column(String(32), default=PENDING, nullable=False)

    # Scrape metadata
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Scraped profile fields
    display_name: Mapped[str | None] = mapped_column(String(200))
    member_since: Mapped[int | None] = mapped_column(Integer)
    league: Mapped[str | None] = mapped_column(String(64))
    platform_points: Mapped[int | None] = mapped_column(Integer)
    avatar_url: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    badges: Mapped[list["Badge"]] = relationship(
        back_populates="member", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def name(self) -> str:
        return self.full_name or self.display_name or f"Member {self.id}"


class Badge(Base):
    __tablename__ = "badges"
    __table_args__ = (UniqueConstraint("member_id", "badge_id", name="uq_badge_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    badge_id: Mapped[str] = mapped_column(String(32), nullable=False)
    badge_url: Mapped[str | None] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    earned_raw: Mapped[str | None] = mapped_column(String(100))
    earned_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    level: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    member: Mapped[Member] = relationship(back_populates="badges")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_RUNNING, index=True)
    mode: Mapped[str | None] = mapped_column(String(8))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_ratio: Mapped[float | None] = mapped_column(Float)
    entries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scoring_hash: Mapped[str | None] = mapped_column(String(64))
    scoring_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)

    standings: Mapped[list["Standing"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Standing.rank"
    )


class Standing(Base):
    __tablename__ = "standings"
    __table_args__ = (Index("ix_standings_run_rank", "run_id", "rank"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(8), nullable=False)  # team | member
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    counted_badges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[Run] = relationship(back_populates="standings")
