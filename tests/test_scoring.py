from dataclasses import dataclass
from datetime import date, datetime

from app.scoring import ScoringConfig, member_entry, rank_entries, score_member, team_entry


@dataclass
class B:
    badge_id: str
    category: str
    earned_date: date | None = date(2026, 9, 10)
    title: str = "Badge"
    deprecated: bool = False
    source_path: str | None = None
    first_seen_at: datetime | None = None


def cfg(**kw) -> ScoringConfig:
    return ScoringConfig.from_dict(kw)


def test_default_points():
    s = score_member(1, "A", [B("1", "skill_badge"), B("2", "course_badge"), B("3", "game_badge"), B("4", "other")], cfg())
    assert s.score == 18
    assert s.counted_badges == 3
    assert s.skill_badges == 1
    assert s.counts_by_category == {"skill_badge": 1, "course_badge": 1, "game_badge": 1, "other": 0}
    assert s.points_by_category["skill_badge"] == 10


def test_duplicate_badge_ids_count_once():
    s = score_member(1, "A", [B("1", "skill_badge"), B("1", "skill_badge")], cfg())
    assert s.score == 10 and s.counted_badges == 1


def test_program_window_inclusive():
    c = cfg(program_start="2026-09-01", program_end="2026-09-30")
    badges = [
        B("1", "skill_badge", date(2026, 8, 31)),
        B("2", "skill_badge", date(2026, 9, 1)),
        B("3", "skill_badge", date(2026, 9, 30)),
        B("4", "skill_badge", date(2026, 10, 1)),
    ]
    assert score_member(1, "A", badges, c).score == 20
    from app.scoring import evaluate_badge

    assert evaluate_badge(badges[0], c).reason == "before_start"
    assert evaluate_badge(badges[3], c).reason == "after_end"


def test_deprecated_toggle():
    badges = [B("1", "skill_badge", deprecated=True)]
    assert score_member(1, "A", badges, cfg(count_deprecated=True)).score == 10
    assert score_member(1, "A", badges, cfg(count_deprecated=False)).score == 0


def test_overrides_win_over_category():
    c = cfg(badge_overrides={"Special": 50, "Level 3: Generative AI": 0})
    badges = [B("1", "course_badge", title="Special"), B("2", "game_badge", title="Level 3: Generative AI")]
    s = score_member(1, "A", badges, c)
    assert s.score == 50 and s.counted_badges == 1


def test_cap():
    s = score_member(1, "A", [B(str(i), "skill_badge", date(2026, 9, i + 1)) for i in range(5)], cfg(max_points_per_member=25))
    assert s.score == 25 and s.capped
    assert s.reached_at == date(2026, 9, 3)  # third badge pushed the total to 25


