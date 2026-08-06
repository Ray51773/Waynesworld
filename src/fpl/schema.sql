-- FPL Optimiser schema. Append-only; nothing is ever UPDATEd in place.
-- Signed off from SCHEMA.md. Deviation noted at the bottom: the three change-detection
-- tables are views, not tables, because they are pure functions of players_state.

CREATE SEQUENCE IF NOT EXISTS seq_snapshot_id START 1;

-- ---------------------------------------------------------------- provenance
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id     BIGINT PRIMARY KEY,
  endpoint        VARCHAR NOT NULL,
  url             VARCHAR NOT NULL,
  params          JSON,
  fetched_at      TIMESTAMPTZ NOT NULL,
  http_status     SMALLINT NOT NULL,
  content_sha256  VARCHAR,
  raw_path        VARCHAR,
  bytes           BIGINT,
  duration_ms     INTEGER,
  content_changed BOOLEAN,
  from_cache      BOOLEAN
);

-- ------------------------------------------------------------ reference data
CREATE TABLE IF NOT EXISTS teams (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  team_id SMALLINT, code INTEGER, name VARCHAR, short_name VARCHAR,
  strength SMALLINT,
  strength_overall_home SMALLINT, strength_overall_away SMALLINT,
  strength_attack_home SMALLINT, strength_attack_away SMALLINT,
  strength_defence_home SMALLINT, strength_defence_away SMALLINT,
  played SMALLINT, win SMALLINT, draw SMALLINT, loss SMALLINT,
  points SMALLINT, position SMALLINT, form VARCHAR,
  unavailable BOOLEAN, pulse_id INTEGER
);

CREATE TABLE IF NOT EXISTS element_types (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_type SMALLINT, singular_name_short VARCHAR, singular_name VARCHAR,
  plural_name VARCHAR, plural_name_short VARCHAR,
  squad_select SMALLINT, squad_min_play SMALLINT, squad_max_play SMALLINT,
  element_count INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  event_id SMALLINT, name VARCHAR,
  deadline_time TIMESTAMPTZ, deadline_time_epoch BIGINT,
  finished BOOLEAN, data_checked BOOLEAN,
  is_current BOOLEAN, is_next BOOLEAN, is_previous BOOLEAN,
  average_entry_score INTEGER, highest_score INTEGER,
  highest_scoring_entry BIGINT, ranked_count INTEGER,
  most_selected INTEGER, most_captained INTEGER,
  most_vice_captained INTEGER, most_transferred_in INTEGER,
  top_element INTEGER, top_element_info JSON,
  transfers_made BIGINT, chip_plays JSON,
  released BOOLEAN, release_time TIMESTAMPTZ, overrides JSON
);

CREATE TABLE IF NOT EXISTS chips_config (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  chip_id SMALLINT, name VARCHAR, chip_type VARCHAR,
  number SMALLINT, start_event SMALLINT, stop_event SMALLINT,
  overrides JSON
);

CREATE TABLE IF NOT EXISTS game_rules (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  rule_name VARCHAR, value JSON
);

-- Long form: the API mixes scalars (assists: 3) with position maps
-- (goals_scored: {GKP:10,...}). Flattened here so the engine never branches on shape.
CREATE TABLE IF NOT EXISTS scoring_rules (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  rule_name VARCHAR, position VARCHAR, points INTEGER
);

-- ------------------------------------------------------------------- players
CREATE TABLE IF NOT EXISTS players_identity (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_id INTEGER, code INTEGER, opta_code VARCHAR,
  first_name VARCHAR, second_name VARCHAR, web_name VARCHAR, known_name VARCHAR,
  team_id SMALLINT, team_code INTEGER, element_type SMALLINT,
  birth_date DATE, region INTEGER, squad_number SMALLINT,
  team_join_date DATE, photo VARCHAR, has_temporary_code BOOLEAN
);

