"""Squad state, validation and valuation.

Two things the FPL API will not tell us and this module therefore owns: the squad
itself before the first deadline (picks are not public until then) and the selling
value of each player (which depends on what you paid, not what they cost now).

Selling rule: profit is halved and rounded down to the nearest £0.1m. A player bought
at 6.0 now worth 6.5 sells for 6.2, not 6.5. Getting this wrong quietly inflates the
budget and makes the optimiser recommend transfers you cannot afford.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .db import Database

SQUAD_SIZE = 15
SQUAD_BY_POSITION = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3
STARTING_XI = 11
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
INITIAL_BUDGET = 1000     # tenths of a million


def selling_price(purchase_price: int, now_cost: int) -> int:
    """FPL sell-on rule, in tenths. Profit is halved, rounded down."""
    if now_cost <= purchase_price:
        return now_cost
    profit = now_cost - purchase_price
    return purchase_price + profit // 2


@dataclass
class SquadPlayer:
    element_id: int
    web_name: str
    position: str
    team: str
    now_cost: int
    purchase_price: int
    slot: int = 0
    is_captain: bool = False
    is_vice_captain: bool = False

    @property
    def sell_value(self) -> int:
        return selling_price(self.purchase_price, self.now_cost)


@dataclass
class Squad:
    players: list[SquadPlayer]
    bank: int = 0                 # tenths
    free_transfers: int = 1
    manager_id: int = 0
    event: int = 1

    @property
    def sell_value(self) -> int:
        return sum(p.sell_value for p in self.players)

    @property
    def budget(self) -> int:
        """What is available to spend if the whole squad were sold."""
        return self.sell_value + self.bank

    @property
    def ids(self) -> set[int]:
        return {p.element_id for p in self.players}

    def by_position(self) -> dict[str, list[SquadPlayer]]:
        grouped: dict[str, list[SquadPlayer]] = {}
        for player in self.players:
            grouped.setdefault(player.position, []).append(player)
        return grouped

    def club_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for player in self.players:
            counts[player.team] = counts.get(player.team, 0) + 1
        return counts

    def replace(self, out_id: int, incoming: SquadPlayer) -> "Squad":
        """A copy with one player swapped, bank adjusted by the price difference."""
        outgoing = next(p for p in self.players if p.element_id == out_id)
        players = [p for p in self.players if p.element_id != out_id] + [incoming]
        return Squad(
            players=players,
            bank=self.bank + outgoing.sell_value - incoming.now_cost,
            free_transfers=self.free_transfers,
            manager_id=self.manager_id,
            event=self.event,
        )


def validate(squad: Squad, check_budget: bool = True) -> list[str]:
    """Every FPL squad rule. Returns a list of problems; empty means legal."""
    problems: list[str] = []

    if len(squad.players) != SQUAD_SIZE:
        problems.append(f"squad has {len(squad.players)} players, needs {SQUAD_SIZE}")

    if len({p.element_id for p in squad.players}) != len(squad.players):
        problems.append("the same player appears twice")

    grouped = squad.by_position()
    for position, required in SQUAD_BY_POSITION.items():
        actual = len(grouped.get(position, []))
        if actual != required:
            problems.append(f"{position}: {actual} players, needs {required}")

    for club, count in squad.club_counts().items():
        if count > MAX_PER_CLUB:
            problems.append(f"{count} players from {club}, maximum is {MAX_PER_CLUB}")

    if check_budget and squad.bank < 0:
        problems.append(f"over budget by £{abs(squad.bank) / 10:.1f}m")

    return problems


def valid_formations() -> list[tuple[int, int, int, int]]:
    """Every legal (GKP, DEF, MID, FWD) split of the starting eleven."""
    formations = []
    for defenders in range(XI_MIN["DEF"], XI_MAX["DEF"] + 1):
        for midfielders in range(XI_MIN["MID"], XI_MAX["MID"] + 1):
            forwards = STARTING_XI - 1 - defenders - midfielders
            if XI_MIN["FWD"] <= forwards <= XI_MAX["FWD"]:
                formations.append((1, defenders, midfielders, forwards))
    return formations


FORMATIONS = valid_formations()


def best_eleven(
    squad: Squad, xp: dict[int, float]
) -> tuple[list[SquadPlayer], list[SquadPlayer], tuple[int, int, int, int]]:
    """Highest-scoring legal eleven, plus the bench and the formation used."""
    grouped = squad.by_position()
    ranked = {
        position: sorted(players, key=lambda p: xp.get(p.element_id, 0.0), reverse=True)
        for position, players in grouped.items()
    }

    best_total = float("-inf")
    best_xi: list[SquadPlayer] = []
    best_formation = (1, 4, 4, 2)

    for formation in FORMATIONS:
        counts = dict(zip(("GKP", "DEF", "MID", "FWD"), formation))
        if any(len(ranked.get(pos, [])) < need for pos, need in counts.items()):
            continue
        xi = [p for pos, need in counts.items() for p in ranked[pos][:need]]
        total = sum(xp.get(p.element_id, 0.0) for p in xi)
        if total > best_total:
            best_total, best_xi, best_formation = total, xi, formation

    chosen = {p.element_id for p in best_xi}
    bench = sorted(
        (p for p in squad.players if p.element_id not in chosen),
        key=lambda p: (p.position == "GKP", -xp.get(p.element_id, 0.0)),
    )
    return best_xi, bench, best_formation


def load_squad(db: Database, manager_id: int, event: int) -> Squad | None:
    """Most recent squad for this manager: the manual entry, or API picks."""
    manual = db.query(
        """
        SELECT m.element_id, m.slot, m.is_captain, m.is_vice_captain,
               m.purchase_price, v.web_name, v.position, v.team, v.now_cost
        FROM manual_squad m JOIN v_player v USING (element_id)
        WHERE m.manager_id = ? AND m.recorded_at = (
            SELECT MAX(recorded_at) FROM manual_squad WHERE manager_id = ?
        )
        ORDER BY m.slot
        """,
        [manager_id, manager_id],
    ).to_dicts()

    source = manual
    if not source:
        source = db.query(
            """
            SELECT p.element_id, p.position AS slot, p.is_captain, p.is_vice_captain,
                   COALESCE(p.purchase_price, v.now_cost) AS purchase_price,
                   v.web_name, v.position, v.team, v.now_cost
            FROM latest_my_picks p JOIN v_player v USING (element_id)
            WHERE p.manager_id = ? AND p.event = ?
            ORDER BY p.position
            """,
            [manager_id, event],
        ).to_dicts()

    if not source:
        return None

    players = [
        SquadPlayer(
            element_id=row["element_id"], web_name=row["web_name"],
            position=row["position"], team=row["team"],
            now_cost=row["now_cost"],
            purchase_price=row["purchase_price"] or row["now_cost"],
            slot=row["slot"] or 0,
            is_captain=bool(row["is_captain"]),
            is_vice_captain=bool(row["is_vice_captain"]),
        )
        for row in source
    ]

    state = db.query(
        """
        SELECT bank, free_transfers FROM my_manual_state
        WHERE manager_id = ? ORDER BY recorded_at DESC LIMIT 1
        """,
        [manager_id],
    ).to_dicts()
    bank = state[0]["bank"] if state else 0
    free_transfers = state[0]["free_transfers"] if state else 1

    return Squad(players=players, bank=bank or 0, free_transfers=free_transfers or 1,
                 manager_id=manager_id, event=event)


def save_squad(
    db: Database,
    manager_id: int,
    event: int,
    entries: Sequence[dict],
    bank: int,
    free_transfers: int,
    note: str = "manual entry",
) -> None:
    """Append a squad. Never updates in place, so earlier entries stay auditable."""
    now = datetime.now(timezone.utc)
    rows = [
        {
            "manager_id": manager_id, "event": event, "recorded_at": now,
            "element_id": int(entry["element_id"]), "slot": int(entry.get("slot", index + 1)),
            "is_captain": bool(entry.get("is_captain")),
            "is_vice_captain": bool(entry.get("is_vice_captain")),
            "purchase_price": int(entry["purchase_price"]),
            "selling_price": int(entry.get("selling_price") or entry["purchase_price"]),
        }
        for index, entry in enumerate(entries)
    ]
    db.append("manual_squad", rows)
    db.append("my_manual_state", [{
        "manager_id": manager_id, "event": event, "recorded_at": now,
        "free_transfers": int(free_transfers), "bank": int(bank), "note": note,
    }])