def _team(scores, working=None):
    out = []
    for i, sc in enumerate(scores):
        out.append(
            score_member(i, f"m{i}", [B(str(j), "skill_badge") for j in range(sc // 10)], cfg(),
                         working_link=True if working is None else working[i])
        )
    return out


def test_team_sum_vs_average():
    members = _team([30, 10, 0], working=[True, True, False])
    assert team_entry(1, "T", members, cfg(team_aggregation="sum")).score == 40
    assert team_entry(1, "T", members, cfg(team_aggregation="average")).score == 20  # 40 / 2 working


def _entry(name, score, skill, reached):
    badges = [B(str(i), "skill_badge", reached) for i in range(skill)]
    badges += [B(f"c{i}", "course_badge", reached) for i in range((score - skill * 10) // 3)]
    return member_entry(score_member(hash(name) % 1000, name, badges, cfg()))


def test_tiebreak_score():
    ranked = rank_entries([_entry("a", 10, 1, date(2026, 9, 1)), _entry("b", 20, 2, date(2026, 9, 1))])
    assert [e.name for _, e in ranked] == ["b", "a"]


def test_tiebreak_skill_badges():
    # 30 points: 3 skill badges vs 0 skill + 10 course badges
    a = _entry("a", 30, 0, date(2026, 9, 1))
    b = _entry("b", 30, 3, date(2026, 9, 1))
    assert a.score == b.score == 30
    # Skill badges break score ties among non-finishers (top is a 40-point finisher).
    top = _entry("top", 40, 4, date(2026, 9, 1))
    assert [e.name for _, e in rank_entries([a, b, top])] == ["top", "b", "a"]
    # Among finishers (both at max score) completion time, then name, decides; skill count does not.
    assert [e.name for _, e in rank_entries([b, a])] == ["a", "b"]


def test_tiebreak_reached_final_score_earlier():
    a = _entry("a", 20, 2, date(2026, 9, 9))
    b = _entry("b", 20, 2, date(2026, 9, 3))
    assert [e.name for _, e in rank_entries([a, b])] == ["b", "a"]


def test_tiebreak_name():
    a = _entry("Zed", 20, 2, date(2026, 9, 3))
    b = _entry("amy", 20, 2, date(2026, 9, 3))
    ranked = rank_entries([a, b])
    assert [e.name for _, e in ranked] == ["amy", "Zed"]
    assert [r for r, _ in ranked] == [1, 2]


def test_finishers_ranked_by_completion_date():
    # Everyone at max score finished; order is by completion date, not by name.
    zed = _entry("Zed", 30, 3, date(2026, 9, 1))
    bob = _entry("Bob", 30, 3, date(2026, 9, 5))
    alice = _entry("Alice", 30, 3, date(2026, 9, 10))
    charlie = _entry("Charlie", 20, 2, date(2026, 9, 1))

    ranked = rank_entries([bob, charlie, zed, alice])
    assert [e.name for _, e in ranked] == ["Zed", "Bob", "Alice", "Charlie"]
    assert [r for r, _ in ranked] == [1, 2, 3, 4]


def test_finishers_with_target_badges_ranked_by_completion_date():
    config = cfg(target_badges=3)
    zed = _entry("Zed", 30, 3, date(2026, 9, 1))
    bob = _entry("Bob", 30, 3, date(2026, 9, 5))
    alice = _entry("Alice", 30, 3, date(2026, 9, 10))
    david = _entry("David", 20, 2, date(2026, 9, 1))

    ranked = rank_entries([bob, david, zed, alice], config)
    assert [e.name for _, e in ranked] == ["Zed", "Bob", "Alice", "David"]


def _finisher(member_id, name, day, last_seen):
    """Completes a two-course syllabus; the second badge (earned on `day`) completes the set."""
    badges = [
        B("1", "skill_badge", date(2026, 10, 15), source_path="/course_templates/638",
          first_seen_at=datetime(2026, 10, 15, 6, 0)),
        B("2", "skill_badge", day, source_path="/course_templates/1426", first_seen_at=last_seen),
    ]
    return member_entry(score_member(member_id, name, badges, cfg(syllabus=SYLLABUS)))


def test_same_completion_date_broken_by_first_seen_at():
    c = cfg(syllabus=SYLLABUS)
    day = date(2026, 10, 25)
    amy = _finisher(1, "Amy", day, datetime(2026, 10, 25, 18, 0))
    zed = _finisher(2, "Zed", day, datetime(2026, 10, 25, 9, 30))  # seen first that day
    assert zed.completed_seen_at == datetime(2026, 10, 25, 9, 30)
    assert zed.breakdown["completed_seen_at"] == "2026-10-25T09:30:00"
    assert [e.name for _, e in rank_entries([amy, zed], c)] == ["Zed", "Amy"]

    # Fully tied (same date and seen time, or no seen time): name, then id.
    bob = _finisher(3, "bob", day, None)
    al = _finisher(4, "Al", day, None)
    assert bob.breakdown["completed_seen_at"] is None
    assert [e.name for _, e in rank_entries([bob, amy, al, zed], c)] == ["Zed", "Amy", "Al", "bob"]


def test_finisher_without_reached_at_goes_after_dated_finishers():
    c = cfg(target_badges=1)
    undated = member_entry(score_member(1, "Aaron", [B("1", "skill_badge", None)], c))
    assert undated.reached_at is None
    late = member_entry(score_member(2, "Zoe", [B("1", "skill_badge", date(2026, 10, 30))], c))
    early = member_entry(score_member(3, "Yan", [B("1", "skill_badge", date(2026, 10, 1))], c))
    unfinished = member_entry(score_member(4, "Abe", [], c))
    assert [e.name for _, e in rank_entries([undated, unfinished, late, early], c)] == ["Yan", "Zoe", "Aaron", "Abe"]


def test_completed_seen_at_picks_latest_seen_badge_on_completion_day():
    s = score_member(
        1,
        "A",
        [
            B("1", "skill_badge", date(2026, 10, 25), first_seen_at=datetime(2026, 10, 25, 20, 0)),
            B("2", "skill_badge", date(2026, 10, 25), first_seen_at=datetime(2026, 10, 25, 8, 0)),
            B("3", "skill_badge", date(2026, 10, 20), first_seen_at=datetime(2026, 10, 26, 0, 0)),
        ],
        cfg(),
    )
    assert s.reached_at == date(2026, 10, 25) and s.completed_seen_at == datetime(2026, 10, 25, 20, 0)


def test_team_completed_seen_at_is_latest_member():
    a = score_member(1, "A", [B("1", "skill_badge", date(2026, 10, 2), first_seen_at=datetime(2026, 10, 2, 9))], cfg())
    b = score_member(2, "B", [B("1", "skill_badge", date(2026, 10, 2), first_seen_at=datetime(2026, 10, 2, 17))], cfg())
    team = team_entry(7, "T", [a, b], cfg())
    assert team.completed_seen_at == datetime(2026, 10, 2, 17)
    assert team.breakdown["completed_seen_at"] == "2026-10-02T17:00:00"


def test_program_start_after_13th_midnight():
    c = cfg(program_start="2026-10-14")
    b_before = B("1", "skill_badge", date(2026, 10, 13))
    b_after = B("2", "skill_badge", date(2026, 10, 14))
    b_later = B("3", "skill_badge", date(2026, 10, 21))

    from app.scoring import evaluate_badge
    assert evaluate_badge(b_before, c).reason == "before_start"
    assert evaluate_badge(b_after, c).counted is True
    assert evaluate_badge(b_later, c).counted is True


def test_config_hash_and_snapshot_stable():
    a, b = cfg(points={"skill_badge": 10}), cfg(points={"skill_badge": 10})
    assert a.hash() == b.hash()
    assert cfg(points={"skill_badge": 11}).hash() != a.hash()
    assert a.snapshot()["points"]["course_badge"] == 3


def test_repo_scoring_yaml_loads():
    from pathlib import Path

    c = ScoringConfig.load(Path(__file__).parent.parent / "scoring.yaml")
    assert c.points == {"skill_badge": 10, "course_badge": 3, "game_badge": 5, "other": 0}
    assert c.team_aggregation == "sum"
    assert len(c.syllabus) == 10
    assert c.completion_target == 10
    assert c.max_points_per_member == 100
    assert {course.points for course in c.syllabus} == {10}


SYLLABUS = [
    {"title": "Build a Website on Google Cloud", "url": "https://www.skills.google/course_templates/638", "points": 13},
    {"title": "Develop AI-Powered Prototypes", "url": "https://www.skills.google/course_templates/1426/", "points": 0},
]


def test_syllabus_only_counts_listed_courses_with_their_points():
    c = cfg(syllabus=SYLLABUS)
    s = score_member(
        1,
        "A",
        [
            B("1", "skill_badge", source_path="/course_templates/638"),
            B("2", "skill_badge", source_path="/course_templates/978"),  # not in syllabus
            B("3", "game_badge", source_path="/games/32800"),
        ],
        c,
    )
    assert s.score == 13 and s.counted_badges == 1


def test_syllabus_zero_point_badge_still_completes():
    c = cfg(syllabus=SYLLABUS)
    s = score_member(
        1,
        "A",
        [
            B("1", "skill_badge", date(2026, 10, 20), source_path="/course_templates/638"),
            B("2", "skill_badge", date(2026, 10, 25), source_path="/course_templates/1426"),
        ],
        c,
    )
    assert s.score == 13 and s.counted_badges == 2
    assert s.reached_at == date(2026, 10, 25)  # completing the set needs the 0-point badge too
    assert c.completion_target == 2


def test_syllabus_title_fallback_and_course_counted_once():
    c = cfg(syllabus=SYLLABUS)
    s = score_member(
        1,
        "A",
        [
            B("1", "skill_badge", title="Build a  Website on Google Cloud"),
            B("2", "skill_badge", source_path="/course_templates/638"),
            B("3", "skill_badge", title="Some Other Course"),
        ],
        c,
    )
    assert s.score == 13 and s.counted_badges == 1


def test_syllabus_completers_ranked_first():
    c = cfg(syllabus=SYLLABUS)
    done = [B("1", "skill_badge", date(2026, 10, 20), source_path="/course_templates/638"),
            B("2", "skill_badge", date(2026, 10, 22), source_path="/course_templates/1426")]
    early = [B("1", "skill_badge", date(2026, 10, 15), source_path="/course_templates/638")]
    entries = [
        member_entry(score_member(1, "Zed", done, c)),
        member_entry(score_member(2, "Amy", early, c)),
    ]
    assert [e.name for _, e in rank_entries(entries, c)] == ["Zed", "Amy"]
