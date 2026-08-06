"""Expected points, by component, per player per gameweek.

Two passes over the same model:

*   An analytic expectation, exact given the model's assumptions. The optimiser uses
    this, because comparing two transfers on Monte Carlo means is comparing their
    sampling noise as much as their merit.
*   A Monte Carlo pass for the shape — P(haul), P(blank), and the p10/p90 spread,
    which is what captaincy and differential decisions actually turn on.

Every component is separable and reported separately. A number this tool cannot
break down is a number the spec says will not be acted on.

Honest statement of what is weak, in order:

1.  **Bonus is a placeholder.** The BPS weightings are not in the API and cannot be
    fitted from last season because the system was reworked (FINDINGS.md caveat 2).
    Bonus here is a shrunk historical rate, which will be wrong in ways nobody can
    quantify yet. It is deliberately the smallest component and is flagged everywhere.
2.  **DEFCON uses Poisson counts**, which understates variance for a threshold
    statistic. Where a player has enough match history the Poisson probability is
    blended with their observed hit rate, which carries the overdispersion for free.
3.  **No 2026/27 evidence exists.** Every rate is last season's, so a player in a new
    role is mispriced until matches are played.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from ..db import Database
from ..scoring import ScoringConstants, ScoringRules
from .minutes import MinutesProfile
from .team_ratings import TeamRatings

# Shrinkage: a rate is trusted in proportion to the minutes behind it. At
# SHRINK_MINUTES the estimate sits halfway between the player and the positional mean.
SHRINK_MINUTES = 900.0
DEFCON_EMPIRICAL_WEIGHT = 0.5      # blended with Poisson where history allows
DEFCON_MIN_MATCHES = 10


@dataclass
class Projection:
    element_id: int
    event: int
    fixture_count: int

    xp_mean: float = 0.0
    xp_p10: float = 0.0
    xp_median: float = 0.0
    xp_p90: float = 0.0
    p_haul_10plus: float = 0.0
    p_blank_2minus: float = 0.0

    c_minutes: float = 0.0
    c_goals: float = 0.0
    c_assists: float = 0.0
    c_clean_sheet: float = 0.0
    c_goals_conceded: float = 0.0
    c_defcon: float = 0.0
    c_bonus: float = 0.0
    c_saves: float = 0.0
    c_negatives: float = 0.0

    p_defcon_hit: float = 0.0
    p_clean_sheet: float = 0.0
    expected_minutes: float = 0.0
    p_60: float = 0.0
    opponent: str = ""
    is_home: bool = True
    difficulty: float = 3.0
    confidence: str = "evidence"
    notes: list[str] = field(default_factory=list)

    def components(self) -> dict[str, float]:
        return {
            "minutes": self.c_minutes, "goals": self.c_goals, "assists": self.c_assists,
            "clean sheet": self.c_clean_sheet, "goals conceded": self.c_goals_conceded,
            "defensive contribution": self.c_defcon, "bonus": self.c_bonus,
            "saves": self.c_saves, "cards": self.c_negatives,
        }


@dataclass
class PlayerRates:
    """Per-90 rates, already shrunk toward the positional mean."""

    element_id: int
    position: str
    element_type: int
    team: str
    xg90: float = 0.0
    xa90: float = 0.0
    defcon90: float = 0.0
    saves90: float = 0.0
    bonus90: float = 0.0
    yellow90: float = 0.0
    red90: float = 0.0
    defcon_hit_rate: float | None = None    # observed share of 60+ matches clearing the bar
    defcon_matches: int = 0
    minutes_history: int = 0


def _shrink(value: float, prior: float, minutes: float) -> float:
    weight = minutes / (minutes + SHRINK_MINUTES)
    return weight * value + (1 - weight) * prior


def _poisson_tail(lam: float, threshold: int) -> float:
    """P(X >= threshold) for X ~ Poisson(lam)."""
    if threshold <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    cumulative = 0.0
    term = math.exp(-lam)
    for k in range(threshold):
        cumulative += term
        term *= lam / (k + 1)
    return max(0.0, 1.0 - cumulative)


def _expected_floor_div(lam: float, divisor: int, max_k: int = 30) -> float:
    """E[floor(X / divisor)] for X ~ Poisson(lam). Used for concessions and saves."""
    if lam <= 0:
        return 0.0
    total = 0.0
    term = math.exp(-lam)
    for k in range(max_k):
        total += term * (k // divisor)
        term *= lam / (k + 1)
    return total


def build_rates(db: Database, season: str = "2025-26") -> dict[int, PlayerRates]:
    """Per-90 rates for every current player, shrunk toward positional means.

    Attacking and DEFCON rates come from the current season-total fields; DEFCON is
    rebuilt from components under the player's *current* position, never read off the
    stored aggregate (FINDINGS.md caveat 3b).
    """
    players = db.query(
        """
        SELECT element_id, position, element_type, team_name AS team, minutes,
               expected_goals_per_90, expected_assists_per_90,
               tackles, clearances_blocks_interceptions, recoveries,
               saves, bonus, yellow_cards, red_cards
        FROM v_player
        """
    ).to_dicts()

    # Observed DEFCON hit rate per player, from matches they actually played 60+ in.
    hit_rows = db.query(
        """
        SELECT element_id,
               COUNT(*) AS matches,
               SUM(CASE WHEN defensive_contribution >= thr THEN 1 ELSE 0 END) AS hits
        FROM (
          SELECT h.element_id, h.minutes,
                 CASE WHEN i.element_type = 2
                      THEN h.tackles + h.clearances_blocks_interceptions
                      WHEN i.element_type = 1 THEN 0
                      ELSE h.tackles + h.clearances_blocks_interceptions + h.recoveries
                 END AS defensive_contribution,
                 CASE WHEN i.element_type = 2 THEN 10
                      WHEN i.element_type = 1 THEN 9999 ELSE 12 END AS thr
          FROM hist_player_gw h
          JOIN latest_players_identity i USING (element_id)
          WHERE h.season = ? AND h.element_id IS NOT NULL AND h.minutes >= 60
        )
        GROUP BY element_id
        """,
        [season],
    ).to_dicts()
    hit_by_player = {r["element_id"]: r for r in hit_rows}

    # Positional means, minutes-weighted, used as the shrinkage target.
    def positional_mean(field: str) -> dict[str, float]:
        rows = db.query(
            f"""
            SELECT position,
                   SUM(COALESCE({field}, 0) * minutes) / NULLIF(SUM(minutes), 0) AS mean
            FROM v_player WHERE minutes > 300 GROUP BY position
            """
        ).to_dicts()
        return {r["position"]: float(r["mean"] or 0.0) for r in rows}

    xg_mean = positional_mean("expected_goals_per_90")
    xa_mean = positional_mean("expected_assists_per_90")

    rates: dict[int, PlayerRates] = {}
    for player in players:
        minutes = float(player["minutes"] or 0)
        position = player["position"]
        per90 = (minutes / 90.0) or 1.0

        if player["element_type"] == 1:
            defcon_actions = 0
        elif player["element_type"] == 2:
            defcon_actions = (player["tackles"] or 0) + (player["clearances_blocks_interceptions"] or 0)
        else:
            defcon_actions = ((player["tackles"] or 0)
                              + (player["clearances_blocks_interceptions"] or 0)
                              + (player["recoveries"] or 0))

        hits = hit_by_player.get(player["element_id"])
        hit_rate = None
        matches = 0
        if hits and hits["matches"] >= DEFCON_MIN_MATCHES:
            matches = int(hits["matches"])
            hit_rate = float(hits["hits"]) / matches

        rates[player["element_id"]] = PlayerRates(
            element_id=player["element_id"],
            position=position,
            element_type=player["element_type"],
            team=player["team"],
            xg90=_shrink(float(player["expected_goals_per_90"] or 0.0),
                         xg_mean.get(position, 0.05), minutes),
            xa90=_shrink(float(player["expected_assists_per_90"] or 0.0),
                         xa_mean.get(position, 0.05), minutes),
            defcon90=(defcon_actions / per90) if minutes > 0 else 0.0,
            saves90=((player["saves"] or 0) / per90) if minutes > 0 else 0.0,
            bonus90=((player["bonus"] or 0) / per90) if minutes > 0 else 0.0,
            yellow90=((player["yellow_cards"] or 0) / per90) if minutes > 0 else 0.0,
            red90=((player["red_cards"] or 0) / per90) if minutes > 0 else 0.0,
            defcon_hit_rate=hit_rate,
            defcon_matches=matches,
            minutes_history=int(minutes),
        )
    return rates


class Projector:
    def __init__(
        self,
        rules: ScoringRules,
        ratings: TeamRatings,
        rates: dict[int, PlayerRates],
        minutes: dict[int, MinutesProfile],
        rng: np.random.Generator | None = None,
    ) -> None:
        self.rules = rules
        self.ratings = ratings
        self.rates = rates
        self.minutes = minutes
        self.rng = rng or np.random.default_rng(20262027)
        self.constants: ScoringConstants = rules.constants

        # A team's baseline goals per match, used to turn a fixture into a multiplier.
        self._baseline = {
            team: max(0.05, attack * (ratings.mu_home + ratings.mu_away) / 2.0)
            for team, attack in ratings.attack.items()
        }

    def attack_multiplier(self, team: str, opponent: str, is_home: bool) -> float:
        expected = self.ratings.expected_goals(team, opponent, is_home)
        return expected / self._baseline.get(team, 1.0)

    def project(
        self,
        element_id: int,
        event: int,
        fixtures: Sequence[tuple[str, bool]],
        simulate: int = 0,
    ) -> Projection:
        """Project one player for one gameweek.

        `fixtures` is a list of (opponent, is_home) — usually one, two in a double
        gameweek, empty in a blank.
        """
        rates = self.rates.get(element_id)
        profile = self.minutes.get(element_id)
        projection = Projection(element_id=element_id, event=event, fixture_count=len(fixtures))
        if rates is None or profile is None or not fixtures:
            projection.notes.append("blank gameweek" if rates else "no data for this player")
            return projection

        profile = profile.for_event(event)
        position = rates.position
        projection.expected_minutes = profile.expected_minutes * len(fixtures)
        projection.p_60 = profile.p_60
        projection.confidence = profile.confidence
        if profile.note:
            projection.notes.append(profile.note)

        opponent, is_home = fixtures[0]
        projection.opponent = opponent
        projection.is_home = is_home
        projection.difficulty = self.ratings.difficulty(rates.team, opponent, is_home)
        if self.ratings.is_estimated(opponent) or self.ratings.is_estimated(rates.team):
            projection.notes.append(
                "a promoted side is involved: its rating is a prior, not fitted."
            )

        totals = {k: 0.0 for k in (
            "minutes", "goals", "assists", "clean_sheet", "goals_conceded",
            "defcon", "bonus", "saves", "negatives")}
        cs_probabilities: list[float] = []
        defcon_probabilities: list[float] = []

        for opponent, is_home in fixtures:
            attack_mult = self.attack_multiplier(rates.team, opponent, is_home)
            lambda_conceded = self.ratings.expected_conceded(rates.team, opponent, is_home)

            minutes_share = profile.expected_minutes / 90.0

            # Appearance
            totals["minutes"] += (
                self.rules.long_play * profile.p_60
                + self.rules.short_play * max(0.0, profile.p_start + profile.p_bench - profile.p_60)
            )

            # Attacking returns
            expected_goals = rates.xg90 * minutes_share * attack_mult
            expected_assists = rates.xa90 * minutes_share * attack_mult
            totals["goals"] += expected_goals * self.rules.goals_scored.get(position, 0)
            totals["assists"] += expected_assists * self.rules.assists

            # Clean sheet, conditioned on reaching 60 minutes
            p_clean = math.exp(-lambda_conceded)
            cs_probabilities.append(p_clean * profile.p_60)
            totals["clean_sheet"] += profile.p_60 * p_clean * self.rules.clean_sheets.get(position, 0)

            # Concession penalty: integrate the distribution, do not use the mean
            conceded_rate = self.rules.goals_conceded.get(position, 0)
            if conceded_rate:
                effective = lambda_conceded * minutes_share
                totals["goals_conceded"] += (
                    conceded_rate
                    * _expected_floor_div(effective, self.constants.goals_conceded_per_penalty)
                    * (profile.p_start + profile.p_bench)
                )

            # Defensive contribution: a threshold, not a rate
            threshold = self.constants.defcon_threshold(position)
            if threshold is not None:
                p_hit = self._defcon_probability(rates, profile, threshold)
                defcon_probabilities.append(p_hit)
                totals["defcon"] += p_hit * self.rules.defensive_contribution.get(position, 0)

            # Saves
            if position == "GKP":
                shot_mult = lambda_conceded / max(0.05, (self.ratings.mu_home + self.ratings.mu_away) / 2)
                expected_saves = rates.saves90 * minutes_share * shot_mult
                totals["saves"] += self.rules.saves * _expected_floor_div(
                    expected_saves, self.constants.saves_per_point
                )

            # Bonus — provisional, see module docstring
            totals["bonus"] += rates.bonus90 * minutes_share

            # Cards
            totals["negatives"] += (
                rates.yellow90 * minutes_share * self.rules.yellow_cards
                + rates.red90 * minutes_share * self.rules.red_cards
            )

        projection.c_minutes = totals["minutes"]
        projection.c_goals = totals["goals"]
        projection.c_assists = totals["assists"]
        projection.c_clean_sheet = totals["clean_sheet"]
        projection.c_goals_conceded = totals["goals_conceded"]
        projection.c_defcon = totals["defcon"]
        projection.c_bonus = totals["bonus"]
        projection.c_saves = totals["saves"]
        projection.c_negatives = totals["negatives"]
        projection.xp_mean = sum(totals.values())
        projection.p_clean_sheet = max(cs_probabilities) if cs_probabilities else 0.0
        projection.p_defcon_hit = max(defcon_probabilities) if defcon_probabilities else 0.0

        if rates.minutes_history < 300:
            projection.notes.append("thin minutes history: rates lean on the positional average.")

        if simulate:
            self._simulate(projection, rates, profile, fixtures, simulate)

        return projection

    def _defcon_probability(
        self, rates: PlayerRates, profile: MinutesProfile, threshold: int
    ) -> float:
        """P(clearing the DEFCON threshold), blending Poisson with observed hit rate.

        Poisson alone understates the variance of a count statistic, which for a
        threshold matters in both directions. Where a player has enough matches, their
        own hit rate carries the overdispersion without having to model it.
        """
        poisson_start = _poisson_tail(rates.defcon90 * profile.minutes_if_start / 90.0, threshold)
        poisson_bench = _poisson_tail(rates.defcon90 * profile.minutes_if_bench / 90.0, threshold)
        modelled = profile.p_start * poisson_start + profile.p_bench * poisson_bench

        if rates.defcon_hit_rate is None:
            return modelled

        empirical = rates.defcon_hit_rate * (profile.p_start + profile.p_bench)
        return (DEFCON_EMPIRICAL_WEIGHT * empirical
                + (1 - DEFCON_EMPIRICAL_WEIGHT) * modelled)

    def _simulate(
        self,
        projection: Projection,
        rates: PlayerRates,
        profile: MinutesProfile,
        fixtures: Sequence[tuple[str, bool]],
        draws: int,
    ) -> None:
        """Monte Carlo for the shape of the distribution, not its centre."""
        rng = self.rng
        position = rates.position
        points = np.zeros(draws)

        for opponent, is_home in fixtures:
            attack_mult = self.attack_multiplier(rates.team, opponent, is_home)
            lambda_conceded = self.ratings.expected_conceded(rates.team, opponent, is_home)

            # Minutes: start, bench appearance, or unused
            roll = rng.random(draws)
            minutes = np.where(
                roll < profile.p_start, profile.minutes_if_start,
                np.where(roll < profile.p_start + profile.p_bench, profile.minutes_if_bench, 0.0),
            )
            played = minutes > 0
            share = minutes / 90.0

            points += np.where(minutes >= 60, self.rules.long_play,
                               np.where(played, self.rules.short_play, 0))

            goals = rng.poisson(np.maximum(rates.xg90 * share * attack_mult, 0))
            assists = rng.poisson(np.maximum(rates.xa90 * share * attack_mult, 0))
            points += goals * self.rules.goals_scored.get(position, 0)
            points += assists * self.rules.assists

            conceded = rng.poisson(np.maximum(lambda_conceded * share, 0))
            clean = (conceded == 0) & (minutes >= 60)
            points += clean * self.rules.clean_sheets.get(position, 0)
            conceded_rate = self.rules.goals_conceded.get(position, 0)
            if conceded_rate:
                points += (conceded // self.constants.goals_conceded_per_penalty) * conceded_rate

            threshold = self.constants.defcon_threshold(position)
            if threshold is not None:
                actions = rng.poisson(np.maximum(rates.defcon90 * share, 0))
                points += (actions >= threshold) * self.rules.defensive_contribution.get(position, 0)

            if position == "GKP":
                shot_mult = lambda_conceded / max(0.05, (self.ratings.mu_home + self.ratings.mu_away) / 2)
                saves = rng.poisson(np.maximum(rates.saves90 * share * shot_mult, 0))
                points += (saves // self.constants.saves_per_point) * self.rules.saves

            # Bonus drawn around its provisional expectation
            expected_bonus = rates.bonus90 * share
            points += rng.poisson(np.maximum(expected_bonus, 0)) * played

            points += rng.binomial(1, np.clip(rates.yellow90 * share, 0, 1)) * self.rules.yellow_cards

        projection.xp_p10 = float(np.percentile(points, 10))
        projection.xp_median = float(np.percentile(points, 50))
        projection.xp_p90 = float(np.percentile(points, 90))
        projection.p_haul_10plus = float((points >= 10).mean())
        projection.p_blank_2minus = float((points <= 2).mean())


def fixtures_by_event(db: Database, horizon_start: int, horizon: int) -> dict[tuple[str, int], list[tuple[str, bool]]]:
    """(team, event) -> list of (opponent, is_home). Handles blanks and doubles."""
    rows = db.query(
        """
        SELECT t.name AS team, f.event, o.name AS opponent, f.is_home
        FROM v_team_fixtures f
        JOIN latest_teams t ON t.team_id = f.team_id
        JOIN latest_teams o ON o.team_id = f.opponent_id
        WHERE f.event BETWEEN ? AND ?
        """,
        [horizon_start, horizon_start + horizon - 1],
    ).to_dicts()

    result: dict[tuple[str, int], list[tuple[str, bool]]] = {}
    for row in rows:
        result.setdefault((row["team"], row["event"]), []).append((row["opponent"], row["is_home"]))
    return result
