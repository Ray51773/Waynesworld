"""Cold-start history from the vaastav/Fantasy-Premier-League dataset.

Until 2026/27 matches are played, this is the only source of gameweek-level data,
and every model below it is fitted here. Two jobs:

1.  Load per-match player rows for a past season.
2.  Link those rows to the current season's `element_id` by name, so a player's
    history can be looked up from their current listing. Match rate is reported,
    never silently swallowed — an unmatched player is one the model is blind to.

Team-level match results are derived from the same rows, because the API's own
team strength ratings are all zero pre-season (FINDINGS.md caveat 1).
"""

from __future__ import annotations

import csv
import gzip
import io
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

from .db import Database

RAW_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"

# Columns we need; the file has 46 and the rest are not used by any model.
WANTED = [
    "name", "position", "team", "GW", "fixture", "opponent_team", "was_home",
    "kickoff_time", "team_h_score", "team_a_score", "minutes", "starts",
    "goals_scored", "assists", "clean_sheets", "goals_conceded", "own_goals",
    "penalties_saved", "penalties_missed", "yellow_cards", "red_cards", "saves",
    "bonus", "bps", "tackles", "clearances_blocks_interceptions", "recoveries",
    "defensive_contribution", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded", "total_points",
    "value", "selected",
]


def name_key(name: str) -> str:
    """Normalise a name for matching: strip accents, punctuation and case.

    Deliberately blunt. Anything cleverer risks matching two different players,
    which is worse than leaving one unmatched and saying so.
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum() or c == " ").strip()


@dataclass
class ImportReport:
    season: str
    rows: int = 0
    matched: int = 0
    unmatched_names: list[str] = None
    team_matches: int = 0

    @property
    def match_rate(self) -> float:
        return self.matched / self.rows if self.rows else 0.0


def _to_int(value: str | None, default: int | None = 0) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _to_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def download_season(season: str, cache_dir: Path) -> list[dict]:
    """Fetch merged_gw.csv for a season, caching the raw file on disk."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"merged_gw_{season}.csv.gz"

    if not cached.exists():
        url = f"{RAW_BASE}/{season}/gws/merged_gw.csv"
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            with gzip.open(cached, "wb", compresslevel=6) as fh:
                fh.write(response.content)

    with gzip.open(cached, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def build_name_index(db: Database) -> dict[str, int]:
    """Map normalised current-season names to element_id.

    Indexes both "first second" and web_name, since the historical files use full
    names and the API's web_name is sometimes the only thing that matches.
    """
    rows = db.query(
        "SELECT element_id, first_name, second_name, web_name, known_name "
        "FROM latest_players_identity"
    ).to_dicts()

    index: dict[str, int] = {}
    for row in rows:
        candidates = [
            f"{row['first_name']} {row['second_name']}",
            row["web_name"],
            row.get("known_name") or "",
        ]
        for candidate in candidates:
            key = name_key(candidate)
            if key and key not in index:
                index[key] = row["element_id"]
    return index


def import_season(db: Database, season: str, cache_dir: Path) -> ImportReport:
    """Load one past season's per-match rows and link them to current players."""
    raw = download_season(season, cache_dir)
    index = build_name_index(db)

    report = ImportReport(season=season, unmatched_names=[])
    unmatched: set[str] = set()
    rows: list[dict] = []

    for record in raw:
        key = name_key(record.get("name", ""))
        element_id = index.get(key)
        if element_id is None:
            unmatched.add(record.get("name", ""))
        else:
            report.matched += 1

        rows.append({
            "season": season,
            "name": record.get("name"),
            "name_key": key,
            "element_id": element_id,
            "position": record.get("position"),
            "team": record.get("team"),
            "event": _to_int(record.get("GW")),
            "fixture_id": _to_int(record.get("fixture")),
            "opponent_team": _to_int(record.get("opponent_team")),
            "was_home": str(record.get("was_home", "")).lower() == "true",
            "kickoff_time": record.get("kickoff_time") or None,
            "team_h_score": _to_int(record.get("team_h_score"), None),
            "team_a_score": _to_int(record.get("team_a_score"), None),
            "minutes": _to_int(record.get("minutes")),
            "starts": _to_int(record.get("starts")),
            "goals_scored": _to_int(record.get("goals_scored")),
            "assists": _to_int(record.get("assists")),
            "clean_sheets": _to_int(record.get("clean_sheets")),
            "goals_conceded": _to_int(record.get("goals_conceded")),
            "own_goals": _to_int(record.get("own_goals")),
            "penalties_saved": _to_int(record.get("penalties_saved")),
            "penalties_missed": _to_int(record.get("penalties_missed")),
            "yellow_cards": _to_int(record.get("yellow_cards")),
            "red_cards": _to_int(record.get("red_cards")),
            "saves": _to_int(record.get("saves")),
            "bonus": _to_int(record.get("bonus")),
            "bps": _to_int(record.get("bps")),
            "tackles": _to_int(record.get("tackles")),
            "clearances_blocks_interceptions": _to_int(record.get("clearances_blocks_interceptions")),
            "recoveries": _to_int(record.get("recoveries")),
            "defensive_contribution": _to_int(record.get("defensive_contribution")),
            "expected_goals": _to_float(record.get("expected_goals")),
            "expected_assists": _to_float(record.get("expected_assists")),
            "expected_goal_involvements": _to_float(record.get("expected_goal_involvements")),
            "expected_goals_conceded": _to_float(record.get("expected_goals_conceded")),
            "total_points": _to_int(record.get("total_points")),
            "value": _to_int(record.get("value")),
            "selected": _to_int(record.get("selected")),
        })

    report.rows = len(rows)
    report.unmatched_names = sorted(unmatched)[:40]

    db.con.execute("DELETE FROM hist_player_gw WHERE season = ?", [season])
    db.append("hist_player_gw", rows)

    report.team_matches = build_team_matches(db, season)
    return report


def build_team_matches(db: Database, season: str) -> int:
    """Derive team-level results and xG from the per-player rows.

    Team xG is the sum of its players' xG in that fixture; xG against is the
    opponent's sum. Goals come from the recorded scoreline.
    """
    db.con.execute("DELETE FROM hist_team_match WHERE season = ?", [season])
    db.con.execute(
        """
        INSERT INTO hist_team_match
        WITH sides AS (
          SELECT season, fixture_id, event, team, was_home,
                 MAX(team_h_score) AS team_h_score,
                 MAX(team_a_score) AS team_a_score,
                 SUM(expected_goals) AS xg_for
          FROM hist_player_gw
          WHERE season = ? AND fixture_id IS NOT NULL
          GROUP BY season, fixture_id, event, team, was_home
        )
        SELECT s.season, s.fixture_id, s.event,
               s.team,
               o.team AS opponent,
               s.was_home,
               CASE WHEN s.was_home THEN s.team_h_score ELSE s.team_a_score END AS goals_for,
               CASE WHEN s.was_home THEN s.team_a_score ELSE s.team_h_score END AS goals_against,
               s.xg_for,
               o.xg_for AS xg_against
        FROM sides s
        JOIN sides o ON o.season = s.season AND o.fixture_id = s.fixture_id
                    AND o.was_home <> s.was_home
        """,
        [season],
    )
    return int(db.scalar("SELECT COUNT(*) FROM hist_team_match WHERE season = ?", [season]) or 0)
