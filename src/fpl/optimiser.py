"""Transfer, captain and chip decisions.

The question this answers is "what should I change, and why", so it evaluates whole
squads rather than comparing two players in isolation. A striker who out-scores your
current striker is worth nothing if he sits on your bench, and that only shows up if
you re-pick the eleven after the swap.

Objective, per the spec:

    sum over the horizon of decay^gw * (XI xP + captain bonus + bench_weight * bench xP)
    minus 4 per transfer beyond the free allowance

"Do nothing and roll" is evaluated as an option in its own right, because it usually
wins and a tool that never says "hold" is a tool that will churn your squad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .db import Database
from .model import Model
from .squad import (
    MAX_PER_CLUB,
    Squad,
    SquadPlayer,
    best_eleven,
    selling_price,
)

HIT_COST = 4
MAX_FREE_TRANSFERS = 5


@dataclass
class HorizonValue:
    total: float
    per_event: list[float] = field(default_factory=list)
    captains: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class TransferMove:
    out_player: SquadPlayer
    in_player: SquadPlayer
    gain: float                    # net of hit cost
    gross_gain: float
    hits: int
    cost_change: int               # tenths; negative means money freed
    bank_after: int
    reasoning: list[str] = field(default_factory=list)
    out_xp: float = 0.0
    in_xp: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.out_player.web_name} → {self.in_player.web_name}"


@dataclass
class PlayerVerdict:
    """One squad player, judged: keep him, watch him, or swap him."""

    player: SquadPlayer
    verdict: str                   # "keep" | "consider" | "swap"
    headline: str                  # one plain sentence
    detail: list[str] = field(default_factory=list)
    horizon_xp: float = 0.0
    next_xp: float = 0.0
    best_swap: TransferMove | None = None
    rank_in_squad: int = 0
    starts_xi: bool = True
    unavailable: bool = False

    @property
    def gain(self) -> float:
        return self.best_swap.gain if self.best_swap else 0.0


# Thresholds for the verdict. Judgement calls, not fitted: a move has to be worth
# more than the noise in the projection before it is worth telling you about.
SWAP_THRESHOLD = 3.0
CONSIDER_THRESHOLD = 1.0


@dataclass
class Recommendation:
    moves: list[TransferMove]
    gain: float
    hits: int
    description: str
    reasoning: list[str] = field(default_factory=list)


class Optimiser:
    def __init__(
        self,
        model: Model,
        decay: float = 0.85,
        bench_weight: float = 0.15,
        captain_multiplier: int = 2,
    ) -> None:
        self.model = model
        self.decay = decay
        self.bench_weight = bench_weight
        self.captain_multiplier = captain_multiplier
        self._xp_cache: dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------- valuation
    def xp(self, element_id: int, event: int) -> float:
        key = (element_id, event)
        if key not in self._xp_cache:
            self._xp_cache[key] = self.model.project_player(element_id, event).xp_mean
        return self._xp_cache[key]

    def warm_cache(self, element_ids: Iterable[int]) -> None:
        events = range(self.model.horizon_start, self.model.horizon_start + self.model.horizon)
        for element_id in element_ids:
            for event in events:
                self.xp(element_id, event)

    def value_squad(self, squad: Squad) -> HorizonValue:
        """Decayed horizon value, re-picking the eleven and the captain each week."""
        total = 0.0
        per_event: list[float] = []
        captains: list[tuple[int, float]] = []

        for index in range(self.model.horizon):
            event = self.model.horizon_start + index
            xp = {p.element_id: self.xp(p.element_id, event) for p in squad.players}

            xi, bench, _ = best_eleven(squad, xp)
            xi_total = sum(xp[p.element_id] for p in xi)
            bench_total = sum(xp[p.element_id] for p in bench)

            captain = max(xi, key=lambda p: xp[p.element_id]) if xi else None
            captain_bonus = xp[captain.element_id] * (self.captain_multiplier - 1) if captain else 0.0
            if captain:
                captains.append((captain.element_id, xp[captain.element_id]))

            week = xi_total + captain_bonus + self.bench_weight * bench_total
            total += (self.decay**index) * week
            per_event.append(week)

        return HorizonValue(total=total, per_event=per_event, captains=captains)

    # -------------------------------------------------------------- candidates
    def candidates(self, db: Database, squad: Squad, limit_per_position: int = 60) -> dict[str, list[SquadPlayer]]:
        """Plausible incoming players, ranked by horizon xP within each position.

        Filtered to available players only — recommending an injured player is worse
        than recommending nothing.
        """
        rows = db.query(
            """
            SELECT element_id, web_name, position, team, now_cost, status
            FROM v_player
            WHERE status = 'a' AND now_cost <= ?
            """,
            [squad.budget],
        ).to_dicts()

        owned = squad.ids
        by_position: dict[str, list[tuple[float, SquadPlayer]]] = {}
        for row in rows:
            if row["element_id"] in owned:
                continue
            horizon = sum(
                (self.decay**i) * self.xp(row["element_id"], self.model.horizon_start + i)
                for i in range(self.model.horizon)
            )
            player = SquadPlayer(
                element_id=row["element_id"], web_name=row["web_name"],
                position=row["position"], team=row["team"],
                now_cost=row["now_cost"], purchase_price=row["now_cost"],
            )
            by_position.setdefault(row["position"], []).append((horizon, player))

        return {
            position: [player for _, player in sorted(entries, key=lambda e: -e[0])[:limit_per_position]]
            for position, entries in by_position.items()
        }

    # ------------------------------------------------------------- transfers
    def single_transfers(
        self, db: Database, squad: Squad, top: int = 10
    ) -> list[TransferMove]:
        baseline = self.value_squad(squad).total
        pool = self.candidates(db, squad)
        club_counts = squad.club_counts()

        moves: list[TransferMove] = []
        for outgoing in squad.players:
            for incoming in pool.get(outgoing.position, []):
                budget_after = squad.bank + outgoing.sell_value - incoming.now_cost
                if budget_after < 0:
                    continue

                # Club limit, counting the departure first.
                projected = club_counts.get(incoming.team, 0) + 1
                if incoming.team == outgoing.team:
                    projected -= 1
                if projected > MAX_PER_CLUB:
                    continue

                candidate = squad.replace(outgoing.element_id, incoming)
                gross = self.value_squad(candidate).total - baseline
                hits = 0 if squad.free_transfers >= 1 else 1
                net = gross - hits * HIT_COST

                moves.append(TransferMove(
                    out_player=outgoing, in_player=incoming,
                    gain=net, gross_gain=gross, hits=hits,
                    cost_change=incoming.now_cost - outgoing.sell_value,
                    bank_after=budget_after,
                    out_xp=sum(self.xp(outgoing.element_id, self.model.horizon_start + i)
                               for i in range(self.model.horizon)),
                    in_xp=sum(self.xp(incoming.element_id, self.model.horizon_start + i)
                              for i in range(self.model.horizon)),
                ))

        moves.sort(key=lambda m: -m.gain)
        for move in moves[:top]:
            move.reasoning = self.explain(move)
        return moves[:top]

    def double_transfers(
        self, db: Database, squad: Squad, singles: Sequence[TransferMove], top: int = 5
    ) -> list[Recommendation]:
        """Pairs built from the best singles.

        Searching all pairs is quadratic in a pool of thousands; pairing the best
        singles finds essentially the same moves for a fraction of the work. Stated
        because it is an approximation, not an exhaustive search.
        """
        baseline = self.value_squad(squad).total
        shortlist = singles[:14]
        results: list[Recommendation] = []

        for i, first in enumerate(shortlist):
            for second in shortlist[i + 1:]:
                if first.out_player.element_id == second.out_player.element_id:
                    continue
                if first.in_player.element_id == second.in_player.element_id:
                    continue

                interim = squad.replace(first.out_player.element_id, first.in_player)
                if second.out_player.element_id not in interim.ids:
                    continue
                outgoing = next(p for p in interim.players
                                if p.element_id == second.out_player.element_id)
                budget_after = interim.bank + outgoing.sell_value - second.in_player.now_cost
                if budget_after < 0:
                    continue

                candidate = interim.replace(second.out_player.element_id, second.in_player)
                counts = candidate.club_counts()
                if any(count > MAX_PER_CLUB for count in counts.values()):
                    continue

                gross = self.value_squad(candidate).total - baseline
                hits = max(0, 2 - squad.free_transfers)
                net = gross - hits * HIT_COST

                results.append(Recommendation(
                    moves=[first, second], gain=net, hits=hits,
                    description=f"{first.label} and {second.label}",
                    reasoning=[
                        f"Two moves gain {gross:.2f} points over {self.model.horizon} gameweeks"
                        + (f", less {hits * HIT_COST} for {hits} hit{'s' if hits > 1 else ''}."
                           if hits else " with no hit, using both free transfers."),
                    ],
                ))

        results.sort(key=lambda r: -r.gain)
        return results[:top]

    def roll_option(self, squad: Squad) -> Recommendation:
        """Doing nothing, stated explicitly with its own value."""
        banked = min(squad.free_transfers + 1, MAX_FREE_TRANSFERS)
        return Recommendation(
            moves=[], gain=0.0, hits=0, description="Roll: make no transfer",
            reasoning=[
                f"Keeps the squad as is and banks a transfer, taking you to "
                f"{banked} free transfer{'s' if banked != 1 else ''} next week"
                + (f" (the cap is {MAX_FREE_TRANSFERS})." if banked == MAX_FREE_TRANSFERS else "."),
                "This is the baseline every move below is measured against, so a move "
                "only earns its place by beating zero.",
            ],
        )

    # --------------------------------------------------------------- reasoning
    def explain(self, move: TransferMove) -> list[str]:
        """Name the components actually driving the recommendation."""
        horizon_events = range(self.model.horizon_start, self.model.horizon_start + self.model.horizon)
        out_projections = [self.model.project_player(move.out_player.element_id, e) for e in horizon_events]
        in_projections = [self.model.project_player(move.in_player.element_id, e) for e in horizon_events]

        reasons: list[str] = []
        reasons.append(
            f"Over the next {self.model.horizon} gameweeks the model projects "
            f"{move.in_xp:.1f} points for {move.in_player.web_name} against "
            f"{move.out_xp:.1f} for {move.out_player.web_name}."
        )

        # Which components moved, largest first.
        out_totals: dict[str, float] = {}
        in_totals: dict[str, float] = {}
        for projection in out_projections:
            for key, value in projection.components().items():
                out_totals[key] = out_totals.get(key, 0.0) + value
        for projection in in_projections:
            for key, value in projection.components().items():
                in_totals[key] = in_totals.get(key, 0.0) + value

        deltas = sorted(
            ((key, in_totals.get(key, 0.0) - out_totals.get(key, 0.0)) for key in in_totals),
            key=lambda kv: -abs(kv[1]),
        )
        drivers = [f"{key} {value:+.1f}" for key, value in deltas[:3] if abs(value) >= 0.15]
        if drivers:
            reasons.append("The difference is mostly " + ", ".join(drivers) + ".")

        # Minutes are the biggest single risk, so call them out.
        in_minutes = self.model.minutes.get(move.in_player.element_id)
        if in_minutes:
            if in_minutes.confidence == "prior":
                reasons.append(
                    f"Caution: {move.in_player.web_name} has no Premier League history, "
                    "so his minutes are guessed from his price rather than measured."
                )
            elif in_minutes.p_start < 0.65:
                reasons.append(
                    f"Caution: {move.in_player.web_name} started only "
                    f"{in_minutes.p_start:.0%} of the time last season."
                )

        # Fixtures
        difficulties = [p.difficulty for p in in_projections if p.fixture_count]
        if difficulties:
            average = sum(difficulties) / len(difficulties)
            opponents = ", ".join(
                f"{p.opponent}{'(H)' if p.is_home else '(A)'}" for p in in_projections[:3] if p.fixture_count
            )
            reasons.append(f"Fixtures average {average:.1f} difficulty: {opponents}.")

        blanks = sum(1 for p in in_projections if p.fixture_count == 0)
        if blanks:
            reasons.append(f"Note {blanks} blank gameweek(s) in the horizon for this player.")

        if move.cost_change > 0:
            reasons.append(f"Costs an extra £{move.cost_change / 10:.1f}m, "
                           f"leaving £{move.bank_after / 10:.1f}m in the bank.")
        elif move.cost_change < 0:
            reasons.append(f"Frees £{abs(move.cost_change) / 10:.1f}m, "
                           f"leaving £{move.bank_after / 10:.1f}m in the bank.")

        if move.hits:
            reasons.append(f"Needs a {move.hits * HIT_COST}-point hit, already subtracted above.")

        return reasons


    # ------------------------------------------------------------- squad review
    def review_squad(self, db: Database, squad: Squad) -> list[PlayerVerdict]:
        """Judge every player in the squad: keep, consider, or swap — and why.

        This is the plain answer to "who should I change". It walks each of the
        fifteen, finds the best legal replacement for that specific player, and turns
        the gap into a verdict with a reason a human can act on.
        """
        baseline = self.value_squad(squad).total
        pool = self.candidates(db, squad)
        club_counts = squad.club_counts()

        horizon_events = list(range(
            self.model.horizon_start, self.model.horizon_start + self.model.horizon
        ))
        horizon_xp = {
            player.element_id: sum(self.xp(player.element_id, event) for event in horizon_events)
            for player in squad.players
        }
        next_xp = {
            player.element_id: self.xp(player.element_id, self.model.horizon_start)
            for player in squad.players
        }
        starting_xi, _, _ = best_eleven(squad, next_xp)
        starters = {p.element_id for p in starting_xi}

        # Rank within position, so "your third-best midfielder" means something.
        by_position: dict[str, list[SquadPlayer]] = {}
        for player in squad.players:
            by_position.setdefault(player.position, []).append(player)
        position_rank: dict[int, int] = {}
        for position, members in by_position.items():
            for index, player in enumerate(
                sorted(members, key=lambda p: -horizon_xp[p.element_id]), start=1
            ):
                position_rank[player.element_id] = index

        verdicts: list[PlayerVerdict] = []
        for outgoing in squad.players:
            best_move: TransferMove | None = None

            for incoming in pool.get(outgoing.position, []):
                budget_after = squad.bank + outgoing.sell_value - incoming.now_cost
                if budget_after < 0:
                    continue
                projected = club_counts.get(incoming.team, 0) + 1
                if incoming.team == outgoing.team:
                    projected -= 1
                if projected > MAX_PER_CLUB:
                    continue

                candidate = squad.replace(outgoing.element_id, incoming)
                gross = self.value_squad(candidate).total - baseline
                hits = 0 if squad.free_transfers >= 1 else 1
                net = gross - hits * HIT_COST

                if best_move is None or net > best_move.gain:
                    best_move = TransferMove(
                        out_player=outgoing, in_player=incoming,
                        gain=net, gross_gain=gross, hits=hits,
                        cost_change=incoming.now_cost - outgoing.sell_value,
                        bank_after=budget_after,
                        out_xp=horizon_xp[outgoing.element_id],
                        in_xp=sum(self.xp(incoming.element_id, event) for event in horizon_events),
                    )

            if best_move is not None:
                best_move.reasoning = self.explain(best_move)

            verdicts.append(self._verdict_for(
                outgoing, best_move, horizon_xp[outgoing.element_id],
                next_xp[outgoing.element_id], position_rank[outgoing.element_id],
                outgoing.element_id in starters, len(by_position[outgoing.position]),
            ))

        order = {"swap": 0, "consider": 1, "keep": 2}
        verdicts.sort(key=lambda v: (order[v.verdict], -v.gain))
        return verdicts

    def _verdict_for(
        self,
        player: SquadPlayer,
        best_move: TransferMove | None,
        horizon_xp: float,
        next_xp: float,
        rank: int,
        starts_xi: bool,
        position_total: int,
    ) -> PlayerVerdict:
        gain = best_move.gain if best_move else 0.0
        profile = self.model.minutes.get(player.element_id)
        projection = self.model.project_player(player.element_id, self.model.horizon_start)

        unavailable = bool(profile and profile.availability < 1.0)
        detail: list[str] = []

        # What is good or bad about this player, in his own right.
        if profile and profile.availability == 0.0:
            detail.append(f"Flagged as unavailable, so he is projected to score nothing.")
        elif profile and profile.availability < 1.0:
            detail.append(
                f"A doubt: only {profile.availability:.0%} likely to be fit, "
                "which cuts everything below proportionally."
            )

        if profile and profile.confidence == "prior":
            detail.append(
                "No Premier League history, so his minutes are estimated from his price. "
                "Treat this projection as a guess."
            )
        elif profile and profile.p_start < 0.6:
            detail.append(f"Started only {profile.p_start:.0%} of matches last season.")

        # Appearance points dominate almost everyone, so saying so is noise. What is
        # worth knowing is where his *scoring* comes from beyond just turning up.
        components = {k: v for k, v in projection.components().items() if k != "minutes"}
        strongest = max(components.items(), key=lambda kv: kv[1]) if components else None
        if strongest and strongest[1] >= 0.5:
            share = strongest[1] / projection.xp_mean if projection.xp_mean > 0 else 0
            detail.append(
                f"Beyond appearance points, most of his score comes from {strongest[0]}"
                + (f" ({share:.0%} of his total)." if share > 0.2 else ".")
            )

        if projection.p_defcon_hit > 0.55:
            detail.append(
                f"Clears the defensive-contribution threshold about "
                f"{projection.p_defcon_hit:.0%} of the time, which is a reliable two points."
            )

        ordinal = {1: "best", 2: "second-best", 3: "third-best"}.get(rank, f"{rank}th-best")

        if unavailable and profile and profile.availability == 0.0:
            verdict = "swap"
            headline = f"Not available — {projection.notes[0] if projection.notes else 'flagged'}."
        elif gain >= SWAP_THRESHOLD and best_move:
            verdict = "swap"
            headline = (
                f"Worth changing: {best_move.in_player.web_name} projects "
                f"{best_move.in_xp - best_move.out_xp:+.1f} points more over the next "
                f"{self.model.horizon} gameweeks."
            )
        elif gain >= CONSIDER_THRESHOLD and best_move:
            verdict = "consider"
            headline = (
                f"Only a small upgrade available "
                f"({best_move.in_player.web_name}, {gain:+.1f}). Probably not worth the move."
            )
        else:
            verdict = "keep"
            if not starts_xi:
                headline = (
                    f"Bench player, and no better option is affordable. "
                    f"Projects {horizon_xp:.1f} points over {self.model.horizon} gameweeks."
                )
            else:
                headline = (
                    f"Your {ordinal} {player.position}, projecting {horizon_xp:.1f} points "
                    f"over {self.model.horizon} gameweeks. Nothing affordable beats him."
                )

        return PlayerVerdict(
            player=player, verdict=verdict, headline=headline, detail=detail,
            horizon_xp=horizon_xp, next_xp=next_xp, best_swap=best_move,
            rank_in_squad=rank, starts_xi=starts_xi, unavailable=unavailable,
        )

    # ---------------------------------------------------------------- captain
    def captain_options(self, squad: Squad, event: int | None = None, top: int = 6) -> list[dict]:
        event = event or self.model.horizon_start
        options = []
        for player in squad.players:
            projection = self.model.project_player(player.element_id, event, simulate=4000)
            options.append({
                "element_id": player.element_id,
                "web_name": player.web_name,
                "position": player.position,
                "team": player.team,
                "xp": projection.xp_mean,
                "captain_xp": projection.xp_mean * self.captain_multiplier,
                "p_haul": projection.p_haul_10plus,
                "p_blank": projection.p_blank_2minus,
                "p90": projection.xp_p90,
                "opponent": projection.opponent,
                "is_home": projection.is_home,
                "difficulty": projection.difficulty,
                "notes": projection.notes,
            })
        options.sort(key=lambda o: -o["xp"])
        return options[:top]