CREATE TABLE IF NOT EXISTS players_state (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_id INTEGER,
  status VARCHAR, news VARCHAR, news_added TIMESTAMPTZ,
  chance_of_playing_this_round SMALLINT, chance_of_playing_next_round SMALLINT,
  scout_news_link VARCHAR, scout_risks JSON,
  removed BOOLEAN, can_select BOOLEAN, can_transact BOOLEAN,
  now_cost SMALLINT, cost_change_event SMALLINT, cost_change_event_fall SMALLINT,
  cost_change_start SMALLINT, cost_change_start_fall SMALLINT,
  price_change_percent DECIMAL(8,3),
  penalties_order SMALLINT, penalties_text VARCHAR,
  direct_freekicks_order SMALLINT, direct_freekicks_text VARCHAR,
  corners_and_indirect_freekicks_order SMALLINT,
  corners_and_indirect_freekicks_text VARCHAR,
  minutes INTEGER, starts SMALLINT, starts_per_90 DECIMAL(8,3),
  goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, clean_sheets_per_90 DECIMAL(8,3),
  goals_conceded SMALLINT, goals_conceded_per_90 DECIMAL(8,3),
  own_goals SMALLINT, penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT,
  saves SMALLINT, saves_per_90 DECIMAL(8,3),
  bonus SMALLINT, bps INTEGER,
  tackles INTEGER, clearances_blocks_interceptions INTEGER, recoveries INTEGER,
  defensive_contribution INTEGER, defensive_contribution_per_90 DECIMAL(8,3),
  expected_goals DECIMAL(10,3), expected_goals_per_90 DECIMAL(8,3),
  expected_assists DECIMAL(10,3), expected_assists_per_90 DECIMAL(8,3),
  expected_goal_involvements DECIMAL(10,3), expected_goal_involvements_per_90 DECIMAL(8,3),
  expected_goals_conceded DECIMAL(10,3), expected_goals_conceded_per_90 DECIMAL(8,3),
  influence DECIMAL(10,2), creativity DECIMAL(10,2), threat DECIMAL(10,2), ict_index DECIMAL(10,2),
  form DECIMAL(8,2), value_form DECIMAL(8,2), value_season DECIMAL(10,2),
  points_per_game DECIMAL(8,2), total_points INTEGER, event_points INTEGER,
  ep_this DECIMAL(8,2), ep_next DECIMAL(8,2),
  dreamteam_count SMALLINT, in_dreamteam BOOLEAN,
  selected_by_percent DECIMAL(8,3),
  transfers_in BIGINT, transfers_out BIGINT,
  transfers_in_event BIGINT, transfers_out_event BIGINT,
  now_cost_rank INTEGER, now_cost_rank_type INTEGER,
  form_rank INTEGER, form_rank_type INTEGER,
  points_per_game_rank INTEGER, points_per_game_rank_type INTEGER,
  selected_rank INTEGER, selected_rank_type INTEGER,
  influence_rank INTEGER, creativity_rank INTEGER,
  threat_rank INTEGER, ict_index_rank INTEGER
);

-- ------------------------------------------------------------------ fixtures
CREATE TABLE IF NOT EXISTS fixtures (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  fixture_id INTEGER, code INTEGER, event SMALLINT,
  kickoff_time TIMESTAMPTZ, provisional_start_time BOOLEAN,
  team_h SMALLINT, team_a SMALLINT,
  team_h_score SMALLINT, team_a_score SMALLINT,
  team_h_difficulty SMALLINT, team_a_difficulty SMALLINT,
  started BOOLEAN, finished BOOLEAN, finished_provisional BOOLEAN,
  minutes SMALLINT, pulse_id INTEGER
);

CREATE TABLE IF NOT EXISTS fixture_stats (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  fixture_id INTEGER, identifier VARCHAR, side VARCHAR,
  element_id INTEGER, value INTEGER
);

