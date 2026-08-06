"""Tests for the data layer.

Scope note, per the spec's "skip tests that would be theatre": there is no maths in
Milestone 1 to test. What is load-bearing here is append-on-change — if it drops a
genuine change, the price and injury time series silently develop holes and every
backtest built on them is wrong. That, the hash stability it depends on, and the
type coercion at the JSON boundary are what these tests cover.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpl import loaders
from fpl.db import Database, row_hash

UTC = timezone.utc


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.duckdb")
    yield database
    database.close()


@pytest.fixture()
def bootstrap() -> dict:
    """Real payload captured from the API on 2026-08-05, not a hand-written fake.

    Committed to the repo (116 KB gzipped) rather than read from data/, which is
    gitignored: a test that silently skips on a fresh clone is worse than no test.
    """
    path = Path(__file__).parent / "fixtures" / "bootstrap-static-2026-08-05.json.gz"
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


# --------------------------------------------------------------------- hashing
def test_row_hash_ignores_snapshot_metadata():
    """Two observations of an unchanged entity must hash the same."""
    base = {"element_id": 1, "now_cost": 60, "status": "a"}
    first = {**base, "snapshot_id": 1, "snapshot_at": datetime(2026, 8, 1, tzinfo=UTC)}
    second = {**base, "snapshot_id": 99, "snapshot_at": datetime(2026, 8, 2, tzinfo=UTC)}
    assert row_hash(first) == row_hash(second)


def test_row_hash_is_key_order_independent():
    assert row_hash({"a": 1, "b": 2}) == row_hash({"b": 2, "a": 1})


def test_row_hash_detects_a_one_tenth_price_move():
    a = {"element_id": 1, "now_cost": 60}
    b = {"element_id": 1, "now_cost": 61}
    assert row_hash(a) != row_hash(b)


def test_row_hash_distinguishes_none_from_zero():
    """penalties_order NULL (not a taker) must not hash equal to 0."""
    assert row_hash({"penalties_order": None}) != row_hash({"penalties_order": 0})


# ------------------------------------------------------------ append-on-change
def _state_row(element_id: int, at: datetime, sid: int, **overrides):
    row = {"element_id": element_id, "now_cost": 60, "status": "a",
           "news": "", "penalties_order": None, "total_points": 0}
    row.update(overrides)
    row["row_hash"] = row_hash(row)
    row["snapshot_id"] = sid
    row["snapshot_at"] = at
    return row


def test_identical_rows_are_not_reappended(db: Database):
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    rows = [_state_row(i, t0, 1) for i in range(1, 6)]
    assert db.append_on_change("players_state", rows, ["element_id"]) == 5

    later = [_state_row(i, t0 + timedelta(hours=1), 2) for i in range(1, 6)]
    assert db.append_on_change("players_state", later, ["element_id"]) == 0
    assert db.scalar("SELECT COUNT(*) FROM players_state") == 5


def test_a_changed_row_is_appended_and_history_is_kept(db: Database):
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    db.append_on_change("players_state", [_state_row(1, t0, 1, now_cost=60)], ["element_id"])
    added = db.append_on_change("players_state", [_state_row(1, t1, 2, now_cost=61)], ["element_id"])

    assert added == 1
    assert db.scalar("SELECT COUNT(*) FROM players_state") == 2, "old version must survive"
    assert db.scalar("SELECT now_cost FROM latest_players_state WHERE element_id = 1") == 61


def test_only_the_changed_entity_is_appended(db: Database):
    """A single price move must not rewrite the other 569 players."""
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    db.append_on_change("players_state", [_state_row(i, t0, 1) for i in range(1, 11)], ["element_id"])

    second = [_state_row(i, t1, 2) for i in range(1, 11)]
    second[3] = _state_row(4, t1, 2, now_cost=59)
    assert db.append_on_change("players_state", second, ["element_id"]) == 1
    assert db.scalar("SELECT COUNT(*) FROM players_state") == 11


def test_reverting_to_a_previous_value_still_appends(db: Database):
    """Compare against the latest version, not any version: 60 -> 61 -> 60 is 3 rows."""
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    db.append_on_change("players_state", [_state_row(1, t0, 1, now_cost=60)], ["element_id"])
    db.append_on_change("players_state", [_state_row(1, t0 + timedelta(hours=1), 2, now_cost=61)], ["element_id"])
    added = db.append_on_change("players_state", [_state_row(1, t0 + timedelta(hours=2), 3, now_cost=60)], ["element_id"])

    assert added == 1
    assert db.scalar("SELECT COUNT(*) FROM players_state") == 3
    assert db.scalar("SELECT now_cost FROM latest_players_state WHERE element_id = 1") == 60


def test_composite_key_dedup(db: Database):
    """scoring_rules is keyed on (rule_name, position), not on either alone."""
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    rows = []
    for position, points in (("GKP", 10), ("DEF", 6), ("MID", 5), ("FWD", 4)):
        row = {"rule_name": "goals_scored", "position": position, "points": points}
        row["row_hash"] = row_hash(row)
        row["snapshot_id"] = 1
        row["snapshot_at"] = t0
        rows.append(row)

    assert db.append_on_change("scoring_rules", rows, ["rule_name", "position"]) == 4
    assert db.append_on_change("scoring_rules", rows, ["rule_name", "position"]) == 0


# -------------------------------------------------------- change-detection views
def test_price_change_view_ignores_the_genesis_snapshot(db: Database):
    """First-ever observation is not a change; without the guard it emits a phantom."""
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    db.append_on_change("players_state", [_state_row(1, t0, 1, now_cost=60)], ["element_id"])
    assert db.scalar("SELECT COUNT(*) FROM price_changes") == 0

    db.append_on_change("players_state", [_state_row(1, t0 + timedelta(hours=1), 2, now_cost=61)], ["element_id"])
    assert db.scalar("SELECT COUNT(*) FROM price_changes") == 1
    assert db.scalar("SELECT delta FROM price_changes") == 1


def test_set_piece_view_catches_a_penalty_order_promotion(db: Database):
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    db.append_on_change("players_state", [_state_row(1, t0, 1, penalties_order=2)], ["element_id"])
    db.append_on_change("players_state",
                        [_state_row(1, t0 + timedelta(hours=1), 2, penalties_order=1)], ["element_id"])

    changes = db.query("SELECT field, old_value, new_value FROM set_piece_changes")
    assert len(changes) == 1
    assert changes.row(0) == ("penalties_order", 2, 1)


# ------------------------------------------------------------------- coercion
@pytest.mark.parametrize("raw,expected", [("33.5", 33.5), ("", None), (None, None), (0.0, 0.0)])
def test_float_coercion_at_the_json_boundary(raw, expected):
    """The API returns xG as strings and per_90s as floats; empty string is not zero."""
    assert loaders._f(raw) == expected


@pytest.mark.parametrize("raw,expected", [("5", 5), ("", None), (None, None), (3, 3)])
def test_int_coercion(raw, expected):
    assert loaders._i(raw) == expected


# ----------------------------------------------- loaders against a real payload
def test_scoring_rules_flatten_scalars_and_position_maps(bootstrap):
    rows = loaders.load_scoring_rules(bootstrap, 1, datetime.now(UTC))
    by_key = {(r["rule_name"], r["position"]): r["points"] for r in rows}

    assert by_key[("assists", "ALL")] == 3           # scalar
    assert by_key[("goals_scored", "GKP")] == 10     # position map, 2026/27 change
    assert by_key[("goals_scored", "FWD")] == 4
    assert by_key[("clean_sheets", "MID")] == 1
    assert by_key[("defensive_contribution", "DEF")] == 2
    assert by_key[("defensive_contribution", "GKP")] == 0


def test_every_player_maps_without_loss(bootstrap):
    rows = loaders.load_players_state(bootstrap, 1, datetime.now(UTC))
    assert len(rows) == len(bootstrap["elements"])
    assert all(r["element_id"] is not None for r in rows)
    assert all(r["now_cost"] is not None for r in rows)


def test_defensive_contribution_is_always_one_of_the_two_position_rules(bootstrap):
    """DEF counts tackles+CBI; MID/FWD add recoveries; GKP is always 0.

    Reverse-engineered from season totals (FINDINGS.md caveat 3) because the API
    documents neither the formula nor the thresholds. Pinned here so that if the FPL
    definition shifts, it fails loudly rather than quietly skewing every DEFCON
    projection.
    """
    for element in bootstrap["elements"]:
        if element["minutes"] < 900:
            continue
        outfield_rules = {
            element["tackles"] + element["clearances_blocks_interceptions"],
            element["tackles"] + element["clearances_blocks_interceptions"] + element["recoveries"],
        }
        actual = element["defensive_contribution"]
        if element["element_type"] == 1:
            assert actual == 0, f"GKP {element['web_name']} should have DC 0"
        else:
            assert actual in outfield_rules, f"{element['web_name']} matches neither rule"


def test_reclassified_players_carry_totals_from_their_old_position(bootstrap):
    """The trap: `defensive_contribution` was accrued under the position the player
    held at the time, but `element_type` is their *current* listing. A player moved
    between DEF and MID over the summer therefore has a season total computed under
    the wrong rule for their new position.

    This matters because reclassified full-backs and wing-backs are exactly the
    profile where DEFCON drives value. Any per-90 rate for these players must be
    rebuilt from per-match components under the new rule, never read off the total.
    """
    wrong_rule = []
    for element in bootstrap["elements"]:
        if element["minutes"] < 900 or element["element_type"] == 1:
            continue
        tackles_cbi = element["tackles"] + element["clearances_blocks_interceptions"]
        with_recoveries = tackles_cbi + element["recoveries"]
        expected = tackles_cbi if element["element_type"] == 2 else with_recoveries
        if element["defensive_contribution"] != expected:
            wrong_rule.append(element["web_name"])

    # Small and identifiable, not a silent long tail. If this list grows a lot, the
    # reclassification handling needs revisiting before trusting DEFCON rates.
    assert len(wrong_rule) <= 15, f"unexpectedly many reclassified players: {wrong_rule}"


def test_squad_constraints_match_the_api(bootstrap):
    """Constants the optimiser will depend on, taken from the API not from memory."""
    rules = bootstrap["game_config"]["rules"]
    assert rules["squad_squadsize"] == 15
    assert rules["squad_squadplay"] == 11
    assert rules["squad_team_limit"] == 3
    assert rules["squad_total_spend"] == 1000
    assert rules["max_extra_free_transfers"] == 4      # 1 + 4 = 5 bankable
    assert rules["transfers_sell_on_fee"] == 0.5
    assert rules["element_sell_at_purchase_price"] is False

    by_type = {e["id"]: e for e in bootstrap["element_types"]}
    assert [by_type[i]["squad_select"] for i in (1, 2, 3, 4)] == [2, 5, 5, 3]
    assert [by_type[i]["squad_min_play"] for i in (1, 2, 3, 4)] == [1, 3, 2, 1]
    assert [by_type[i]["squad_max_play"] for i in (1, 2, 3, 4)] == [1, 5, 5, 3]


def test_chip_windows_are_two_sets_expiring_at_gw19(bootstrap):
    chips = bootstrap["chips"]
    first = {c["name"] for c in chips if c["stop_event"] == 19}
    second = {c["name"] for c in chips if c["start_event"] == 20}
    assert first == {"wildcard", "freehit", "bboost", "3xc"}
    assert second == {"wildcard", "freehit", "bboost", "3xc"}
    assert len(chips) == 8
