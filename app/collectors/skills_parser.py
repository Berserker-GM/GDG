"""Parser for server-rendered Google Skills public profiles (www.skills.google/public_profiles/<uuid>).

Badges are located by their link pattern first (``/public_profiles/<uuid>/badges/<id>``) and by CSS
classes second, so a restyle of the page does not silently zero everyone out.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.collectors.base import ParsedBadge, ProfileResult, STATUS_OK

BADGE_HREF_RE = re.compile(r"/public_profiles/[0-9a-fA-F-]{36}/badges/(\d+)")
LOOSE_BADGE_HREF_RE = re.compile(r"/badges/(\d+)")
EARNED_RE = re.compile(r"Earned\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})")
MEMBER_SINCE_RE = re.compile(r"Member since\s+(\d{4})", re.IGNORECASE)
POINTS_RE = re.compile(r"([\d,]+)\s*points?", re.IGNORECASE)
LEVEL_RE = re.compile(r"\b(introductory|intermediate|advanced)\b", re.IGNORECASE)
NO_BADGES_RE = re.compile(r"hasn['’`]?t earned any badges", re.IGNORECASE)
GAME_TITLE_RE = re.compile(r"^Level \d+:|Arcade|Trivia")

GAME_BADGE = "game_badge"
SKILL_BADGE = "skill_badge"
COURSE_BADGE = "course_badge"
OTHER = "other"


class ParseError(Exception):
    pass


def collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_earned_date(raw: str | None) -> date | None:
    """'Earned Oct 31, 2025 EDT' -> date(2025, 10, 31). The calendar date shown on the profile
    (US Eastern, EDT/EST) is used as-is; no timezone conversion is applied."""
    if not raw:
        return None
    match = EARNED_RE.search(raw)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f"{month.title()} {day} {year}", "%b %d %Y").date()
    except ValueError:
        return None


def classify(title: str, description: str | None, source_path: str | None) -> tuple[str, bool, str | None]:
    """Return (category, deprecated, level)."""
    desc = description or ""
    path = source_path or ""
    if path.startswith("/games/"):
        category = GAME_BADGE
    elif path.startswith("/course_templates/"):
        category = SKILL_BADGE if "skill badge" in desc.lower() else COURSE_BADGE
    elif source_path is None and GAME_TITLE_RE.search(title):
        # No dialog to go by: arcade/trivia/"Level N:" titles are game badges.
        category = GAME_BADGE
    else:
        category = OTHER
    deprecated = title.strip().lower().startswith("[deprecated]")
    level_match = LEVEL_RE.search(desc)
    level = level_match.group(1).lower() if level_match else None
    return category, deprecated, level


def _dialogs(soup: BeautifulSoup) -> dict[str, tuple[str | None, str | None]]:
    dialogs: dict[str, tuple[str | None, str | None]] = {}
    for dialog in soup.find_all("ql-dialog"):
        dialog_id = dialog.get("id")
        if not dialog_id:
            continue
        paragraph = dialog.find("p")
        description = collapse(paragraph.get_text(" ")) if paragraph else None
        button = dialog.find("ql-button", href=True)
        if button is None:
            button = dialog.find("a", href=True)
        source_path = button.get("href") if button else None
        dialogs[dialog_id] = (description or None, source_path)
    return dialogs


def _badge_container(anchor: Tag) -> Tag:
    container = anchor.find_parent("div", class_="profile-badge")
    return container if container is not None else (anchor.parent or anchor)


def _title_span(container: Tag, anchor: Tag) -> str:
    span = container.find("span", class_="ql-title-medium")
    if span is None:
        # Fallback: first non-date span after the badge link.
        for sibling in anchor.find_all_next("span", limit=4):
            text = collapse(sibling.get_text(" "))
            if text and not text.lower().startswith("earned"):
                span = sibling
                break
    if span is None:
        button = container.find("ql-button", attrs={"arialabel": True})
        if button is not None:
            return collapse(re.sub(r"^Learn more about\s+", "", button.get("arialabel", "")))
        return ""
    return collapse(span.get_text(" "))


def _earned_span(container: Tag, anchor: Tag) -> str | None:
    for span in container.find_all("span"):
        text = collapse(span.get_text(" "))
        if text.lower().startswith("earned"):
            return text
    for span in anchor.find_all_next("span", limit=4):
        text = collapse(span.get_text(" "))
        if text.lower().startswith("earned"):
            return text
    return None


def parse_profile(html: str, url: str) -> ProfileResult:
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.find("h1")
    if h1 is None:
        raise ParseError("No <h1> found; page is not a public profile")

    result = ProfileResult(status=STATUS_OK, url=url)
    result.display_name = collapse(h1.get_text(" ")) or None

    since = MEMBER_SINCE_RE.search(soup.get_text(" "))
    result.member_since = int(since.group(1)) if since else None

    league_block = soup.find("div", class_="profile-league")
    if league_block is not None:
        heading = league_block.find(["h2", "h3"])
        result.league = collapse(heading.get_text(" ")) if heading else None
        strong = league_block.find("strong")
        points = POINTS_RE.search(strong.get_text(" ") if strong else league_block.get_text(" "))
        result.platform_points = int(points.group(1).replace(",", "")) if points else None

    avatar = soup.find("ql-avatar", src=True) or soup.find("img", class_="profile-avatar")
    result.avatar_url = avatar.get("src") if avatar else None

    dialogs = _dialogs(soup)

    anchors = soup.find_all("a", href=BADGE_HREF_RE)
    if not anchors:
        # Second chance: class-based lookup with a looser link pattern.
        anchors = [
            a
            for block in soup.find_all("div", class_="profile-badge")
            for a in block.find_all("a", href=LOOSE_BADGE_HREF_RE)[:1]
        ]

    seen: set[str] = set()
    for anchor in anchors:
        href = anchor.get("href", "")
        match = BADGE_HREF_RE.search(href) or LOOSE_BADGE_HREF_RE.search(href)
        if not match:
            continue
        badge_id = match.group(1)
        if badge_id in seen:
            continue
        seen.add(badge_id)

        container = _badge_container(anchor)
        title = _title_span(container, anchor)
        earned_raw = _earned_span(container, anchor)
        img = anchor.find("img") or container.find("img")
        button = container.find("ql-button", attrs={"modal": True})
        description, source_path = dialogs.get(button.get("modal"), (None, None)) if button else (None, None)
        category, deprecated, level = classify(title, description, source_path)

        result.badges.append(
            ParsedBadge(
                badge_id=badge_id,
                badge_url=urljoin(url, href),
                image_url=img.get("src") if img else None,
                title=title,
                earned_raw=earned_raw,
                earned_date=parse_earned_date(earned_raw),
                description=description,
                source_path=source_path,
                category=category,
                deprecated=deprecated,
                level=level,
            )
        )

    # Zero badges is only trusted when the page says so. Otherwise the badge markup has probably
    # changed, and reporting zero would silently wipe everyone's score.
    if not result.badges and not NO_BADGES_RE.search(soup.get_text(" ")):
        raise ParseError("No badges found and no \"hasn't earned any badges yet\" text; markup may have changed")
    # Categories come from the dialogs; if not one badge could be matched to its dialog, the dialog
    # markup has changed and most badges would silently fall into "other" (0 points).
    if result.badges and all(b.source_path is None for b in result.badges):
        raise ParseError(
            f"None of {len(result.badges)} badges matched a ql-dialog source path; markup may have changed"
        )
    return result