-- ------------------------------------------------- per-player match history
CREATE TABLE IF NOT EXISTS player_gw_history (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_id INTEGER, fixture_id INTEGER, event SMALLINT,
  opponent_team SMALLINT, was_home BOOLEAN, kickoff_time TIMESTAMPTZ,
  team_h_score SMALLINT, team_a_score SMALLINT,
  minutes SMALLINT, starts SMALLINT,
  goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, goals_conceded SMALLINT, own_goals SMALLINT,
  penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT, saves SMALLINT,
  bonus SMALLINT, bps INTEGER,
  tackles INTEGER, clearances_blocks_interceptions INTEGER,
  recoveries INTEGER, defensive_contribution INTEGER,
  influence DECIMAL(10,2), creativity DECIMAL(10,2), threat DECIMAL(10,2), ict_index DECIMAL(10,2),
  expected_goals DECIMAL(10,3), expected_assists DECIMAL(10,3),
  expected_goal_involvements DECIMAL(10,3), expected_goals_conceded DECIMAL(10,3),
  total_points SMALLINT, value SMALLINT, selected BIGINT,
  transfers_balance BIGINT, transfers_in BIGINT, transfers_out BIGINT,
  modified BOOLEAN
);

CREATE TABLE IF NOT EXISTS player_past_seasons (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_code INTEGER, season_name VARCHAR,
  start_cost SMALLINT, end_cost SMALLINT, total_points INTEGER,
  minutes INTEGER, goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, goals_conceded SMALLINT, own_goals SMALLINT,
  penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT, saves SMALLINT,
  bonus SMALLINT, bps INTEGER, starts SMALLINT,
  tackles INTEGER, clearances_blocks_interceptions INTEGER,
  recoveries INTEGER, defensive_contribution INTEGER,
  influence DECIMAL(10,2), creativity DECIMAL(10,2), threat DECIMAL(10,2), ict_index DECIMAL(10,2),
  expected_goals DECIMAL(10,3), expected_assists DECIMAL(10,3),
  expected_goal_involvements DECIMAL(10,3), expected_goals_conceded DECIMAL(10,3)
);

CREATE TABLE IF NOT EXISTS player_upcoming_fixtures (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  element_id INTEGER, fixture_id INTEGER, event SMALLINT, event_name VARCHAR,
  team_h SMALLINT, team_a SMALLINT, is_home BOOLEAN,
  difficulty SMALLINT, kickoff_time TIMESTAMPTZ,
  finished BOOLEAN, provisional_start_time BOOLEAN
);

-- -------------------------------------------------------------- live scoring
-- Shape UNVERIFIED until GW1 completes. See FINDINGS.md caveat 4.
CREATE TABLE IF NOT EXISTS event_live (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  event SMALLINT, element_id INTEGER,
  minutes SMALLINT, goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, goals_conceded SMALLINT, own_goals SMALLINT,
  penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT, saves SMALLINT,
  bonus SMALLINT, bps INTEGER, starts SMALLINT,
  tackles INTEGER, clearances_blocks_interceptions INTEGER,
  recoveries INTEGER, defensive_contribution INTEGER,
  influence DECIMAL(10,2), creativity DECIMAL(10,2), threat DECIMAL(10,2), ict_index DECIMAL(10,2),
  expected_goals DECIMAL(10,3), expected_assists DECIMAL(10,3),
  expected_goal_involvements DECIMAL(10,3), expected_goals_conceded DECIMAL(10,3),
  in_dreamteam BOOLEAN, total_points SMALLINT, is_final BOOLEAN
);

CREATE TABLE IF NOT EXISTS event_live_explain (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  event SMALLINT, element_id INTEGER, fixture_id INTEGER,
  identifier VARCHAR, value INTEGER, points INTEGER, points_modification INTEGER
);

