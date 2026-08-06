"""Squad rules, valuation and eleven selection.

The spec makes these acceptance criteria: the optimiser must respect every FPL
constraint, proven with edge cases — four from one club rejected, invalid formation
rejected, budget overrun rejected. Those are here, along with the selling-price rule,
which is the quiet one: get it wrong and the optimiser recommends transfers you
cannot actually afford.
"""

from __future__ import annotations

import pytest

from fpl.squad import (
    FORMATIONS,
    MAX_PER_CLUB,
    Squad,
    SquadPlayer,
    best_eleven,
    selling_price,
    validate,
)


def player(element_id: int, position: str, team: str = "ARS",
           now_cost: int = 50, purchase: int | None = None) -> SquadPlayer:
    return SquadPlayer(
        element_id=element_id, web_name=f"P{element_id}", position=position,
        team=team, now_cost=now_cost,
        purchase_price=now_cost if purchase is None else purchase,
    )


def legal_squad(**kwargs) -> Squad:
    """2 GKP, 5 DEF, 5 MID, 3 FWD spread across enough clubs to be legal."""
    clubs = ["ARS", "AVL", "BOU", "BRE", "BHA", "CHE", "CRY"]
    players, element_id = [], 1
    for position, count in (("GKP", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)):
        for i in range(count):
            players.append(player(element_id, position, team=clubs[element_id % len(clubs)]))
            element_id += 1
    return Squad(players=players, **kwargs)


# ------------------------------------------------------------ selling price
@pytest.mark.parametrize("purchase,now,expected", [
    (50, 50, 50),      # unchanged
    (50, 45, 45),      # fallen: you take the full loss
    (50, 55, 52),      # risen 0.5: profit halved, rounded down
    (50, 54, 52),      # risen 0.4: 0.2 profit
    (50, 51, 50),      # risen 0.1: no profit until 0.2
    (50, 60, 55),      # risen 1.0: half kept
    (120, 133, 126),   # a big riser
])
def test_selling_price_halves_profit_rounding_down(purchase, now, expected):
    assert selling_price(purchase, now) == expected


def test_squad_value_uses_selling_not_current_price():
    squad = Squad(players=[player(1, "MID", now_cost=60, purchase=50)])
    assert squad.sell_value == 55
    assert squad.sell_value != 60, "using now_cost would overstate the budget"


# --------------------------------------------------------------- validation
def test_a_legal_squad_has_no_problems():
    assert validate(legal_squad()) == []


def test_four_players_from_one_club_is_rejected():
    squad = legal_squad()
    for i in range(4):
        squad.players[i].team = "LIV"
    problems = validate(squad)
    assert any("LIV" in p and "maximum" in p for p in problems), problems


def test_exactly_three_from_one_club_is_allowed():
    squad = legal_squad()
    for i in range(MAX_PER_CLUB):
        squad.players[i].team = "LIV"
    assert validate(squad) == []


def test_wrong_position_counts_are_rejected():
    squad = legal_squad()
    squad.players[-1].position = "MID"      # now 6 MID, 2 FWD
    problems = validate(squad)
    assert any("MID" in p for p in problems)
    assert any("FWD" in p for p in problems)


def test_wrong_squad_size_is_rejected():
    squad = legal_squad()
    squad.players.pop()
    assert any("14 players" in p for p in validate(squad))


def test_budget_overrun_is_rejected():
    squad = legal_squad(bank=-5)
    assert any("over budget" in p for p in validate(squad))


def test_duplicate_player_is_rejected():
    squad = legal_squad()
    squad.players[1] = player(squad.players[0].element_id, squad.players[1].position)
    assert any("twice" in p for p in validate(squad))


# --------------------------------------------------------------- formations
def test_every_generated_formation_is_legal():
    for formation in FORMATIONS:
        keepers, defenders, midfielders, forwards = formation
        assert keepers == 1
        assert sum(formation) == 11
        assert 3 <= defenders <= 5
        assert 2 <= midfielders <= 5
        assert 1 <= forwards <= 3


def test_illegal_formations_are_not_generated():
    """Two at the back and five up front are both illegal, however good the players."""
    assert (1, 2, 5, 3) not in FORMATIONS
    assert (1, 5, 1, 4) not in FORMATIONS
    assert (1, 6, 3, 1) not in FORMATIONS


def test_best_eleven_never_picks_two_keepers():
    squad = legal_squad()
    xp = {p.element_id: 10.0 if p.position == "GKP" else 1.0 for p in squad.players}
    xi, bench, formation = best_eleven(squad, xp)
    assert sum(1 for p in xi if p.position == "GKP") == 1
    assert formation[0] == 1


def test_best_eleven_respects_the_minimum_three_defenders():
    """Even when every defender is worthless, three must play."""
    squad = legal_squad()
    xp = {p.element_id: (0.0 if p.position == "DEF" else 9.0) for p in squad.players}
    xi, _, formation = best_eleven(squad, xp)
    assert sum(1 for p in xi if p.position == "DEF") == 3
    assert formation[1] == 3


def test_best_eleven_picks_the_highest_scorers_available():
    squad = legal_squad()
    xp = {p.element_id: float(p.element_id) for p in squad.players}
    xi, bench, _ = best_eleven(squad, xp)
    assert len(xi) == 11 and len(bench) == 4
    assert min(xp[p.element_id] for p in xi) > 0
    # The very best outfielder must start.
    best_outfield = max((p for p in squad.players if p.position != "GKP"),
                        key=lambda p: xp[p.element_id])
    assert best_outfield in xi


def test_bench_keeps_the_spare_keeper_last():
    squad = legal_squad()
    xp = {p.element_id: 1.0 for p in squad.players}
    _, bench, _ = best_eleven(squad, xp)
    assert bench[-1].position == "GKP", "the reserve keeper is the last substitute"


# ---------------------------------------------------------------- transfers
def test_replace_adjusts_the_bank_by_the_selling_price():
    squad = legal_squad(bank=10)
    outgoing = squad.players[0]
    outgoing.purchase_price = 50
    outgoing.now_cost = 60          # sells for 55, not 60
    incoming = player(99, outgoing.position, team="LIV", now_cost=70)

    after = squad.replace(outgoing.element_id, incoming)
    assert after.bank == 10 + 55 - 70
    assert incoming.element_id in after.ids
    assert outgoing.element_id not in after.ids
    assert len(after.players) == 15


def test_replace_leaves_the_original_squad_untouched():
    squad = legal_squad(bank=0)
    original = set(squad.ids)
    squad.replace(squad.players[0].element_id, player(99, squad.players[0].position))
    assert squad.ids == original
