"""FPL scoring engine.

Reimplements match scoring from the rules table rather than from hardcoded numbers,
so that when the game changes the engine follows. Everything the API does supply is
read from it; the handful of constants it does not supply are declared explicitly in
`ScoringConstants` with their source, rather than being scattered through the code.

The engine is season-agnostic: pass 2026/27 rules to project, pass 2025/26 rules to
verify against last season's results. That separation is what makes the verification
meaningful — the same code path is exercised either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

POSITIONS = ("GKP", "DEF", "MID", "FWD")

# The historical CSVs say GK where the API says GKP.
_POSITION_ALIASES = {"GK": "GKP", "GKP": "GKP", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}


def normalise_position(position: str | int) -> str:
    if isinstance(position, int):
        return POSITIONS[position - 1]
    key = str(position).strip().upper()
    if key not in _POSITION_ALIASES:
        raise ValueError(f"unrecognised position: {position!r}")
    return _POSITION_ALIASES[key]


@dataclass(frozen=True)
class ScoringConstants:
    """Rules the API does not publish. See FINDINGS.md caveats 2 and 3.

    These are seeded from the published game rules. They are kept together and
    labelled so that nothing downstream can mistake them for API-derived values.
    """

    appearance_minutes: int = 60          # step from short_play to long_play
    saves_per_point: int = 3
    goals_conceded_per_penalty: int = 2   # -1 per 2 conceded, DEF and GKP only
    defcon_threshold_def: int = 10        # tackles + CBI
    defcon_threshold_mid_fwd: int = 12    # tackles + CBI + recoveries
    source: str = "official_rules_2026_27"

    def defcon_threshold(self, position: str) -> int | None:
        if position == "DEF":
            return self.defcon_threshold_def
        if position in ("MID", "FWD"):
            return self.defcon_threshold_mid_fwd
        return None                        # goalkeepers do not score DEFCON


@dataclass(frozen=True)
class ScoringRules:
    """Point values, keyed by position where the game varies them."""

    long_play: int
    short_play: int
    goals_scored: Mapping[str, int]
    assists: int
    clean_sheets: Mapping[str, int]
    goals_conceded: Mapping[str, int]
    saves: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int
    defensive_contribution: Mapping[str, int]
    constants: ScoringConstants = field(default_factory=ScoringConstants)
    season: str = "2026/27"
    source: str = "api"

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping], season: str = "2026/27",
                  constants: ScoringConstants | None = None) -> "ScoringRules":
        """Build from long-form (rule_name, position, points) rows."""
        scalars: dict[str, int] = {}
        maps: dict[str, dict[str, int]] = {}
        for row in rows:
            name, position, points = row["rule_name"], row["position"], int(row["points"])
            if position == "ALL":
                scalars[name] = points
            else:
                maps.setdefault(name, {})[position] = points

        def as_map(name: str) -> dict[str, int]:
            found = maps.get(name, {})
            return {pos: int(found.get(pos, 0)) for pos in POSITIONS}

        return cls(
            long_play=scalars.get("long_play", 2),
            short_play=scalars.get("short_play", 1),
            goals_scored=as_map("goals_scored"),
            assists=scalars.get("assists", 3),
            clean_sheets=as_map("clean_sheets"),
            goals_conceded=as_map("goals_conceded"),
            saves=scalars.get("saves", 1),
            penalties_saved=scalars.get("penalties_saved", 5),
            penalties_missed=scalars.get("penalties_missed", -2),
            yellow_cards=scalars.get("yellow_cards", -1),
            red_cards=scalars.get("red_cards", -3),
            own_goals=scalars.get("own_goals", -2),
            defensive_contribution=as_map("defensive_contribution"),
            constants=constants or ScoringConstants(),
            season=season,
        )

    @classmethod
    def from_db(cls, db, season: str = "2026/27") -> "ScoringRules":
        rows = db.query(
            "SELECT rule_name, position, points FROM latest_scoring_rules"
        ).to_dicts()
        if not rows:
            raise RuntimeError("no scoring rules in the store - run `fpl refresh` first")
        return cls.from_rows(rows, season=season)

    def for_season_2025_26(self) -> "ScoringRules":
        """The 2025/26 ruleset, used to verify the engine against last season.

        Identical to 2026/27 except that goalkeeper goals were worth 6, not 10.
        Deriving it from the current rules rather than restating the whole table
        keeps the two in step if anything else about the engine changes.
        """
        goals = dict(self.goals_scored)
        goals["GKP"] = 6
        return replace(
            self,
            goals_scored=goals,
            season="2025/26",
            source="derived_from_api_plus_known_2025_26_delta",
        )


@dataclass(frozen=True)
class MatchStats:
    """One player's raw statistics for one match."""

    minutes: int = 0
    goals_scored: int = 0
    assists: int = 0
    goals_conceded: int = 0
    own_goals: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    bonus: int = 0
    tackles: int = 0
    clearances_blocks_interceptions: int = 0
    recoveries: int = 0
    clean_sheets: int | None = None   # if None, derived from minutes and goals conceded

    @classmethod
    def from_mapping(cls, row: Mapping) -> "MatchStats":
        def get(key: str, default: int = 0) -> int:
            value = row.get(key, default)
            if value in (None, ""):
                return default
            return int(float(value))

        return cls(
            minutes=get("minutes"), goals_scored=get("goals_scored"),
            assists=get("assists"), goals_conceded=get("goals_conceded"),
            own_goals=get("own_goals"), penalties_saved=get("penalties_saved"),
            penalties_missed=get("penalties_missed"),
            yellow_cards=get("yellow_cards"), red_cards=get("red_cards"),
            saves=get("saves"), bonus=get("bonus"), tackles=get("tackles"),
            clearances_blocks_interceptions=get("clearances_blocks_interceptions"),
            recoveries=get("recoveries"),
            clean_sheets=get("clean_sheets") if "clean_sheets" in row else None,
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    """Points by component. Mandatory output: a recommendation that cannot be
    explained by component is one the spec says will not be acted on."""

    appearance: int = 0
    goals: int = 0
    assists: int = 0
    clean_sheet: int = 0
    goals_conceded: int = 0
    saves: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    cards: int = 0
    own_goals: int = 0
    defensive_contribution: int = 0
    bonus: int = 0

    @property
    def total(self) -> int:
        return (
            self.appearance + self.goals + self.assists + self.clean_sheet
            + self.goals_conceded + self.saves + self.penalties_saved
            + self.penalties_missed + self.cards + self.own_goals
            + self.defensive_contribution + self.bonus
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "appearance": self.appearance, "goals": self.goals, "assists": self.assists,
            "clean_sheet": self.clean_sheet, "goals_conceded": self.goals_conceded,
            "saves": self.saves, "penalties_saved": self.penalties_saved,
            "penalties_missed": self.penalties_missed, "cards": self.cards,
            "own_goals": self.own_goals,
            "defensive_contribution": self.defensive_contribution,
            "bonus": self.bonus, "total": self.total,
        }


def defensive_actions(position: str, tackles: int, cbi: int, recoveries: int) -> int:
    """Countable defensive actions under the given position's rule.

    Always computed from components rather than read off the aggregate
    `defensive_contribution` field, which is accrued under whatever position the
    player held at the time. See FINDINGS.md caveat 3b.
    """
    if position == "GKP":
        return 0
    if position == "DEF":
        return tackles + cbi
    return tackles + cbi + recoveries


def score_match(stats: MatchStats, position: str | int, rules: ScoringRules) -> ScoreBreakdown:
    """Score one player's match. Returns zero for a player who did not appear."""
    position = normalise_position(position)
    consts = rules.constants

    if stats.minutes <= 0:
        return ScoreBreakdown()

    appearance = rules.long_play if stats.minutes >= consts.appearance_minutes else rules.short_play

    goals = stats.goals_scored * rules.goals_scored.get(position, 0)
    assists = stats.assists * rules.assists

    # A clean sheet needs 60 minutes and no goal conceded while on the pitch.
    if stats.clean_sheets is not None:
        kept_clean_sheet = bool(stats.clean_sheets)
    else:
        kept_clean_sheet = stats.minutes >= consts.appearance_minutes and stats.goals_conceded == 0
    clean_sheet = rules.clean_sheets.get(position, 0) if kept_clean_sheet else 0

    # Concession penalty applies per two goals, and only to defenders and keepers.
    conceded_rate = rules.goals_conceded.get(position, 0)
    conceded = (stats.goals_conceded // consts.goals_conceded_per_penalty) * conceded_rate

    saves = (stats.saves // consts.saves_per_point) * rules.saves

    threshold = consts.defcon_threshold(position)
    actions = defensive_actions(
        position, stats.tackles, stats.clearances_blocks_interceptions, stats.recoveries
    )
    defcon = (
        rules.defensive_contribution.get(position, 0)
        if threshold is not None and actions >= threshold
        else 0
    )

    return ScoreBreakdown(
        appearance=appearance,
        goals=goals,
        assists=assists,
        clean_sheet=clean_sheet,
        goals_conceded=conceded,
        saves=saves,
        penalties_saved=stats.penalties_saved * rules.penalties_saved,
        penalties_missed=stats.penalties_missed * rules.penalties_missed,
        cards=stats.yellow_cards * rules.yellow_cards + stats.red_cards * rules.red_cards,
        own_goals=stats.own_goals * rules.own_goals,
        defensive_contribution=defcon,
        bonus=stats.bonus,
    )
