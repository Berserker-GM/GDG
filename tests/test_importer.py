from datetime import datetime

import pytest
from sqlalchemy import select

from app.importer import FormImportError, import_form, map_headers, normalize_usn
from app.models import Badge, Member, Team
from app.urls import check_profile_url
from tests.helpers import UUIDS, form_row, profile_url, write_form


def members(session):
    return {m.usn: m for m in session.scalars(select(Member))}


@pytest.fixture
def form(tmp_path):
    rows = [
        form_row(" 1by23cs001 ", "  Asha   Rao ", profile_url(0) + "/", ts=datetime(2026, 9, 1, 9)),
        # Duplicate USN with a different profile: the later timestamp must win.
        form_row("1BY23CS002", "Bala K", profile_url(1), ts=datetime(2026, 9, 2, 9)),
        form_row("1by23cs002", "Bala K", profile_url(2) + "?utm=x", ts=datetime(2026, 9, 5, 9)),
        form_row("1BY23CS003", "Chitra", "https://me.developers.google.com/u/1234567890"),
        form_row("1BY23CS004", "Dev", "https://example.com/not-a-profile"),
        form_row("1BY23CS005", "Esha", profile_url(4), registered="No", email="esha.gmail.com"),
        form_row(
            "1BY23CS006", "Farhan",
            f"https://www.cloudskillsboost.google/public_profiles/{UUIDS[5].upper()}",
            year="3",
        ),
        form_row("1BY23CS007", "Gita", None),
        [None] * 13,  # blank row
    ]
    return write_form(tmp_path / "form.xlsx", rows)


def test_header_mapping_handles_messy_headers():
    cols = map_headers(
        ["Timestamp", "Email address", "Full Name ", "College Email Address\n", "USN  ", "Year of Study",
         "Department ", "Upload Your BMSIT College ID Card",
         "Have you completed the official Google registration form? \n", "Google Cloud Profile Link ",
         "Upload Screenshot of Official Form Submission", "Column 11", "Column 12"]
    )
    assert cols == {
        "timestamp": 0, "email": 1, "full_name": 2, "college_email": 3, "usn": 4, "year": 5,
        "department": 6, "registration": 8, "profile_url": 9,
    }


def test_missing_required_columns():
    with pytest.raises(FormImportError):
        map_headers(["Timestamp", "Name"])


def test_import(session, form):
    report = import_form(session, form)
    session.commit()
    m = members(session)

    assert report.total_rows == 8
    assert report.unique_students == 7
    assert set(m) == {f"1BY23CS00{i}" for i in range(1, 8)}

    asha = m["1BY23CS001"]
    assert asha.full_name == "Asha Rao"
    assert asha.year == "2"
    assert asha.link_status == "pending"
    assert asha.profile_id == UUIDS[0]
    assert asha.profile_url == f"https://www.skills.google/public_profiles/{UUIDS[0]}"

    # Duplicate: latest wins and the conflict is logged
    assert m["1BY23CS002"].profile_id == UUIDS[2]
    assert len(report.duplicates_resolved) == 1
    assert report.usn_conflicts[0]["usn"] == "1BY23CS002"
    assert report.usn_conflicts[0]["profile_conflict"] is True

    dev_prog = m["1BY23CS003"]
    assert dev_prog.link_status == "unsupported_platform"
    assert dev_prog.profile_url == "https://me.developers.google.com/u/1234567890"
    assert dev_prog.platform == "google_developers"

    assert m["1BY23CS004"].link_status == "invalid_url"
    assert m["1BY23CS007"].link_status == "missing_url"

    esha = m["1BY23CS005"]
    assert "not_registered" in esha.flags
    assert "invalid_email" in esha.flags
    assert esha.link_status == "pending"  # still processed
    assert esha.registration_answer == "No"

    legacy = m["1BY23CS006"]
    assert legacy.profile_id == UUIDS[5]
    assert legacy.profile_url.startswith("https://www.skills.google/public_profiles/")
    assert legacy.year == "3"

    assert len(report.invalid_links) == 1
    assert len(report.unsupported_links) == 1
    assert len(report.missing_links) == 1
    assert report.teams == 0


