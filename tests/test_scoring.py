"""Tests for the scoring engine.

This is maths, so it gets real tests. The headline one replays every 2025/26
player-gameweek and demands an exact match: the spec's line is that if it does not
match to the point, everything above it is built on sand.
"""

from __future__ import annotations

import csv
import dataclasses
import gzip
import json
from pathlib import Path

import pytest

from fpl.scoring import (
    MatchStats,
    ScoringConstants,
    ScoringRules,
    defensive_actions,
    normalise_position,
    score_match,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rules_2026_27() -> ScoringRules:
    """Built from the real API payload, the same path production uses."""
    with gzip.open(FIXTURES / "bootstrap-static-2026-08-05.json.gz", "rb") as fh:
        bootstrap = json.loads(fh.read().decode("utf-8"))

    rows = []
    for name, value in bootstrap["game_config"]["scoring"].items():
        if isinstance(value, dict):
            rows += [{"rule_name": name, "position": p, "points": v} for p, v in value.items()]
        else:
            rows.append({"rule_name": name, "position": "ALL", "points": value})
    return ScoringRules.from_rows(rows)


@pytest.fixture(scope="module")
def rules_2025_26(rules_2026_27: ScoringRules) -> ScoringRules:
    return rules_2026_27.for_season_2025_26()


@pytest.fixture(scope="module")
def played_rows() -> list[dict]:
    with gzip.open(FIXTURES / "scoring_2025_26_played.csv.gz", "rt", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------- the acceptance criterion
def test_engine_reproduces_every_2025_26_score_exactly(played_rows, rules_2025_26):
    """All 11,498 player-gameweeks with minutes, not the 200 the spec asked for."""
    mismatches = []
    for row in played_rows:
        computed = score_match(
            MatchStats.from_mapping(row), normalise_position(row["position"]), rules_2025_26
        ).total
        if computed != int(row["total_points"]):
            mismatches.append((row["name"], row["GW"], computed, row["total_points"]))

    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:5]}"
    assert len(played_rows) > 11_000, "fixture looks truncated"


def test_clean_sheets_derived_from_minutes_and_concessions(played_rows, rules_2025_26):
    """Projection has no clean-sheet flag to read, so the derived path is the one
    that will actually run. It must be exact too, not merely close."""
    mismatches = 0
    for row in played_rows:
        stats = dataclasses.replace(MatchStats.from_mapping(row), clean_sheets=None)
        computed = score_match(stats, normalise_position(row["position"]), rules_2025_26).total
        mismatches += computed != int(row["total_points"])

    assert mismatches == 0


def test_component_breakdown_sums_to_the_actual_total(played_rows, rules_2025_26):
    """The breakdown is the product, not a debug aid: it must reconcile."""
    for row in played_rows[:2000]:
        breakdown = score_match(
            MatchStats.from_mapping(row), normalise_position(row["position"]), rules_2025_26
        )
        assert sum(v for k, v in breakdown.as_dict().items() if k != "total") == breakdown.total
        assert breakdown.total == int(row["total_points"])


def test_defcon_formula_holds_per_match(played_rows):
    """Per-match confirmation of the formula reverse-engineered from season totals."""
    checked = 0
    for row in played_rows:
        position = normalise_position(row["position"])
        if position == "GKP":
            continue
        computed = defensive_actions(
            position, int(row["tackles"]),
            int(row["clearances_blocks_interceptions"]), int(row["recoveries"]),
        )
        assert computed == int(row["defensive_contribution"]), row["name"]
        checked += 1
    assert checked > 10_000


