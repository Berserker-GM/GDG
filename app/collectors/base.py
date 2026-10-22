"""Collector interface: every platform collector implements ``fetch(profile) -> ProfileResult``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

# Result statuses (mirrors Member.link_status values)
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class ProfileRef:
    """What a collector needs to know about a member's profile."""

    member_id: int | None
    url: str
    profile_id: str | None = None
    platform: str | None = None


@dataclass
class ParsedBadge:
    badge_id: str
    badge_url: str | None
    image_url: str | None
    title: str
    earned_raw: str | None
    earned_date: date | None
    description: str | None
    source_path: str | None
    category: str
    deprecated: bool
    level: str | None


@dataclass
class ProfileResult:
    status: str
    url: str
    http_status: int | None = None
    error: str | None = None
    attempts: int = 0
    fetched_at: datetime | None = None
    display_name: str | None = None
    member_since: int | None = None
    league: str | None = None
    platform_points: int | None = None
    avatar_url: str | None = None
    badges: list[ParsedBadge] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return convert(asdict(self))


@runtime_checkable
class Collector(Protocol):
    platform: str

    async def fetch(self, profile: ProfileRef) -> ProfileResult: ...

    async def aclose(self) -> None: ...
