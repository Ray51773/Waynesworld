"""Tests for the projection model's maths.

Testing the parts where being wrong is silent: the threshold handling for DEFCON, the
distribution integration for concessions and saves, minutes as a multiplier on
everything, and the shrinkage that stops an eight-minute cameo becoming a rate.
"""

from __future__ import annotations

import math

import pytest

from fpl.model.minutes import MinutesProfile, availability_multiplier
from fpl.model.projections import _expected_floor_div, _poisson_tail, _shrink
from fpl.model.team_ratings import TeamRatings


# ------------------------------------------------------------ distributions
def test_poisson_tail_matches_hand_computed_values():
    # P(X >= 1) = 1 - e^-lambda
    assert _poisson_tail(2.0, 1) == pytest.approx(1 - math.exp(-2.0))
    # P(X >= 0) is certain
    assert _poisson_tail(2.0, 0) == 1.0
    # A zero rate never clears a positive threshold
    assert _poisson_tail(0.0, 1) == 0.0


def test_poisson_tail_is_monotonic_in_the_threshold():
    values = [_poisson_tail(10.0, k) for k in range(1, 20)]
    assert values == sorted(values, reverse=True)


def test_poisson_tail_rises_with_the_rate():
    """The whole point of DEFCON: a higher action rate clears the bar more often."""
    assert _poisson_tail(8.0, 12) < _poisson_tail(12.0, 12) < _poisson_tail(18.0, 12)


def test_expected_floor_div_is_not_the_naive_mean():
    """E[floor(X/2)] != floor(E[X]/2). Using the mean is the classic mistake here,
    and it misprices every defender in a leaky team."""
    lam = 1.5
    exact = _expected_floor_div(lam, 2)
    naive = math.floor(lam) // 2
    assert exact != naive
    assert 0.0 < exact < lam / 2


def test_expected_floor_div_zero_rate_is_zero():
    assert _expected_floor_div(0.0, 3) == 0.0


def test_expected_floor_div_grows_with_the_rate():
    values = [_expected_floor_div(lam, 3) for lam in (1.0, 3.0, 6.0, 9.0)]
    assert values == sorted(values)


# ---------------------------------------------------------------- shrinkage
def test_shrinkage_pulls_small_samples_toward_the_prior():
    """A striker with one good half should not look like a 2.0 xG per 90 player."""
    barely_played = _shrink(2.0, 0.3, minutes=45)
    full_season = _shrink(2.0, 0.3, minutes=3000)
    assert barely_played < full_season
    assert barely_played < 0.6, "45 minutes should count for very little"
    assert full_season > 1.5, "a full season should mostly be trusted"


def test_shrinkage_with_no_minutes_returns_the_prior():
    assert _shrink(5.0, 0.25, minutes=0) == pytest.approx(0.25)


def test_shrinkage_is_bounded_by_value_and_prior():
    for minutes in (0, 100, 900, 5000):
        result = _shrink(1.0, 0.2, minutes)
        assert 0.2 <= result <= 1.0


# ----------------------------------------------------------- availability
@pytest.mark.parametrize("status,chance,expected", [
    ("a", None, 1.0),
    (None, None, 1.0),
    ("d", None, 0.75),
    ("d", 50, 0.5),
    ("i", None, 0.0),
    ("s", None, 0.0),
    ("u", None, 0.0),
    ("a", 25, 0.25),      # an explicit chance always wins
    ("i", 75, 0.75),
])
def test_availability_multiplier(status, chance, expected):
    value, _ = availability_multiplier(status, chance)
    assert value == pytest.approx(expected)


def test_flagged_players_get_an_explanation():
    _, note = availability_multiplier("i", None)
    assert "injured" in note


# --------------------------------------------------------------- minutes
def _profile(**kwargs) -> MinutesProfile:
    base = dict(
        element_id=1, p_start=0.9, p_bench=0.05, p_unused=0.05, p_60=0.85,
        expected_minutes=78.0, minutes_if_start=85.0, minutes_if_bench=15.0,
        matches_observed=30, availability=1.0, confidence="evidence",
    )
    base.update(kwargs)
    return MinutesProfile(**base)


def test_world_cup_haircut_only_applies_early():
    profile = _profile(wc_returnee=True)
    early = profile.for_event(1)
    later = profile.for_event(10)

    assert early.expected_minutes < profile.expected_minutes
    assert early.p_start < profile.p_start
    assert later.expected_minutes == profile.expected_minutes
    assert "World Cup" in early.note


def test_no_haircut_for_players_who_did_not_go_deep():
    profile = _profile(wc_returnee=False)
    assert profile.for_event(1).expected_minutes == profile.expected_minutes


# ----------------------------------------------------------- team ratings
def _ratings() -> TeamRatings:
    return TeamRatings(
        season_fitted="test",
        attack={"Strong": 1.5, "Weak": 0.6, "Average": 1.0},
        defence={"Strong": 0.6, "Weak": 1.5, "Average": 1.0},
        mu_home=1.5, mu_away=1.2,
    )


def test_home_advantage_raises_expected_goals():
    ratings = _ratings()
    home = ratings.expected_goals("Average", "Average", is_home=True)
    away = ratings.expected_goals("Average", "Average", is_home=False)
    assert home > away
    assert home == pytest.approx(1.5)
    assert away == pytest.approx(1.2)


def test_a_strong_attack_against_a_weak_defence_scores_most():
    ratings = _ratings()
    best = ratings.expected_goals("Strong", "Weak", is_home=True)
    worst = ratings.expected_goals("Weak", "Strong", is_home=False)
    assert best > worst
    assert best == pytest.approx(1.5 * 1.5 * 1.5)


def test_conceded_is_the_opponents_expected_goals_reversed():
    ratings = _ratings()
    conceded = ratings.expected_conceded("Strong", "Weak", is_home=True)
    opponent_scores = ratings.expected_goals("Weak", "Strong", is_home=False)
    assert conceded == pytest.approx(opponent_scores)


def test_difficulty_stays_on_the_one_to_five_scale():
    ratings = _ratings()
    pairs = [("Strong", "Weak"), ("Weak", "Strong"), ("Average", "Average")]
    for team, opponent in pairs:
        for is_home in (True, False):
            assert 1.0 <= ratings.difficulty(team, opponent, is_home) <= 5.0


def test_facing_a_stronger_side_is_rated_harder():
    ratings = _ratings()
    easy = ratings.difficulty("Average", "Weak", is_home=True)
    hard = ratings.difficulty("Average", "Strong", is_home=False)
    assert hard > easy


def test_promoted_teams_are_marked_as_estimated():
    ratings = _ratings()
    assert not ratings.is_estimated("Strong")
    ratings.promoted.add("Newbie")
    assert ratings.is_estimated("Newbie")
