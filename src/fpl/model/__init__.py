"""Model assembly: ratings + minutes + rates -> projections over a horizon."""

from __future__ import annotations

import csv
import gzip
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..db import Database
from ..scoring import ScoringRules
from .minutes import MinutesProfile, build_profiles
from .projections import Projection, Projector, build_rates, fixtures_by_event
from .team_ratings import TeamRatings, fit_for_current_season

WORLD_CUP_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/"
    "data/world_cup_2026.csv"
)
# A nation still playing in these rounds went deep, so its players returned late.
WC_DEEP_ROUNDS = ("round5", "round6", "round7")


def _name_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum() or c == " ").strip()


def world_cup_returnees(db: Database, cache_dir: Path) -> tuple[set[int], list[str]]:
    """Current players whose nation ran deep at World Cup 2026.

    The spec asks for elevated opening-gameweek minutes risk to be flagged. Matching
    is by name and will not be complete; the match count is returned so the UI can
    say how confident this is rather than implying full coverage.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / "world_cup_2026.csv.gz"
    if not cached.exists():
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(WORLD_CUP_URL)
                response.raise_for_status()
                with gzip.open(cached, "wb") as fh:
                    fh.write(response.content)
        except httpx.HTTPError:
            return set(), ["world cup data unavailable; late-returnee flag not applied"]

    with gzip.open(cached, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    def played_deep(row: dict) -> bool:
        for key in WC_DEEP_ROUNDS:
            value = row.get(key)
            if value not in (None, "", "0"):
                return True
        return False

    deep_squads = {row["squad"] for row in rows if played_deep(row)}
    deep_players = {_name_key(row["name"]) for row in rows
                    if row.get("squad") in deep_squads and played_deep(row)}

    index = db.query(
        "SELECT element_id, first_name, second_name, web_name FROM latest_players_identity"
    ).to_dicts()

    matched: set[int] = set()
    for player in index:
        keys = {_name_key(f"{player['first_name']} {player['second_name']}"),
                _name_key(player["web_name"])}
        if keys & deep_players:
            matched.add(player["element_id"])

    notes = [
        f"world cup: {len(deep_squads)} nations ran deep; "
        f"{len(matched)} current players matched by name"
    ]
    return matched, notes


@dataclass
class Model:
    projector: Projector
    ratings: TeamRatings
    minutes: dict[int, MinutesProfile]
    fixtures: dict[tuple[str, int], list[tuple[str, bool]]]
    horizon_start: int
    horizon: int
    notes: list[str]

    def project_player(self, element_id: int, event: int, simulate: int = 0) -> Projection:
        team = self.projector.rates[element_id].team if element_id in self.projector.rates else None
        fixtures = self.fixtures.get((team, event), []) if team else []
        return self.projector.project(element_id, event, fixtures, simulate=simulate)

    def project_horizon(self, element_id: int, simulate: int = 0) -> list[Projection]:
        return [
            self.project_player(element_id, event, simulate=simulate)
            for event in range(self.horizon_start, self.horizon_start + self.horizon)
        ]

    def horizon_xp(self, element_id: int, decay: float = 1.0) -> float:
        """Decayed sum of expected points across the horizon."""
        total = 0.0
        for index, projection in enumerate(self.project_horizon(element_id)):
            total += (decay**index) * projection.xp_mean
        return total


def build_model(
    db: Database,
    horizon: int = 6,
    horizon_start: int | None = None,
    season: str = "2025-26",
    cache_dir: Path | None = None,
) -> Model:
    if horizon_start is None:
        horizon_start = int(
            db.scalar(
                "SELECT MIN(event_id) FROM latest_events WHERE deadline_time > now()"
            ) or 1
        )

    notes: list[str] = []
    rules = ScoringRules.from_db(db)
    ratings = fit_for_current_season(db, season)
    if ratings.promoted:
        notes.append(
            f"promoted sides use a prior, not fitted ratings: {', '.join(sorted(ratings.promoted))}"
        )

    returnees: set[int] = set()
    if cache_dir is not None:
        returnees, wc_notes = world_cup_returnees(db, cache_dir)
        notes += wc_notes

    rates = build_rates(db, season)
    minutes = build_profiles(db, season, wc_returnees=returnees)
    fixtures = fixtures_by_event(db, horizon_start, horizon)

    notes.append(
        "bonus is provisional: BPS weightings are absent from the API and last "
        "season's are mis-calibrated for the reworked system"
    )
    notes.append(f"all rates fitted on {season}; no 2026/27 matches have been played")

    return Model(
        projector=Projector(rules, ratings, rates, minutes),
        ratings=ratings, minutes=minutes, fixtures=fixtures,
        horizon_start=horizon_start, horizon=horizon, notes=notes,
    )


__all__ = [
    "Model", "Projection", "Projector", "TeamRatings", "MinutesProfile",
    "build_model", "build_rates", "build_profiles", "fit_for_current_season",
    "world_cup_returnees",
]
