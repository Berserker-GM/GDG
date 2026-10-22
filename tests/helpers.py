"""Shared test helpers: synthetic form workbooks and a mocked Google Skills site."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from openpyxl import Workbook

FIXTURES = Path(__file__).parent / "fixtures"

# Headers as they appear in the real export, including stray spaces/newlines.
FORM_HEADERS = [
    "Timestamp",
    "Email address",
    "Full Name ",
    "College Email Address\n",
    "USN  ",
    "Year of Study",
    "Department ",
    "Upload Your BMSIT College ID Card",
    "Have you completed the official Google registration form? \n",
    "Google Cloud Profile Link ",
    "Upload Screenshot of Official Form Submission",
    "Column 11",
    "Column 12",
]

UUIDS = [f"{i:08x}-1111-4222-8333-{i:012x}" for i in range(1, 200)]


def profile_url(index: int, host: str = "https://www.skills.google") -> str:
    return f"{host}/public_profiles/{UUIDS[index]}"


def form_row(
    usn: str,
    name: str,
    url: str | None,
    *,
    ts: datetime | None = None,
    email: str | None = None,
    college_email: str | None = None,
    year: float | str = 2.0,
    dept: str = "CSE",
    registered: str = "Yes",
    team: str | None = None,
) -> list:
    row = [
        ts or datetime(2026, 9, 1, 10, 0, 0),
        email if email is not None else f"{usn.lower().strip()}@gmail.com",
        name,
        college_email if college_email is not None else f"{usn.lower().strip()}@bmsit.in",
        usn,
        year,
        dept,
        "https://drive.google.com/open?id=idcard",
        registered,
        url,
        "https://drive.google.com/open?id=shot",
        None,
        None,
    ]
    if team is not None:
        row.append(team)
    return row


def write_form(path: Path, rows: list[list], with_team_column: bool = False) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Form responses 1"
    headers = FORM_HEADERS + (["Team "] if with_team_column else [])
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def badge_page(name: str, badges: list[tuple[str, str, str, str, str]]) -> str:
    """Render a profile page. badges: (badge_id, title, earned 'Mon D, YYYY', source_path, description)."""
    cards, dialogs = [], []
    for i, (badge_id, title, earned, source, desc) in enumerate(badges):
        cards.append(
            f"""<div class='profile-badge'>
<a class="badge-image" href="https://www.skills.google/public_profiles/{UUIDS[0]}/badges/{badge_id}"><img src="https://cdn.example/{badge_id}.png" /></a>
<span class='ql-title-medium l-mts'>
{title}
</span>
<span class='ql-body-medium l-mbs'>
Earned {earned} EDT
</span>
<ql-button label='Learn more' modal='public-profile-award-modal-{i}' outlined></ql-button>
</div>"""
        )
        dialogs.append(
            f"""<ql-dialog headline='{title}' id='public-profile-award-modal-{i}'><p>{desc}</p>
<ql-button href='{source}' label='Learn more' slot='action' text>Learn more</ql-button></ql-dialog>"""
        )
    league = "<div class='profile-league'><h2>Bronze League</h2><strong>100 points</strong></div>" if badges else ""
    body = "\n".join(cards) if badges else f"<p>{name} hasn't earned any badges yet.</p>"
    return f"""<html><body><h1 class='ql-display-small'>{name}</h1><p>Member since 2026</p>{league}
<div class='profile-badges'>{body}</div>{''.join(dialogs)}</body></html>"""


SKILL = ("/course_templates/978", "Complete the intermediate X skill badge course.")
COURSE = ("/course_templates/60", "Complete the introductory course.")
GAME = ("/games/32800", "Play the arcade game.")


def mock_transport(pages: dict[str, object]) -> httpx.MockTransport:
    """pages: profile uuid -> html string | int status | callable(request) -> Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        profile_id = request.url.path.rstrip("/").split("/")[-1]
        page = pages.get(profile_id, 404)
        if callable(page):
            return page(request)
        if isinstance(page, int):
            return httpx.Response(page, text="error")
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


def collectors_for(pages: dict[str, object], settings) -> Callable[[], dict]:
    from app.collectors import build_collectors

    return lambda: build_collectors(settings, transport=mock_transport(pages))
