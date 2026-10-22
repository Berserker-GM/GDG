"""Profile URL validation and normalisation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import (
    INVALID_URL,
    MISSING_URL,
    PENDING,
    PLATFORM_DEVELOPERS,
    PLATFORM_SKILLS,
    UNSUPPORTED_PLATFORM,
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SKILLS_RE = re.compile(
    rf"^https?://(?:www\.)?(?:skills\.google|cloudskillsboost\.google)/public_profiles/({_UUID})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_DEVELOPERS_RE = re.compile(
    r"^https?://(?:me\.developers\.google\.com|g\.dev|developers\.google\.com/profile)(?:/|$)",
    re.IGNORECASE,
)

CANONICAL_PROFILE = "https://www.skills.google/public_profiles/{}"


@dataclass(frozen=True)
class LinkCheck:
    status: str
    url: str | None = None  # normalised URL (or the cleaned raw URL for unsupported platforms)
    profile_id: str | None = None
    platform: str | None = None


def check_profile_url(raw: object) -> LinkCheck:
    value = "" if raw is None else str(raw).strip()
    if not value:
        return LinkCheck(MISSING_URL)
    value = value.split()[0]  # stray text after the URL
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value.lstrip("/")

    match = _SKILLS_RE.match(value)
    if match:
        profile_id = match.group(1).lower()
        return LinkCheck(PENDING, CANONICAL_PROFILE.format(profile_id), profile_id, PLATFORM_SKILLS)
    if _DEVELOPERS_RE.match(value):
        # Kept so a g.dev collector can be added later.
        return LinkCheck(UNSUPPORTED_PLATFORM, value, None, PLATFORM_DEVELOPERS)
    return LinkCheck(INVALID_URL, value)
