"""Minutes model.

Everything else multiplies by this, so it comes first and it is kept explicit.
Produces, per player: P(start), P(bench appearance), P(60+ minutes) and expected
minutes, before and after availability news.

No 2026/27 matches have been played, so the base rates come from last season's
per-match history. That has two consequences worth stating plainly rather than
burying: a player who changed club looks like their old role, and a player with no
Premier League history has no evidence at all and falls back to a price-based prior.
Both are flagged on the output so the UI can show confidence rather than implying it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..db import Database

RECENCY_DECAY = 0.93        # per match going back; ~10 match effective window
MIN_MATCHES_FOR_EVIDENCE = 6
STARTER_MINUTES_FALLBACK = 82.0
BENCH_MINUTES_FALLBACK = 18.0

# World Cup 2026 ran into July; players from deep-running nations returned late.
# Applied to the opening gameweeks only, and surfaced as a flag either way.
WC_EARLY_EVENTS = 3
WC_MINUTES_HAIRCUT = 0.85


@dataclass
class MinutesProfile:
    element_id: int
    p_start: float
    p_bench: float
    p_unused: float
    p_60: float
    expected_minutes: float
    minutes_if_start: float
    minutes_if_bench: float
    matches_observed: int
    availability: float          # multiplier applied from status / chance_of_playing
    confidence: str              # "evidence" | "thin" | "prior"
    wc_returnee: bool = False
    note: str = ""

    def for_event(self, event: int) -> "MinutesProfile":
        """Apply the early-season World Cup haircut where it applies."""
        if not self.wc_returnee or event > WC_EARLY_EVENTS:
            return self
        scale = WC_MINUTES_HAIRCUT
        return MinutesProfile(
            element_id=self.element_id,
            p_start=self.p_start * scale,
            p_bench=self.p_bench,
            p_unused=max(0.0, 1.0 - self.p_start * scale - self.p_bench),
            p_60=self.p_60 * scale,
            expected_minutes=self.expected_minutes * scale,
            minutes_if_start=self.minutes_if_start,
            minutes_if_bench=self.minutes_if_bench,
            matches_observed=self.matches_observed,
            availability=self.availability,
            confidence=self.confidence,
            wc_returnee=True,
            note=(self.note + " late World Cup return: opening-weeks minutes reduced.").strip(),
        )


def availability_multiplier(status: str | None, chance: int | None) -> tuple[float, str]:
    """Turn the API's availability fields into a probability of being fit.

    `chance_of_playing_next_round` is authoritative when present. Otherwise the
    status code carries the meaning: d=doubtful, i=injured, s=suspended,
    u=unavailable, n=on loan or not in squad.
    """
    if chance is not None:
        return chance / 100.0, f"chance of playing {chance}%"
    if status in (None, "a"):
        return 1.0, ""
    if status == "d":
        return 0.75, "doubtful, no percentage given"
    return 0.0, {
        "i": "injured", "s": "suspended",
        "u": "unavailable", "n": "not in squad",
    }.get(status or "", "flagged")


def _price_prior(now_cost: int, element_type: int) -> float:
    """P(start) for a player with no history, from price alone.

    Crude and labelled as such: price is the market's view of a player's role, which
    is better than nothing and worse than evidence.
    """
    thresholds = {
        1: [(50, 0.75), (45, 0.35), (0, 0.10)],      # GKP: a clear number one is priced up
        2: [(60, 0.85), (50, 0.60), (45, 0.30), (0, 0.12)],
        3: [(80, 0.88), (60, 0.70), (50, 0.40), (0, 0.15)],
        4: [(80, 0.88), (60, 0.65), (50, 0.35), (0, 0.15)],
    }
    for floor, probability in thresholds.get(element_type, thresholds[3]):
        if now_cost >= floor:
            return probability
    return 0.15


def build_profiles(
    db: Database,
    season: str = "2025-26",
    wc_returnees: Iterable[int] = (),
) -> dict[int, MinutesProfile]:
    """One profile per current player."""
    wc_set = set(wc_returnees)

    history = db.query(
        """
        SELECT element_id, event, minutes, starts
        FROM hist_player_gw
        WHERE season = ? AND element_id IS NOT NULL
        ORDER BY element_id, event
        """,
        [season],
    ).to_dicts()

    by_player: dict[int, list[dict]] = {}
    for row in history:
        by_player.setdefault(row["element_id"], []).append(row)

    current = db.query(
        "SELECT element_id, element_type, now_cost, status, "
        "chance_of_playing_next_round AS chance "
        "FROM latest_players_state s JOIN latest_players_identity USING (element_id)"
    ).to_dicts()

    profiles: dict[int, MinutesProfile] = {}
    for player in current:
        element_id = player["element_id"]
        matches = by_player.get(element_id, [])
        available, availability_note = availability_multiplier(player["status"], player["chance"])

        if len(matches) >= MIN_MATCHES_FOR_EVIDENCE:
            profile = _from_history(element_id, matches)
            confidence = "evidence" if len(matches) >= 15 else "thin"
        elif matches:
            profile = _from_history(element_id, matches)
            confidence = "thin"
        else:
            prior = _price_prior(player["now_cost"], player["element_type"])
            profile = {
                "p_start": prior, "p_bench": min(0.25, (1 - prior) * 0.35),
                "p_60": prior * 0.88,
                "minutes_if_start": STARTER_MINUTES_FALLBACK,
                "minutes_if_bench": BENCH_MINUTES_FALLBACK,
                "matches": 0,
            }
            confidence = "prior"

        p_start = profile["p_start"] * available
        p_bench = profile["p_bench"] * available
        p_60 = profile["p_60"] * available
        expected = (p_start * profile["minutes_if_start"] + p_bench * profile["minutes_if_bench"])

        note = availability_note
        if confidence == "prior":
            note = (note + " no Premier League history: minutes estimated from price.").strip()
        elif confidence == "thin":
            note = (note + f" only {profile['matches']} matches of history.").strip()

        profiles[element_id] = MinutesProfile(
            element_id=element_id,
            p_start=p_start, p_bench=p_bench,
            p_unused=max(0.0, 1.0 - p_start - p_bench),
            p_60=p_60, expected_minutes=expected,
            minutes_if_start=profile["minutes_if_start"],
            minutes_if_bench=profile["minutes_if_bench"],
            matches_observed=profile["matches"],
            availability=available, confidence=confidence,
            wc_returnee=element_id in wc_set, note=note,
        )
    return profiles


def _from_history(element_id: int, matches: list[dict]) -> dict:
    """Recency-weighted rates from a player's own match log."""
    ordered = sorted(matches, key=lambda m: m["event"], reverse=True)
    weights = [RECENCY_DECAY**i for i in range(len(ordered))]
    total_weight = sum(weights) or 1.0

    w_start = w_bench = w_60 = 0.0
    start_minutes = start_weight = 0.0
    bench_minutes = bench_weight = 0.0

    for weight, match in zip(weights, ordered):
        minutes = match["minutes"] or 0
        started = bool(match["starts"])
        if started:
            w_start += weight
            start_minutes += weight * minutes
            start_weight += weight
        elif minutes > 0:
            w_bench += weight
            bench_minutes += weight * minutes
            bench_weight += weight
        if minutes >= 60:
            w_60 += weight

    return {
        "p_start": w_start / total_weight,
        "p_bench": w_bench / total_weight,
        "p_60": w_60 / total_weight,
        "minutes_if_start": (start_minutes / start_weight) if start_weight else STARTER_MINUTES_FALLBACK,
        "minutes_if_bench": (bench_minutes / bench_weight) if bench_weight else BENCH_MINUTES_FALLBACK,
        "matches": len(ordered),
    }
