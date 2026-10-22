from datetime import date

import pytest

from app.collectors.skills_parser import ParseError, classify, parse_earned_date, parse_profile
from tests.helpers import read_fixture

URL = "https://www.skills.google/public_profiles/92edbe64-5685-4127-a4d6-24202fa95d29"


@pytest.fixture(scope="module")
def multi():
    return parse_profile(read_fixture("profile_multi.html"), URL)


def test_profile_fields(multi):
    assert multi.status == "ok"
    assert multi.display_name == "Suyash Barnawal"
    assert multi.member_since == 2025
    assert multi.league == "Silver League"
    assert multi.platform_points == 2220
    assert multi.avatar_url == "https://lh3.googleusercontent.com/a/avatar-example"
    assert len(multi.badges) == 4


def test_game_badge(multi):
    b = multi.badges[0]
    assert b.badge_id == "19820890"
    assert b.badge_url == f"{URL}/badges/19820890"
    assert b.image_url == "https://cdn.qwiklabs.com/eNdZJT5MeYt6TqgsDvDuqyfgItkHfqLqlDs6ggJr%2BYA%3D"
    assert b.title == "Level 3: Generative AI"
    assert b.earned_raw == "Earned Oct 31, 2025 EDT"
    assert b.earned_date == date(2025, 10, 31)
    assert b.description.startswith("Nearly 1 in 3 people worldwide")
    assert b.source_path == "/games/32800"
    assert b.category == "game_badge"
    assert b.deprecated is False
    assert b.level is None


def test_skill_badge(multi):
    b = multi.badges[1]
    assert b.badge_id == "19785601"
    assert b.title == "Develop Gen AI Apps with Gemini and Streamlit"
    assert b.earned_date == date(2025, 10, 30)
    assert b.source_path == "/course_templates/978"
    assert b.category == "skill_badge"
    assert b.level == "intermediate"
    assert b.deprecated is False


def test_course_badge_without_skill_badge_text(multi):
    b = multi.badges[2]
    assert b.title == "Google Cloud Fundamentals: Core Infrastructure"
    assert b.earned_date == date(2025, 9, 5)
    assert b.source_path == "/course_templates/60"
    assert b.category == "course_badge"
    assert b.level is None


def test_deprecated_badge_keeps_category(multi):
    b = multi.badges[3]
    assert b.title == "[Deprecated] Build a Website on Google Cloud"
    assert b.earned_raw == "Earned Jan 15, 2025 EST"
    assert b.earned_date == date(2025, 1, 15)
    assert b.category == "skill_badge"
    assert b.deprecated is True
    assert b.level == "advanced"


def test_empty_profile_is_zero_badges_not_error():
    result = parse_profile(read_fixture("profile_empty.html"), URL)
    assert result.status == "ok"
    assert result.display_name == "New Student"
    assert result.member_since == 2026
    assert result.league is None
    assert result.platform_points is None
    assert result.badges == []


def test_page_without_h1_is_parse_error():
    with pytest.raises(ParseError):
        parse_profile(read_fixture("not_a_profile.html"), URL)


def test_fallback_without_profile_badge_class():
    html = read_fixture("profile_multi.html").replace("profile-badge'", "restyled-card'")
    result = parse_profile(html, URL)
    assert [b.badge_id for b in result.badges] == ["19820890", "19785601", "15000001", "9000042"]
    assert result.badges[0].title == "Level 3: Generative AI"
    assert result.badges[0].earned_date == date(2025, 10, 31)


def test_classify_rules():
    assert classify("x", "anything", "/games/1")[0] == "game_badge"
    assert classify("x", "A Skill Badge course", "/course_templates/1")[0] == "skill_badge"
    assert classify("x", "course", "/course_templates/1")[0] == "course_badge"
    assert classify("x", None, None)[0] == "other"
    assert classify("x", "", "/focuses/1")[0] == "other"


def test_parse_earned_date_variants():
    assert parse_earned_date("Earned Sep 5, 2025 EDT") == date(2025, 9, 5)
    assert parse_earned_date("Earned Dec 1, 2025 EST") == date(2025, 12, 1)
    assert parse_earned_date("garbage") is None