-- ---------------------------------------------------------- my team & state
CREATE TABLE IF NOT EXISTS my_entry (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, name VARCHAR,
  player_first_name VARCHAR, player_last_name VARCHAR,
  player_region_name VARCHAR,
  started_event SMALLINT, current_event SMALLINT,
  summary_overall_points INTEGER, summary_overall_rank BIGINT,
  summary_event_points INTEGER, summary_event_rank BIGINT,
  last_deadline_bank INTEGER, last_deadline_value INTEGER,
  last_deadline_total_transfers INTEGER, years_active SMALLINT,
  favourite_team SMALLINT, joined_time TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS my_picks (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, event SMALLINT, element_id INTEGER,
  position SMALLINT, multiplier SMALLINT,
  is_captain BOOLEAN, is_vice_captain BOOLEAN,
  purchase_price SMALLINT, selling_price SMALLINT,
  source VARCHAR
);

CREATE TABLE IF NOT EXISTS my_entry_history (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, event SMALLINT,
  points INTEGER, total_points INTEGER,
  rank BIGINT, rank_sort BIGINT, overall_rank BIGINT, percentile_rank SMALLINT,
  bank INTEGER, value INTEGER,
  event_transfers SMALLINT, event_transfers_cost SMALLINT,
  points_on_bench INTEGER
);

CREATE TABLE IF NOT EXISTS my_past_seasons (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, season_name VARCHAR,
  total_points INTEGER, rank BIGINT, rank_percentage VARCHAR
);

CREATE TABLE IF NOT EXISTS my_transfers (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, event SMALLINT, transfer_time TIMESTAMPTZ,
  element_in INTEGER, element_in_cost SMALLINT,
  element_out INTEGER, element_out_cost SMALLINT
);

CREATE TABLE IF NOT EXISTS my_chips (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  manager_id BIGINT, chip_name VARCHAR, event SMALLINT,
  chip_set SMALLINT, played_at TIMESTAMPTZ, source VARCHAR
);

-- Free transfers are exposed by no public endpoint (FINDINGS.md caveat 5).
-- Append-only so a correction never destroys the prior belief.
CREATE TABLE IF NOT EXISTS my_manual_state (
  manager_id BIGINT, event SMALLINT, recorded_at TIMESTAMPTZ,
  free_transfers SMALLINT, bank INTEGER, note VARCHAR
);

CREATE TABLE IF NOT EXISTS league_standings (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ, row_hash VARCHAR,
  league_id BIGINT, league_name VARCHAR,
  entry_id BIGINT, entry_name VARCHAR, player_name VARCHAR,
  rank INTEGER, last_rank INTEGER, rank_sort INTEGER,
  total INTEGER, event_total INTEGER
);

-- ---------------------------------------------------- hardcoded rule tables
-- The API supplies neither of these. See FINDINGS.md caveats 2 and 3.
CREATE TABLE IF NOT EXISTS defcon_thresholds (
  season VARCHAR, element_type SMALLINT,
  threshold SMALLINT, counts_recoveries BOOLEAN, points SMALLINT,
  source VARCHAR, verified_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS bps_weights (
  season VARCHAR, action VARCHAR, element_type SMALLINT,
  bps_value INTEGER, source VARCHAR,
  fitted_r2 DECIMAL(6,4), verified_at TIMESTAMPTZ
);

-- ------------------------------------------------------------ model outputs
CREATE TABLE IF NOT EXISTS model_runs (
  run_id BIGINT PRIMARY KEY, created_at TIMESTAMPTZ,
  model_version VARCHAR, git_sha VARCHAR,
  config JSON, as_of_snapshot_id BIGINT, as_of_deadline TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS minutes_projections (
  run_id BIGINT, element_id INTEGER, event SMALLINT,
  p_start DECIMAL(6,4), p_bench DECIMAL(6,4), p_unused DECIMAL(6,4),
  expected_minutes DECIMAL(6,2), p_60_plus DECIMAL(6,4),
  wc_returnee_flag BOOLEAN, rotation_risk DECIMAL(6,4)
);

CREATE TABLE IF NOT EXISTS team_ratings (
  run_id BIGINT, team_id SMALLINT, event SMALLINT,
  attack DECIMAL(10,4), defence DECIMAL(10,4), home_advantage DECIMAL(10,4),
  expected_goals_for DECIMAL(8,3), expected_goals_against DECIMAL(8,3)
);

CREATE TABLE IF NOT EXISTS xp_projections (
  run_id BIGINT, element_id INTEGER, event SMALLINT, horizon_index SMALLINT,
  xp_mean DECIMAL(9,3), xp_median DECIMAL(9,3),
  xp_p10 DECIMAL(9,3), xp_p90 DECIMAL(9,3),
  p_haul_10plus DECIMAL(6,4), p_blank_2minus DECIMAL(6,4),
  c_minutes DECIMAL(9,3), c_goals DECIMAL(9,3), c_assists DECIMAL(9,3),
  c_clean_sheet DECIMAL(9,3), c_defcon DECIMAL(9,3), c_bonus DECIMAL(9,3),
  c_saves DECIMAL(9,3), c_negatives DECIMAL(9,3),
  p_defcon_hit DECIMAL(6,4)
);

CREATE TABLE IF NOT EXISTS recommendations (
  run_id BIGINT, rec_id BIGINT, event SMALLINT,
  mode VARCHAR, rank SMALLINT, action JSON,
  ev_gain DECIMAL(9,3), ev_gain_net_of_hits DECIMAL(9,3),
  risk_posture VARCHAR, reasoning JSON
);

CREATE TABLE IF NOT EXISTS decision_log (
  manager_id BIGINT, event SMALLINT, decided_at TIMESTAMPTZ,
  recommended_rec_id BIGINT, action_taken JSON,
  followed_recommendation BOOLEAN,
  actual_points INTEGER, counterfactual_points INTEGER, note VARCHAR
);

CREATE TABLE IF NOT EXISTS accuracy_log (
  run_id BIGINT, element_id INTEGER, event SMALLINT,
  predicted_xp DECIMAL(9,3), actual_points SMALLINT,
  error DECIMAL(9,3), abs_error DECIMAL(9,3),
  element_type SMALLINT, price_band VARCHAR,
  predicted_p60 DECIMAL(6,4), actual_60plus BOOLEAN
);

CREATE TABLE IF NOT EXISTS watchlist (
  element_id INTEGER, added_at TIMESTAMPTZ,
  note VARCHAR, tags VARCHAR[], active BOOLEAN
);

-- ------------------------------------------------- historical (cold start)
-- Per-match rows from the vaastav dataset, the only source of gameweek-level
-- history until 2026/27 matches are played. `element_id` is the CURRENT season's
-- id where a name match was found, NULL where it was not.
CREATE TABLE IF NOT EXISTS hist_player_gw (
  season VARCHAR, name VARCHAR, name_key VARCHAR, element_id INTEGER,
  position VARCHAR, team VARCHAR, event SMALLINT, fixture_id INTEGER,
  opponent_team SMALLINT, was_home BOOLEAN, kickoff_time TIMESTAMPTZ,
  team_h_score SMALLINT, team_a_score SMALLINT,
  minutes SMALLINT, starts SMALLINT,
  goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, goals_conceded SMALLINT, own_goals SMALLINT,
  penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT, saves SMALLINT,
  bonus SMALLINT, bps INTEGER,
  tackles INTEGER, clearances_blocks_interceptions INTEGER,
  recoveries INTEGER, defensive_contribution INTEGER,
  expected_goals DECIMAL(10,3), expected_assists DECIMAL(10,3),
  expected_goal_involvements DECIMAL(10,3), expected_goals_conceded DECIMAL(10,3),
  total_points SMALLINT, value SMALLINT, selected BIGINT
);

-- Team-level match results derived from hist_player_gw, used to fit ratings.
CREATE TABLE IF NOT EXISTS hist_team_match (
  season VARCHAR, fixture_id INTEGER, event SMALLINT,
  team VARCHAR, opponent VARCHAR, was_home BOOLEAN,
  goals_for SMALLINT, goals_against SMALLINT,
  xg_for DECIMAL(10,3), xg_against DECIMAL(10,3)
);

-- Squad entered by hand, because picks are not public before the first deadline
-- and free transfers are exposed by no endpoint at all.
CREATE TABLE IF NOT EXISTS manual_squad (
  manager_id BIGINT, event SMALLINT, recorded_at TIMESTAMPTZ,
  element_id INTEGER, slot SMALLINT,
  is_captain BOOLEAN, is_vice_captain BOOLEAN,
  purchase_price SMALLINT, selling_price SMALLINT
);
