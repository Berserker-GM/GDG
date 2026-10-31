from dataclasses import dataclass
from datetime import date

from app.scoring import ScoringConfig, member_entry, rank_entries, score_member, team_entry


@dataclass
class B:
    badge_id: str
    category: str
    earned_date: date | None = date(2026, 9, 10)
    title: str = "Badge"
    deprecated: bool = False


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
    assert [e.name for _, e in rank_entries([a, b])] == ["b", "a"]


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


def test_first_to_complete_on_top_then_alphabetical():
    # Winner: 'Zed' completed first on Sep 1
    # Subsequent finishers: 'Bob' completed on Sep 5, 'Alice' completed on Sep 10
    # Expected ranking:
    # 1: Zed (first to complete on top)
    # 2: Alice (after that, whoever completes is sorted alphabetically)
    # 3: Bob
    # 4: Charlie (lower score, ranked below completers)
    zed = _entry("Zed", 30, 3, date(2026, 9, 1))
    bob = _entry("Bob", 30, 3, date(2026, 9, 5))
    alice = _entry("Alice", 30, 3, date(2026, 9, 10))
    charlie = _entry("Charlie", 20, 2, date(2026, 9, 1))

    ranked = rank_entries([bob, charlie, zed, alice])
    names = [e.name for _, e in ranked]
    assert names == ["Zed", "Alice", "Bob", "Charlie"]
    assert [r for r, _ in ranked] == [1, 2, 3, 4]


def test_first_to_complete_with_target_badges():
    # With target_badges=3, anyone with >= 3 counted badges is considered completed
    config = cfg(target_badges=3)
    zed = _entry("Zed", 30, 3, date(2026, 9, 1))
    bob = _entry("Bob", 30, 3, date(2026, 9, 5))
    alice = _entry("Alice", 30, 3, date(2026, 9, 10))
    david = _entry("David", 20, 2, date(2026, 9, 1))

    ranked = rank_entries([bob, david, zed, alice], config)
    names = [e.name for _, e in ranked]
    assert names == ["Zed", "Alice", "Bob", "David"]


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
