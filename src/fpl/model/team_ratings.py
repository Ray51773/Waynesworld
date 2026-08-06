"""Team attack and defence ratings, fitted from last season's results.

This exists because the API's own `strength_attack_*` and `strength_defence_*` fields
are all zero pre-season (FINDINGS.md caveat 1), so the prior the spec suggested is
not available. Fitting is the primary path, not an upgrade.

Model: independent Poisson with multiplicative team effects and a home advantage,

    goals(home) ~ Poisson(mu_home * attack[home] * defence[away])
    goals(away) ~ Poisson(mu_away * attack[away] * defence[home])

fitted by iterative proportional fitting, which is the MLE for this parameterisation
and needs no optimiser. Ratings are centred so 1.0 is league average: attack above 1
scores more than average, defence above 1 concedes more than average.

Judgement call, flagged: the target blends expected goals with actual goals rather
than using either alone. xG is the more stable signal but understates teams that
finish well; 70/30 is a defensible compromise, not a fitted constant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db import Database

XG_WEIGHT = 0.70          # judgement call, see module docstring
ITERATIONS = 50
PROMOTED_ATTACK_PRIOR = 0.82   # typical promoted side, see promoted_prior()
PROMOTED_DEFENCE_PRIOR = 1.18


@dataclass
class TeamRatings:
    season_fitted: str
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    mu_home: float = 1.5
    mu_away: float = 1.2
    promoted: set[str] = field(default_factory=set)

    def expected_goals(self, team: str, opponent: str, is_home: bool) -> float:
        """Goals this team is expected to score in this fixture."""
        base = self.mu_home if is_home else self.mu_away
        return base * self.attack.get(team, 1.0) * self.defence.get(opponent, 1.0)

    def expected_conceded(self, team: str, opponent: str, is_home: bool) -> float:
        return self.expected_goals(opponent, team, not is_home)

    def difficulty(self, team: str, opponent: str, is_home: bool) -> float:
        """Model-derived fixture difficulty on the FPL 1-5 scale, for comparison
        with the official FDR. Driven by what the team is expected to concede
        and to score, so it is directional rather than a generic strength number."""
        conceded = self.expected_conceded(team, opponent, is_home)
        scored = self.expected_goals(team, opponent, is_home)
        raw = conceded - 0.5 * scored
        # Map roughly [-0.6, 1.6] onto [1, 5].
        scaled = 1.0 + (raw + 0.6) * (4.0 / 2.2)
        return max(1.0, min(5.0, scaled))

    def is_estimated(self, team: str) -> bool:
        return team in self.promoted


def fit(db: Database, season: str = "2025-26") -> TeamRatings:
    rows = db.query(
        """
        SELECT team, opponent, was_home,
               CAST(goals_for AS DOUBLE) AS goals_for,
               CAST(goals_against AS DOUBLE) AS goals_against,
               CAST(xg_for AS DOUBLE) AS xg_for,
               CAST(xg_against AS DOUBLE) AS xg_against
        FROM hist_team_match
        WHERE season = ? AND goals_for IS NOT NULL
        """,
        [season],
    ).to_dicts()
    if not rows:
        raise RuntimeError(f"no team match data for {season}; run `fpl import-history` first")

    for row in rows:
        row["target_for"] = XG_WEIGHT * (row["xg_for"] or 0.0) + (1 - XG_WEIGHT) * row["goals_for"]

    teams = sorted({row["team"] for row in rows})
    home_rows = [r for r in rows if r["was_home"]]
    away_rows = [r for r in rows if not r["was_home"]]

    mu_home = sum(r["target_for"] for r in home_rows) / max(len(home_rows), 1)
    mu_away = sum(r["target_for"] for r in away_rows) / max(len(away_rows), 1)

    attack = {t: 1.0 for t in teams}
    defence = {t: 1.0 for t in teams}

    scored: dict[str, float] = {t: 0.0 for t in teams}
    conceded: dict[str, float] = {t: 0.0 for t in teams}
    for row in rows:
        scored[row["team"]] += row["target_for"]
        conceded[row["opponent"]] += row["target_for"]

    for _ in range(ITERATIONS):
        for team in teams:
            expected = sum(
                (mu_home if r["was_home"] else mu_away) * defence[r["opponent"]]
                for r in rows if r["team"] == team
            )
            if expected > 0:
                attack[team] = scored[team] / expected

        for team in teams:
            expected = sum(
                (mu_home if r["was_home"] else mu_away) * attack[r["team"]]
                for r in rows if r["opponent"] == team
            )
            if expected > 0:
                defence[team] = conceded[team] / expected

        # Re-centre so the ratings stay interpretable as "relative to average".
        mean_attack = sum(attack.values()) / len(attack)
        mean_defence = sum(defence.values()) / len(defence)
        attack = {t: v / mean_attack for t, v in attack.items()}
        defence = {t: v / mean_defence for t, v in defence.items()}

    return TeamRatings(
        season_fitted=season, attack=attack, defence=defence,
        mu_home=mu_home, mu_away=mu_away,
    )


def promoted_prior(ratings: TeamRatings, promoted_teams: list[str]) -> TeamRatings:
    """Give newly promoted teams a prior, since they have no top-flight history.

    Uses fixed priors rather than last season's bottom three, because who finished
    bottom is noisy year to year while "a promoted side is roughly this much worse
    than average" is stable. Marked as estimated so the UI can say so.
    """
    for team in promoted_teams:
        ratings.attack[team] = PROMOTED_ATTACK_PRIOR
        ratings.defence[team] = PROMOTED_DEFENCE_PRIOR
        ratings.promoted.add(team)
    return ratings


def fit_for_current_season(db: Database, season: str = "2025-26") -> TeamRatings:
    """Fit on last season, then fill in this season's promoted sides."""
    ratings = fit(db, season)
    current = [r["name"] for r in db.query("SELECT name FROM latest_teams").to_dicts()]
    promoted = [t for t in current if t not in ratings.attack]
    return promoted_prior(ratings, promoted)
