"""Map raw API JSON onto the schema.

Every field name here was read off the wire on 2026-08-05, not recalled. The API
returns several numerics as strings ("33.5") and several as floats; coercion is
centralised in the helpers so the schema stays the single source of truth for type.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Sequence

from .db import row_hash

POSITION_BY_TYPE = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
TYPE_BY_POSITION = {v: k for k, v in POSITION_BY_TYPE.items()}


# ------------------------------------------------------------------ coercion
def _i(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _b(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _ts(v: Any) -> str | None:
    return None if v in (None, "") else str(v)


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _finalise(rows: list[dict[str, Any]], snapshot_id: int, snapshot_at: datetime) -> list[dict[str, Any]]:
    for row in rows:
        row["row_hash"] = row_hash(row)
        row["snapshot_id"] = snapshot_id
        row["snapshot_at"] = snapshot_at
    return rows


# --------------------------------------------------------- bootstrap-static
def load_teams(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "team_id": _i(t["id"]), "code": _i(t.get("code")),
            "name": _s(t.get("name")), "short_name": _s(t.get("short_name")),
            "strength": _i(t.get("strength")),
            "strength_overall_home": _i(t.get("strength_overall_home")),
            "strength_overall_away": _i(t.get("strength_overall_away")),
            "strength_attack_home": _i(t.get("strength_attack_home")),
            "strength_attack_away": _i(t.get("strength_attack_away")),
            "strength_defence_home": _i(t.get("strength_defence_home")),
            "strength_defence_away": _i(t.get("strength_defence_away")),
            "played": _i(t.get("played")), "win": _i(t.get("win")),
            "draw": _i(t.get("draw")), "loss": _i(t.get("loss")),
            "points": _i(t.get("points")), "position": _i(t.get("position")),
            "form": _s(t.get("form")), "unavailable": _b(t.get("unavailable")),
            "pulse_id": _i(t.get("pulse_id")),
        }
        for t in data["teams"]
    ]
    return _finalise(rows, sid, at)


def load_element_types(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "element_type": _i(e["id"]),
            "singular_name_short": _s(e.get("singular_name_short")),
            "singular_name": _s(e.get("singular_name")),
            "plural_name": _s(e.get("plural_name")),
            "plural_name_short": _s(e.get("plural_name_short")),
            "squad_select": _i(e.get("squad_select")),
            "squad_min_play": _i(e.get("squad_min_play")),
            "squad_max_play": _i(e.get("squad_max_play")),
            "element_count": _i(e.get("element_count")),
        }
        for e in data["element_types"]
    ]
    return _finalise(rows, sid, at)


def load_events(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "event_id": _i(e["id"]), "name": _s(e.get("name")),
            "deadline_time": _ts(e.get("deadline_time")),
            "deadline_time_epoch": _i(e.get("deadline_time_epoch")),
            "finished": _b(e.get("finished")), "data_checked": _b(e.get("data_checked")),
            "is_current": _b(e.get("is_current")), "is_next": _b(e.get("is_next")),
            "is_previous": _b(e.get("is_previous")),
            "average_entry_score": _i(e.get("average_entry_score")),
            "highest_score": _i(e.get("highest_score")),
            "highest_scoring_entry": _i(e.get("highest_scoring_entry")),
            "ranked_count": _i(e.get("ranked_count")),
            "most_selected": _i(e.get("most_selected")),
            "most_captained": _i(e.get("most_captained")),
            "most_vice_captained": _i(e.get("most_vice_captained")),
            "most_transferred_in": _i(e.get("most_transferred_in")),
            "top_element": _i(e.get("top_element")),
            "top_element_info": _j(e.get("top_element_info")),
            "transfers_made": _i(e.get("transfers_made")),
            "chip_plays": _j(e.get("chip_plays")),
            "released": _b(e.get("released")),
            "release_time": _ts(e.get("release_time")),
            "overrides": _j(e.get("overrides")),
        }
        for e in data["events"]
    ]
    return _finalise(rows, sid, at)


def load_chips_config(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "chip_id": _i(c["id"]), "name": _s(c.get("name")),
            "chip_type": _s(c.get("chip_type")), "number": _i(c.get("number")),
            "start_event": _i(c.get("start_event")), "stop_event": _i(c.get("stop_event")),
            "overrides": _j(c.get("overrides")),
        }
        for c in data.get("chips", [])
    ]
    return _finalise(rows, sid, at)


def load_game_rules(data: dict, sid: int, at: datetime) -> list[dict]:
    rules = data.get("game_config", {}).get("rules", {}) or data.get("game_settings", {})
    rows = [{"rule_name": _s(k), "value": _j(v)} for k, v in rules.items()]
    return _finalise(rows, sid, at)


def load_scoring_rules(data: dict, sid: int, at: datetime) -> list[dict]:
    """Flatten scalars and position maps into one long-form table."""
    scoring = data.get("game_config", {}).get("scoring", {})
    rows: list[dict] = []
    for name, value in scoring.items():
        if isinstance(value, dict):
            for position, points in value.items():
                rows.append({"rule_name": _s(name), "position": _s(position), "points": _i(points)})
        else:
            rows.append({"rule_name": _s(name), "position": "ALL", "points": _i(value)})
    return _finalise(rows, sid, at)


def load_players_identity(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "element_id": _i(e["id"]), "code": _i(e.get("code")),
            "opta_code": _s(e.get("opta_code")),
            "first_name": _s(e.get("first_name")), "second_name": _s(e.get("second_name")),
            "web_name": _s(e.get("web_name")), "known_name": _s(e.get("known_name")),
            "team_id": _i(e.get("team")), "team_code": _i(e.get("team_code")),
            "element_type": _i(e.get("element_type")),
            "birth_date": _ts(e.get("birth_date")), "region": _i(e.get("region")),
            "squad_number": _i(e.get("squad_number")),
            "team_join_date": _ts(e.get("team_join_date")),
            "photo": _s(e.get("photo")),
            "has_temporary_code": _b(e.get("has_temporary_code")),
        }
        for e in data["elements"]
    ]
    return _finalise(rows, sid, at)


def load_players_state(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = []
    for e in data["elements"]:
        rows.append({
            "element_id": _i(e["id"]),
            "status": _s(e.get("status")), "news": _s(e.get("news")),
            "news_added": _ts(e.get("news_added")),
            "chance_of_playing_this_round": _i(e.get("chance_of_playing_this_round")),
            "chance_of_playing_next_round": _i(e.get("chance_of_playing_next_round")),
            "scout_news_link": _s(e.get("scout_news_link")),
            "scout_risks": _j(e.get("scout_risks")),
            "removed": _b(e.get("removed")), "can_select": _b(e.get("can_select")),
            "can_transact": _b(e.get("can_transact")),
            "now_cost": _i(e.get("now_cost")),
            "cost_change_event": _i(e.get("cost_change_event")),
            "cost_change_event_fall": _i(e.get("cost_change_event_fall")),
            "cost_change_start": _i(e.get("cost_change_start")),
            "cost_change_start_fall": _i(e.get("cost_change_start_fall")),
            "price_change_percent": _f(e.get("price_change_percent")),
            "penalties_order": _i(e.get("penalties_order")),
            "penalties_text": _s(e.get("penalties_text")),
            "direct_freekicks_order": _i(e.get("direct_freekicks_order")),
            "direct_freekicks_text": _s(e.get("direct_freekicks_text")),
            "corners_and_indirect_freekicks_order": _i(e.get("corners_and_indirect_freekicks_order")),
            "corners_and_indirect_freekicks_text": _s(e.get("corners_and_indirect_freekicks_text")),
            "minutes": _i(e.get("minutes")), "starts": _i(e.get("starts")),
            "starts_per_90": _f(e.get("starts_per_90")),
            "goals_scored": _i(e.get("goals_scored")), "assists": _i(e.get("assists")),
            "clean_sheets": _i(e.get("clean_sheets")),
            "clean_sheets_per_90": _f(e.get("clean_sheets_per_90")),
            "goals_conceded": _i(e.get("goals_conceded")),
            "goals_conceded_per_90": _f(e.get("goals_conceded_per_90")),
            "own_goals": _i(e.get("own_goals")),
            "penalties_saved": _i(e.get("penalties_saved")),
            "penalties_missed": _i(e.get("penalties_missed")),
            "yellow_cards": _i(e.get("yellow_cards")), "red_cards": _i(e.get("red_cards")),
            "saves": _i(e.get("saves")), "saves_per_90": _f(e.get("saves_per_90")),
            "bonus": _i(e.get("bonus")), "bps": _i(e.get("bps")),
            "tackles": _i(e.get("tackles")),
            "clearances_blocks_interceptions": _i(e.get("clearances_blocks_interceptions")),
            "recoveries": _i(e.get("recoveries")),
            "defensive_contribution": _i(e.get("defensive_contribution")),
            "defensive_contribution_per_90": _f(e.get("defensive_contribution_per_90")),
            "expected_goals": _f(e.get("expected_goals")),
            "expected_goals_per_90": _f(e.get("expected_goals_per_90")),
            "expected_assists": _f(e.get("expected_assists")),
            "expected_assists_per_90": _f(e.get("expected_assists_per_90")),
            "expected_goal_involvements": _f(e.get("expected_goal_involvements")),
            "expected_goal_involvements_per_90": _f(e.get("expected_goal_involvements_per_90")),
            "expected_goals_conceded": _f(e.get("expected_goals_conceded")),
            "expected_goals_conceded_per_90": _f(e.get("expected_goals_conceded_per_90")),
            "influence": _f(e.get("influence")), "creativity": _f(e.get("creativity")),
            "threat": _f(e.get("threat")), "ict_index": _f(e.get("ict_index")),
            "form": _f(e.get("form")), "value_form": _f(e.get("value_form")),
            "value_season": _f(e.get("value_season")),
            "points_per_game": _f(e.get("points_per_game")),
            "total_points": _i(e.get("total_points")),
            "event_points": _i(e.get("event_points")),
            "ep_this": _f(e.get("ep_this")), "ep_next": _f(e.get("ep_next")),
            "dreamteam_count": _i(e.get("dreamteam_count")),
            "in_dreamteam": _b(e.get("in_dreamteam")),
            "selected_by_percent": _f(e.get("selected_by_percent")),
            "transfers_in": _i(e.get("transfers_in")), "transfers_out": _i(e.get("transfers_out")),
            "transfers_in_event": _i(e.get("transfers_in_event")),
            "transfers_out_event": _i(e.get("transfers_out_event")),
            "now_cost_rank": _i(e.get("now_cost_rank")),
            "now_cost_rank_type": _i(e.get("now_cost_rank_type")),
            "form_rank": _i(e.get("form_rank")), "form_rank_type": _i(e.get("form_rank_type")),
            "points_per_game_rank": _i(e.get("points_per_game_rank")),
            "points_per_game_rank_type": _i(e.get("points_per_game_rank_type")),
            "selected_rank": _i(e.get("selected_rank")),
            "selected_rank_type": _i(e.get("selected_rank_type")),
            "influence_rank": _i(e.get("influence_rank")),
            "creativity_rank": _i(e.get("creativity_rank")),
            "threat_rank": _i(e.get("threat_rank")),
            "ict_index_rank": _i(e.get("ict_index_rank")),
        })
    return _finalise(rows, sid, at)


# ------------------------------------------------------------------ fixtures
def load_fixtures(data: list, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "fixture_id": _i(f["id"]), "code": _i(f.get("code")), "event": _i(f.get("event")),
            "kickoff_time": _ts(f.get("kickoff_time")),
            "provisional_start_time": _b(f.get("provisional_start_time")),
            "team_h": _i(f.get("team_h")), "team_a": _i(f.get("team_a")),
            "team_h_score": _i(f.get("team_h_score")), "team_a_score": _i(f.get("team_a_score")),
            "team_h_difficulty": _i(f.get("team_h_difficulty")),
            "team_a_difficulty": _i(f.get("team_a_difficulty")),
            "started": _b(f.get("started")), "finished": _b(f.get("finished")),
            "finished_provisional": _b(f.get("finished_provisional")),
            "minutes": _i(f.get("minutes")), "pulse_id": _i(f.get("pulse_id")),
        }
        for f in data
    ]
    return _finalise(rows, sid, at)


def load_fixture_stats(data: list, sid: int, at: datetime) -> list[dict]:
    """The post-match `stats` array. Empty pre-season; shape unverified (caveat 4)."""
    rows: list[dict] = []
    for fixture in data:
        for stat in fixture.get("stats") or []:
            identifier = stat.get("identifier")
            for side in ("h", "a"):
                for entry in stat.get(side) or []:
                    rows.append({
                        "snapshot_id": sid, "snapshot_at": at,
                        "fixture_id": _i(fixture["id"]), "identifier": _s(identifier),
                        "side": side, "element_id": _i(entry.get("element")),
                        "value": _i(entry.get("value")),
                    })
    return rows


# ----------------------------------------------------------- element-summary
def load_player_gw_history(summary: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "element_id": _i(h.get("element")), "fixture_id": _i(h.get("fixture")),
            "event": _i(h.get("round")), "opponent_team": _i(h.get("opponent_team")),
            "was_home": _b(h.get("was_home")), "kickoff_time": _ts(h.get("kickoff_time")),
            "team_h_score": _i(h.get("team_h_score")), "team_a_score": _i(h.get("team_a_score")),
            "minutes": _i(h.get("minutes")), "starts": _i(h.get("starts")),
            "goals_scored": _i(h.get("goals_scored")), "assists": _i(h.get("assists")),
            "clean_sheets": _i(h.get("clean_sheets")),
            "goals_conceded": _i(h.get("goals_conceded")), "own_goals": _i(h.get("own_goals")),
            "penalties_saved": _i(h.get("penalties_saved")),
            "penalties_missed": _i(h.get("penalties_missed")),
            "yellow_cards": _i(h.get("yellow_cards")), "red_cards": _i(h.get("red_cards")),
            "saves": _i(h.get("saves")), "bonus": _i(h.get("bonus")), "bps": _i(h.get("bps")),
            "tackles": _i(h.get("tackles")),
            "clearances_blocks_interceptions": _i(h.get("clearances_blocks_interceptions")),
            "recoveries": _i(h.get("recoveries")),
            "defensive_contribution": _i(h.get("defensive_contribution")),
            "influence": _f(h.get("influence")), "creativity": _f(h.get("creativity")),
            "threat": _f(h.get("threat")), "ict_index": _f(h.get("ict_index")),
            "expected_goals": _f(h.get("expected_goals")),
            "expected_assists": _f(h.get("expected_assists")),
            "expected_goal_involvements": _f(h.get("expected_goal_involvements")),
            "expected_goals_conceded": _f(h.get("expected_goals_conceded")),
            "total_points": _i(h.get("total_points")), "value": _i(h.get("value")),
            "selected": _i(h.get("selected")),
            "transfers_balance": _i(h.get("transfers_balance")),
            "transfers_in": _i(h.get("transfers_in")),
            "transfers_out": _i(h.get("transfers_out")),
            "modified": _b(h.get("modified")),
        }
        for h in summary.get("history", [])
    ]
    return _finalise(rows, sid, at)


def load_player_past_seasons(summary: dict, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "element_code": _i(p.get("element_code")), "season_name": _s(p.get("season_name")),
            "start_cost": _i(p.get("start_cost")), "end_cost": _i(p.get("end_cost")),
            "total_points": _i(p.get("total_points")), "minutes": _i(p.get("minutes")),
            "goals_scored": _i(p.get("goals_scored")), "assists": _i(p.get("assists")),
            "clean_sheets": _i(p.get("clean_sheets")),
            "goals_conceded": _i(p.get("goals_conceded")), "own_goals": _i(p.get("own_goals")),
            "penalties_saved": _i(p.get("penalties_saved")),
            "penalties_missed": _i(p.get("penalties_missed")),
            "yellow_cards": _i(p.get("yellow_cards")), "red_cards": _i(p.get("red_cards")),
            "saves": _i(p.get("saves")), "bonus": _i(p.get("bonus")), "bps": _i(p.get("bps")),
            "starts": _i(p.get("starts")), "tackles": _i(p.get("tackles")),
            "clearances_blocks_interceptions": _i(p.get("clearances_blocks_interceptions")),
            "recoveries": _i(p.get("recoveries")),
            "defensive_contribution": _i(p.get("defensive_contribution")),
            "influence": _f(p.get("influence")), "creativity": _f(p.get("creativity")),
            "threat": _f(p.get("threat")), "ict_index": _f(p.get("ict_index")),
            "expected_goals": _f(p.get("expected_goals")),
            "expected_assists": _f(p.get("expected_assists")),
            "expected_goal_involvements": _f(p.get("expected_goal_involvements")),
            "expected_goals_conceded": _f(p.get("expected_goals_conceded")),
        }
        for p in summary.get("history_past", [])
    ]
    return _finalise(rows, sid, at)


def load_player_upcoming_fixtures(summary: dict, element_id: int, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "element_id": element_id, "fixture_id": _i(f.get("id")),
            "event": _i(f.get("event")), "event_name": _s(f.get("event_name")),
            "team_h": _i(f.get("team_h")), "team_a": _i(f.get("team_a")),
            "is_home": _b(f.get("is_home")), "difficulty": _i(f.get("difficulty")),
            "kickoff_time": _ts(f.get("kickoff_time")),
            "finished": _b(f.get("finished")),
            "provisional_start_time": _b(f.get("provisional_start_time")),
        }
        for f in summary.get("fixtures", [])
    ]
    return _finalise(rows, sid, at)


# ------------------------------------------------------------------- my team
def load_my_entry(data: dict, sid: int, at: datetime) -> list[dict]:
    rows = [{
        "manager_id": _i(data.get("id")), "name": _s(data.get("name")),
        "player_first_name": _s(data.get("player_first_name")),
        "player_last_name": _s(data.get("player_last_name")),
        "player_region_name": _s(data.get("player_region_name")),
        "started_event": _i(data.get("started_event")),
        "current_event": _i(data.get("current_event")),
        "summary_overall_points": _i(data.get("summary_overall_points")),
        "summary_overall_rank": _i(data.get("summary_overall_rank")),
        "summary_event_points": _i(data.get("summary_event_points")),
        "summary_event_rank": _i(data.get("summary_event_rank")),
        "last_deadline_bank": _i(data.get("last_deadline_bank")),
        "last_deadline_value": _i(data.get("last_deadline_value")),
        "last_deadline_total_transfers": _i(data.get("last_deadline_total_transfers")),
        "years_active": _i(data.get("years_active")),
        "favourite_team": _i(data.get("favourite_team")),
        "joined_time": _ts(data.get("joined_time")),
    }]
    return _finalise(rows, sid, at)


def load_my_entry_history(data: dict, manager_id: int, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "manager_id": manager_id, "event": _i(h.get("event")),
            "points": _i(h.get("points")), "total_points": _i(h.get("total_points")),
            "rank": _i(h.get("rank")), "rank_sort": _i(h.get("rank_sort")),
            "overall_rank": _i(h.get("overall_rank")),
            "percentile_rank": _i(h.get("percentile_rank")),
            "bank": _i(h.get("bank")), "value": _i(h.get("value")),
            "event_transfers": _i(h.get("event_transfers")),
            "event_transfers_cost": _i(h.get("event_transfers_cost")),
            "points_on_bench": _i(h.get("points_on_bench")),
        }
        for h in data.get("current", [])
    ]
    return _finalise(rows, sid, at)


def load_my_past_seasons(data: dict, manager_id: int, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "manager_id": manager_id, "season_name": _s(p.get("season_name")),
            "total_points": _i(p.get("total_points")), "rank": _i(p.get("rank")),
            "rank_percentage": _s(p.get("rank_percentage")),
        }
        for p in data.get("past", [])
    ]
    return _finalise(rows, sid, at)


def load_my_chips(data: dict, manager_id: int, first_set_last_event: int,
                  sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "manager_id": manager_id, "chip_name": _s(c.get("name")),
            "event": _i(c.get("event")),
            "chip_set": 1 if (_i(c.get("event")) or 0) <= first_set_last_event else 2,
            "played_at": _ts(c.get("time")), "source": "api",
        }
        for c in data.get("chips", [])
    ]
    return _finalise(rows, sid, at)


def load_my_transfers(data: list, manager_id: int, sid: int, at: datetime) -> list[dict]:
    rows = [
        {
            "manager_id": manager_id, "event": _i(t.get("event")),
            "transfer_time": _ts(t.get("time")),
            "element_in": _i(t.get("element_in")),
            "element_in_cost": _i(t.get("element_in_cost")),
            "element_out": _i(t.get("element_out")),
            "element_out_cost": _i(t.get("element_out_cost")),
        }
        for t in data
    ]
    return _finalise(rows, sid, at)


def load_my_picks(data: dict, manager_id: int, event: int, sid: int, at: datetime) -> list[dict]:
    """Shape unverified: endpoint 404s until after the GW1 deadline (caveat 4)."""
    rows = [
        {
            "manager_id": manager_id, "event": event,
            "element_id": _i(p.get("element")), "position": _i(p.get("position")),
            "multiplier": _i(p.get("multiplier")),
            "is_captain": _b(p.get("is_captain")),
            "is_vice_captain": _b(p.get("is_vice_captain")),
            "purchase_price": _i(p.get("purchase_price")),
            "selling_price": _i(p.get("selling_price")),
            "source": "api",
        }
        for p in data.get("picks", [])
    ]
    return _finalise(rows, sid, at)


def load_league_standings(data: dict, league_id: int, sid: int, at: datetime) -> list[dict]:
    league_name = _s((data.get("league") or {}).get("name"))
    rows = [
        {
            "league_id": league_id, "league_name": league_name,
            "entry_id": _i(r.get("entry")), "entry_name": _s(r.get("entry_name")),
            "player_name": _s(r.get("player_name")),
            "rank": _i(r.get("rank")), "last_rank": _i(r.get("last_rank")),
            "rank_sort": _i(r.get("rank_sort")), "total": _i(r.get("total")),
            "event_total": _i(r.get("event_total")),
        }
        for r in ((data.get("standings") or {}).get("results") or [])
    ]
    return _finalise(rows, sid, at)


# ----------------------------------------------------------- live (unverified)
def load_event_live(data: dict, event: int, is_final: bool, sid: int, at: datetime) -> list[dict]:
    rows = []
    for element in data.get("elements", []):
        stats = element.get("stats", {})
        rows.append({
            "event": event, "element_id": _i(element.get("id")),
            "minutes": _i(stats.get("minutes")),
            "goals_scored": _i(stats.get("goals_scored")),
            "assists": _i(stats.get("assists")),
            "clean_sheets": _i(stats.get("clean_sheets")),
            "goals_conceded": _i(stats.get("goals_conceded")),
            "own_goals": _i(stats.get("own_goals")),
            "penalties_saved": _i(stats.get("penalties_saved")),
            "penalties_missed": _i(stats.get("penalties_missed")),
            "yellow_cards": _i(stats.get("yellow_cards")),
            "red_cards": _i(stats.get("red_cards")),
            "saves": _i(stats.get("saves")), "bonus": _i(stats.get("bonus")),
            "bps": _i(stats.get("bps")), "starts": _i(stats.get("starts")),
            "tackles": _i(stats.get("tackles")),
            "clearances_blocks_interceptions": _i(stats.get("clearances_blocks_interceptions")),
            "recoveries": _i(stats.get("recoveries")),
            "defensive_contribution": _i(stats.get("defensive_contribution")),
            "influence": _f(stats.get("influence")), "creativity": _f(stats.get("creativity")),
            "threat": _f(stats.get("threat")), "ict_index": _f(stats.get("ict_index")),
            "expected_goals": _f(stats.get("expected_goals")),
            "expected_assists": _f(stats.get("expected_assists")),
            "expected_goal_involvements": _f(stats.get("expected_goal_involvements")),
            "expected_goals_conceded": _f(stats.get("expected_goals_conceded")),
            "in_dreamteam": _b(element.get("in_dreamteam")),
            "total_points": _i(stats.get("total_points")),
            "is_final": is_final,
        })
    return _finalise(rows, sid, at)


def load_event_live_explain(data: dict, event: int, sid: int, at: datetime) -> list[dict]:
    rows: list[dict] = []
    for element in data.get("elements", []):
        for block in element.get("explain", []) or []:
            fixture_id = _i(block.get("fixture"))
            for detail in block.get("stats", []) or []:
                rows.append({
                    "snapshot_id": sid, "snapshot_at": at,
                    "event": event, "element_id": _i(element.get("id")),
                    "fixture_id": fixture_id,
                    "identifier": _s(detail.get("identifier")),
                    "value": _i(detail.get("value")),
                    "points": _i(detail.get("points")),
                    "points_modification": _i(detail.get("points_modification")),
                })
    return rows