def test_reimport_upserts_deactivates_and_resets_badges(session, tmp_path):
    first = write_form(tmp_path / "a.xlsx", [
        form_row("U1", "One", profile_url(10)),
        form_row("U2", "Two", profile_url(11)),
        form_row("U3", "Three", profile_url(12)),
    ])
    import_form(session, first)
    session.commit()
    m = members(session)
    for usn in ("U1", "U2"):
        m[usn].link_status = "ok"
        session.add(Badge(member_id=m[usn].id, badge_id="1", title="b"))
    session.commit()

    second = write_form(tmp_path / "b.xlsx", [
        form_row("U1", "One", profile_url(10)),       # unchanged
        form_row("U2", "Two", profile_url(20)),       # profile changed
        form_row("U4", "Four", profile_url(13)),      # new
    ])
    report = import_form(session, second)
    session.commit()
    session.expire_all()
    m = members(session)

    assert report.created == 1 and report.updated == 2 and report.deactivated == 1
    assert m["U3"].active is False
    assert m["U1"].link_status == "ok" and len(m["U1"].badges) == 1
    assert m["U2"].link_status == "pending" and m["U2"].badges == []
    assert m["U2"].profile_id == UUIDS[20]
    # Running it again is a no-op
    again = import_form(session, second)
    assert again.created == 0 and again.deactivated == 0


def test_teams_file_and_column(session, tmp_path):
    form = write_form(
        tmp_path / "form.xlsx",
        [
            form_row("T1", "A", profile_url(30), team="Alpha"),
            form_row("T2", "B", profile_url(31), team="Alpha"),
            form_row("T3", "C", profile_url(32), team=""),
        ],
        with_team_column=True,
    )
    report = import_form(session, form)
    session.commit()
    assert report.team_source == "column" and report.teams == 1
    assert [u["usn"] for u in report.unassigned] == ["T3"]

    mapping = tmp_path / "teams.csv"
    mapping.write_text("USN,Team\nt3 ,Beta\nT2,Beta\n", encoding="utf-8")
    report = import_form(session, form, mapping)
    session.commit()
    m = members(session)
    assert report.team_source == "file"
    assert m["T1"].team.name == "Alpha"  # column fallback
    assert m["T2"].team.name == "Beta"   # file wins
    assert m["T3"].team.name == "Beta"
    assert session.scalars(select(Team)).all().__len__() == 2


def test_shared_profile_flagged(session, tmp_path):
    form = write_form(tmp_path / "f.xlsx", [
        form_row("S1", "A", profile_url(40)),
        form_row("S2", "B", profile_url(40, host="https://skills.google")),
    ])
    report = import_form(session, form)
    assert report.shared_profiles == [{"profile_id": UUIDS[40], "usns": ["S1", "S2"]}]
    assert all("shared_profile" in m.flags for m in members(session).values())


@pytest.mark.parametrize(
    "url,status",
    [
        (f"https://www.skills.google/public_profiles/{UUIDS[0]}", "pending"),
        (f"https://skills.google/public_profiles/{UUIDS[0]}/", "pending"),
        (f"https://www.skills.google/public_profiles/{UUIDS[0]}?locale=en", "pending"),
        (f"www.skills.google/public_profiles/{UUIDS[0]}", "pending"),
        (f"https://www.cloudskillsboost.google/public_profiles/{UUIDS[0]}", "pending"),
        ("https://me.developers.google.com/u/123", "unsupported_platform"),
        ("https://www.skills.google/public_profiles/not-a-uuid", "invalid_url"),
        ("", "missing_url"),
        (None, "missing_url"),
    ],
)
def test_url_validation(url, status):
    result = check_profile_url(url)
    assert result.status == status
    if status == "pending":
        assert result.url == f"https://www.skills.google/public_profiles/{UUIDS[0]}"
        assert result.profile_id == UUIDS[0]


def test_normalize_usn():
    assert normalize_usn(" 1by23 cs001\n") == "1BY23CS001"