# --------------------------------------------------------------- rule loading
def test_rules_load_from_the_api_payload(rules_2026_27):
    assert rules_2026_27.goals_scored == {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
    assert rules_2026_27.clean_sheets == {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
    assert rules_2026_27.defensive_contribution == {"GKP": 0, "DEF": 2, "MID": 2, "FWD": 2}
    assert rules_2026_27.assists == 3
    assert rules_2026_27.red_cards == -3


def test_stepping_back_a_season_changes_only_keeper_goals(rules_2026_27, rules_2025_26):
    assert rules_2025_26.goals_scored["GKP"] == 6
    assert rules_2026_27.goals_scored["GKP"] == 10
    for position in ("DEF", "MID", "FWD"):
        assert rules_2025_26.goals_scored[position] == rules_2026_27.goals_scored[position]
    assert rules_2025_26.clean_sheets == rules_2026_27.clean_sheets
    assert rules_2025_26.assists == rules_2026_27.assists


# ------------------------------------------------------------------ edge cases
def test_no_appearance_scores_nothing(rules_2026_27):
    assert score_match(MatchStats(minutes=0, bonus=3), "MID", rules_2026_27).total == 0


def test_appearance_points_step_at_exactly_60_minutes(rules_2026_27):
    assert score_match(MatchStats(minutes=59), "MID", rules_2026_27).appearance == 1
    assert score_match(MatchStats(minutes=60), "MID", rules_2026_27).appearance == 2


def test_clean_sheet_requires_sixty_minutes(rules_2026_27):
    assert score_match(MatchStats(minutes=59, goals_conceded=0), "DEF", rules_2026_27).clean_sheet == 0
    assert score_match(MatchStats(minutes=60, goals_conceded=0), "DEF", rules_2026_27).clean_sheet == 4


def test_midfielder_clean_sheet_is_worth_one(rules_2026_27):
    assert score_match(MatchStats(minutes=90, goals_conceded=0), "MID", rules_2026_27).clean_sheet == 1


def test_forward_gets_nothing_for_a_clean_sheet(rules_2026_27):
    assert score_match(MatchStats(minutes=90, goals_conceded=0), "FWD", rules_2026_27).clean_sheet == 0


@pytest.mark.parametrize("conceded,expected", [(0, 0), (1, 0), (2, -1), (3, -1), (4, -2), (5, -2)])
def test_concession_penalty_applies_per_two_goals(conceded, expected, rules_2026_27):
    got = score_match(MatchStats(minutes=90, goals_conceded=conceded), "DEF", rules_2026_27)
    assert got.goals_conceded == expected


def test_midfielders_are_not_penalised_for_concessions(rules_2026_27):
    assert score_match(MatchStats(minutes=90, goals_conceded=4), "MID", rules_2026_27).goals_conceded == 0


@pytest.mark.parametrize("saves,expected", [(0, 0), (2, 0), (3, 1), (5, 1), (6, 2), (9, 3)])
def test_saves_score_one_per_three(saves, expected, rules_2026_27):
    assert score_match(MatchStats(minutes=90, saves=saves), "GKP", rules_2026_27).saves == expected


def test_goalkeeper_goals_are_worth_ten_in_2026_27(rules_2026_27):
    """The change the spec did not mention; it falls out of the rules table."""
    assert score_match(MatchStats(minutes=90, goals_scored=1), "GKP", rules_2026_27).goals == 10


@pytest.mark.parametrize("position,threshold", [("DEF", 10), ("MID", 12), ("FWD", 12)])
def test_defcon_threshold_is_a_step_not_a_rate(position, threshold, rules_2026_27):
    """Modelling this with a mean instead of a threshold is the classic mistake."""
    below = MatchStats(minutes=90, tackles=threshold - 1, recoveries=0)
    at = MatchStats(minutes=90, tackles=threshold, recoveries=0)
    assert score_match(below, position, rules_2026_27).defensive_contribution == 0
    assert score_match(at, position, rules_2026_27).defensive_contribution == 2


def test_defenders_do_not_count_recoveries_towards_defcon(rules_2026_27):
    """A defender with 9 tackles+CBI and 50 recoveries still misses the threshold."""
    stats = MatchStats(minutes=90, tackles=9, recoveries=50)
    assert score_match(stats, "DEF", rules_2026_27).defensive_contribution == 0
    assert score_match(stats, "MID", rules_2026_27).defensive_contribution == 2


def test_goalkeepers_never_score_defcon(rules_2026_27):
    stats = MatchStats(minutes=90, tackles=5, clearances_blocks_interceptions=20, recoveries=30)
    assert score_match(stats, "GKP", rules_2026_27).defensive_contribution == 0


def test_penalty_save_and_miss_values(rules_2026_27):
    assert score_match(MatchStats(minutes=90, penalties_saved=1), "GKP", rules_2026_27).penalties_saved == 5
    assert score_match(MatchStats(minutes=90, penalties_missed=1), "FWD", rules_2026_27).penalties_missed == -2


def test_cards_and_own_goals(rules_2026_27):
    got = score_match(MatchStats(minutes=90, yellow_cards=1, red_cards=1, own_goals=1), "DEF", rules_2026_27)
    assert got.cards == -4
    assert got.own_goals == -2


def test_a_full_haul_reconciles(rules_2026_27):
    """Defender: 90 mins, goal, assist, clean sheet, DEFCON hit, 3 bonus."""
    stats = MatchStats(minutes=90, goals_scored=1, assists=1, goals_conceded=0,
                       tackles=6, clearances_blocks_interceptions=5, bonus=3)
    got = score_match(stats, "DEF", rules_2026_27)
    assert got.as_dict() == {
        "appearance": 2, "goals": 6, "assists": 3, "clean_sheet": 4, "goals_conceded": 0,
        "saves": 0, "penalties_saved": 0, "penalties_missed": 0, "cards": 0,
        "own_goals": 0, "defensive_contribution": 2, "bonus": 3, "total": 20,
    }


def test_position_aliases(rules_2026_27):
    assert normalise_position("GK") == "GKP"
    assert normalise_position("gkp") == "GKP"
    assert normalise_position(1) == "GKP"
    assert normalise_position(4) == "FWD"
    with pytest.raises(ValueError):
        normalise_position("STRIKER")


def test_constants_are_labelled_as_not_coming_from_the_api():
    """Guards against anyone later mistaking these for API-derived values."""
    assert "official_rules" in ScoringConstants().source
