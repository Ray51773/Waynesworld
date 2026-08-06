# DuckDB Schema Proposal — FPL Optimiser 2026/27

Status: **awaiting sign-off**. Derived from live API inspection on 2026-08-05, not from
remembered field names. Every table carries `snapshot_at`; nothing is ever updated in place.

## Conventions

- `snapshot_id BIGINT` — FK to `snapshots`, identifies the fetch that produced the row.
- `snapshot_at TIMESTAMPTZ` — denormalised onto every table so time-series queries never
  need the join.
- **Append-on-change**: rows are appended only when the content hash of that entity
  differs from its previous version. A pure append-every-hour design would write ~13.6k
  player rows/hour (570 players × 24) of which >99% are identical outside price-change
  windows. *Judgement call — flagged, not settled. Say the word and I'll append
  unconditionally instead.*
- Money is stored in FPL integer tenths (`now_cost = 60` means £6.0m). No floats for money.
- `latest_*` views select the most recent version per entity key.

---

## 1. Provenance

```sql
CREATE TABLE snapshots (
  snapshot_id      BIGINT PRIMARY KEY,      -- monotonic sequence
  endpoint         VARCHAR NOT NULL,        -- 'bootstrap-static', 'fixtures', 'element-summary', ...
  url              VARCHAR NOT NULL,
  params           JSON,                    -- {"player_id": 411} / {"event": 3}
  fetched_at       TIMESTAMPTZ NOT NULL,
  http_status      SMALLINT NOT NULL,
  content_sha256   VARCHAR NOT NULL,
  raw_path         VARCHAR NOT NULL,        -- data/snapshots/{endpoint}/{iso}.json.gz
  bytes            BIGINT,
  duration_ms      INTEGER,
  content_changed  BOOLEAN                  -- vs previous snapshot of same endpoint+params
);
```

Raw gzipped JSON always lands on disk **before** any parsing, so a schema bug can never
lose a fetch.

---

## 2. Reference data (from `bootstrap-static/`)

```sql
CREATE TABLE teams (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  team_id SMALLINT, code INTEGER, name VARCHAR, short_name VARCHAR,
  strength SMALLINT,                    -- NULL pre-season, see caveat
  strength_overall_home SMALLINT, strength_overall_away SMALLINT,
  strength_attack_home  SMALLINT, strength_attack_away  SMALLINT,
  strength_defence_home SMALLINT, strength_defence_away SMALLINT,
  played SMALLINT, win SMALLINT, draw SMALLINT, loss SMALLINT,
  points SMALLINT, position SMALLINT, form VARCHAR,
  unavailable BOOLEAN, pulse_id INTEGER,
  PRIMARY KEY (snapshot_id, team_id)
);

CREATE TABLE element_types (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  element_type SMALLINT, singular_name_short VARCHAR,  -- GKP/DEF/MID/FWD
  plural_name VARCHAR, squad_select SMALLINT,
  squad_min_play SMALLINT, squad_max_play SMALLINT,
  element_count INTEGER,
  PRIMARY KEY (snapshot_id, element_type)
);

CREATE TABLE events (                    -- gameweeks
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
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
  released BOOLEAN, release_time TIMESTAMPTZ, overrides JSON,
  PRIMARY KEY (snapshot_id, event_id)
);

CREATE TABLE chips_config (              -- the two-set structure, straight from the API
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  chip_id SMALLINT, name VARCHAR,        -- wildcard | freehit | bboost | 3xc
  chip_type VARCHAR,                     -- transfer | team
  start_event SMALLINT, stop_event SMALLINT,
  overrides JSON,
  PRIMARY KEY (snapshot_id, chip_id)
);

CREATE TABLE game_rules (                -- long form of game_config.rules
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  rule_name VARCHAR, value JSON,
  PRIMARY KEY (snapshot_id, rule_name)
);

CREATE TABLE scoring_rules (             -- long form of game_config.scoring — drives the engine
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  rule_name VARCHAR,                     -- goals_scored, clean_sheets, defensive_contribution, ...
  position VARCHAR,                      -- 'GKP'|'DEF'|'MID'|'FWD', or 'ALL' for scalars
  points INTEGER,
  PRIMARY KEY (snapshot_id, rule_name, position)
);
```

`scoring_rules` is deliberately long-form: the API returns some rules as scalars
(`assists: 3`) and some as position maps (`goals_scored: {GKP:10, DEF:6, MID:5, FWD:4}`).
Flattening at load time means the scoring engine never branches on shape.

---

## 3. Players

Split into slowly-changing identity and fast-moving state, because ownership and price
churn hourly while names and birth dates do not.

```sql
CREATE TABLE players_identity (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  element_id INTEGER, code INTEGER, opta_code VARCHAR,
  first_name VARCHAR, second_name VARCHAR, web_name VARCHAR, known_name VARCHAR,
  team_id SMALLINT, team_code INTEGER, element_type SMALLINT,
  birth_date DATE, region INTEGER, squad_number SMALLINT,
  team_join_date DATE, photo VARCHAR, has_temporary_code BOOLEAN,
  PRIMARY KEY (snapshot_id, element_id)
);

CREATE TABLE players_state (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  element_id INTEGER,
  -- availability
  status VARCHAR,                        -- a|d|i|s|u|n
  news VARCHAR, news_added TIMESTAMPTZ,
  chance_of_playing_this_round SMALLINT, chance_of_playing_next_round SMALLINT,
  scout_news_link VARCHAR, scout_risks JSON,
  removed BOOLEAN, can_select BOOLEAN, can_transact BOOLEAN,
  -- price & ownership
  now_cost SMALLINT, cost_change_event SMALLINT, cost_change_event_fall SMALLINT,
  cost_change_start SMALLINT, cost_change_start_fall SMALLINT,
  price_change_percent VARCHAR,
  selected_by_percent DECIMAL(6,3),
  transfers_in BIGINT, transfers_out BIGINT,
  transfers_in_event BIGINT, transfers_out_event BIGINT,
  -- set pieces  ** high-priority alerting source **
  penalties_order SMALLINT, penalties_text VARCHAR,
  direct_freekicks_order SMALLINT, direct_freekicks_text VARCHAR,
  corners_and_indirect_freekicks_order SMALLINT,
  corners_and_indirect_freekicks_text VARCHAR,
  -- season cumulative
  minutes INTEGER, starts SMALLINT, starts_per_90 DECIMAL(6,3),
  goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, clean_sheets_per_90 DECIMAL(6,3),
  goals_conceded SMALLINT, goals_conceded_per_90 DECIMAL(6,3),
  own_goals SMALLINT, penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT,
  saves SMALLINT, saves_per_90 DECIMAL(6,3),
  bonus SMALLINT, bps INTEGER,
  -- defensive contribution components
  tackles INTEGER, clearances_blocks_interceptions INTEGER, recoveries INTEGER,
  defensive_contribution INTEGER, defensive_contribution_per_90 DECIMAL(6,3),
  -- expected stats
  expected_goals DECIMAL(8,3), expected_goals_per_90 DECIMAL(6,3),
  expected_assists DECIMAL(8,3), expected_assists_per_90 DECIMAL(6,3),
  expected_goal_involvements DECIMAL(8,3), expected_goal_involvements_per_90 DECIMAL(6,3),
  expected_goals_conceded DECIMAL(8,3), expected_goals_conceded_per_90 DECIMAL(6,3),
  -- indices & form
  influence DECIMAL(8,2), creativity DECIMAL(8,2), threat DECIMAL(8,2), ict_index DECIMAL(8,2),
  form DECIMAL(6,2), value_form DECIMAL(6,2), value_season DECIMAL(8,2),
  points_per_game DECIMAL(6,2), total_points INTEGER, event_points INTEGER,
  ep_this DECIMAL(6,2), ep_next DECIMAL(6,2),
  dreamteam_count SMALLINT, in_dreamteam BOOLEAN,
  -- ranks (kept, cheap, useful for percentile features)
  now_cost_rank INTEGER, now_cost_rank_type INTEGER,
  form_rank INTEGER, form_rank_type INTEGER,
  points_per_game_rank INTEGER, points_per_game_rank_type INTEGER,
  selected_rank INTEGER, selected_rank_type INTEGER,
  influence_rank INTEGER, creativity_rank INTEGER, threat_rank INTEGER, ict_index_rank INTEGER,
  PRIMARY KEY (snapshot_id, element_id)
);
```

Note the API returns `expected_goals`, `influence` etc. as **strings** and the `_per_90`
variants as floats. Loader casts both to DECIMAL; the schema is the single source of truth
for type, not the API.

---

## 4. Fixtures

```sql
CREATE TABLE fixtures (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  fixture_id INTEGER, code INTEGER, event SMALLINT,
  kickoff_time TIMESTAMPTZ, provisional_start_time BOOLEAN,
  team_h SMALLINT, team_a SMALLINT,
  team_h_score SMALLINT, team_a_score SMALLINT,
  team_h_difficulty SMALLINT, team_a_difficulty SMALLINT,
  started BOOLEAN, finished BOOLEAN, finished_provisional BOOLEAN,
  minutes SMALLINT, pulse_id INTEGER,
  PRIMARY KEY (snapshot_id, fixture_id)
);

CREATE TABLE fixture_stats (             -- the post-match `stats` array, long form
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  fixture_id INTEGER,
  identifier VARCHAR,                    -- goals_scored, assists, bps, saves, ...
  side CHAR(1),                          -- 'h' | 'a'
  element_id INTEGER, value INTEGER,
  PRIMARY KEY (snapshot_id, fixture_id, identifier, side, element_id)
);
```

---

## 5. Per-player history (from `element-summary/{id}/`)

```sql
CREATE TABLE player_gw_history (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  element_id INTEGER, fixture_id INTEGER, event SMALLINT,   -- API calls it `round`
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
  influence DECIMAL(8,2), creativity DECIMAL(8,2), threat DECIMAL(8,2), ict_index DECIMAL(8,2),
  expected_goals DECIMAL(8,3), expected_assists DECIMAL(8,3),
  expected_goal_involvements DECIMAL(8,3), expected_goals_conceded DECIMAL(8,3),
  total_points SMALLINT, value SMALLINT, selected BIGINT,
  transfers_balance BIGINT, transfers_in BIGINT, transfers_out BIGINT,
  modified BOOLEAN,
  PRIMARY KEY (snapshot_id, element_id, fixture_id)
);

CREATE TABLE player_past_seasons (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  element_code INTEGER, season_name VARCHAR,
  start_cost SMALLINT, end_cost SMALLINT, total_points INTEGER,
  minutes INTEGER, goals_scored SMALLINT, assists SMALLINT,
  clean_sheets SMALLINT, goals_conceded SMALLINT, own_goals SMALLINT,
  penalties_saved SMALLINT, penalties_missed SMALLINT,
  yellow_cards SMALLINT, red_cards SMALLINT, saves SMALLINT,
  bonus SMALLINT, bps INTEGER, starts SMALLINT,
  tackles INTEGER, clearances_blocks_interceptions INTEGER,
  recoveries INTEGER, defensive_contribution INTEGER,
  influence DECIMAL(8,2), creativity DECIMAL(8,2), threat DECIMAL(8,2), ict_index DECIMAL(8,2),
  expected_goals DECIMAL(8,3), expected_assists DECIMAL(8,3),
  expected_goal_involvements DECIMAL(8,3), expected_goals_conceded DECIMAL(8,3),
  PRIMARY KEY (snapshot_id, element_code, season_name)
);
```

Keyed on `element_code` (the permanent cross-season player code), not `element_id`, which
is reassigned each season.

---

## 6. Live scoring (from `event/{gw}/live/`)

```sql
CREATE TABLE event_live (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  event SMALLINT, element_id INTEGER,
  -- same stat columns as player_gw_history
  minutes SMALLINT, goals_scored SMALLINT, ... , total_points SMALLINT,
  is_final BOOLEAN,                      -- derived: events.data_checked
  PRIMARY KEY (snapshot_id, event, element_id)
);

CREATE TABLE event_live_explain (        -- per-fixture point attribution
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  event SMALLINT, element_id INTEGER, fixture_id INTEGER,
  identifier VARCHAR, value INTEGER, points INTEGER, points_modification INTEGER,
  PRIMARY KEY (snapshot_id, event, element_id, fixture_id, identifier)
);
```

`event_live_explain` is the ground truth for Milestone 2: it states, per player per
category, exactly how many points the game awarded. Verifying the engine against this is
stronger than verifying against `total_points` alone, because it localises any mismatch to
a category. **Shape unverified — see caveat 4.**

---

## 7. My team & state

```sql
CREATE TABLE my_entry (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  manager_id BIGINT, name VARCHAR,
  player_first_name VARCHAR, player_last_name VARCHAR,
  started_event SMALLINT, current_event SMALLINT,
  summary_overall_points INTEGER, summary_overall_rank BIGINT,
  summary_event_points INTEGER, summary_event_rank BIGINT,
  last_deadline_bank INTEGER, last_deadline_value INTEGER,
  last_deadline_total_transfers INTEGER, years_active SMALLINT,
  favourite_team SMALLINT,
  PRIMARY KEY (snapshot_id, manager_id)
);

CREATE TABLE my_picks (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  manager_id BIGINT, event SMALLINT, element_id INTEGER,
  position SMALLINT,                     -- 1..15, 1..11 = starting XI
  multiplier SMALLINT,                   -- 0 bench, 1 starter, 2 captain, 3 TC
  is_captain BOOLEAN, is_vice_captain BOOLEAN,
  purchase_price SMALLINT,               -- tracked by us, needed for sell-on rule
  selling_price SMALLINT,
  source VARCHAR,                        -- 'api' | 'manual'
  PRIMARY KEY (snapshot_id, manager_id, event, element_id)
);

CREATE TABLE my_entry_history (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  manager_id BIGINT, event SMALLINT,
  points INTEGER, total_points INTEGER,
  rank BIGINT, rank_sort BIGINT, overall_rank BIGINT, percentile_rank SMALLINT,
  bank INTEGER, value INTEGER,
  event_transfers SMALLINT, event_transfers_cost SMALLINT,
  points_on_bench INTEGER,
  PRIMARY KEY (snapshot_id, manager_id, event)
);

CREATE TABLE my_transfers (
  snapshot_id BIGINT, snapshot_at TIMESTAMPTZ,
  manager_id BIGINT, event SMALLINT, transfer_time TIMESTAMPTZ,
  element_in INTEGER, element_in_cost SMALLINT,
  element_out INTEGER, element_out_cost SMALLINT,
  PRIMARY KEY (manager_id, event, element_in, element_out, transfer_time)
);

CREATE TABLE my_chips (
  manager_id BIGINT, chip_name VARCHAR, event SMALLINT,
  chip_set SMALLINT,                     -- 1 = GW1-19, 2 = GW20-38
  played_at TIMESTAMPTZ, source VARCHAR,
  PRIMARY KEY (manager_id, chip_name, chip_set)
);

CREATE TABLE my_manual_state (            -- the API will not tell us free transfers
  manager_id BIGINT, event SMALLINT, recorded_at TIMESTAMPTZ,
  free_transfers SMALLINT, bank INTEGER, note VARCHAR,
  PRIMARY KEY (manager_id, event, recorded_at)
);
```

`my_manual_state` exists because **free transfers are not exposed by any public
endpoint** (caveat 5). It is append-only so a correction never destroys the prior belief.

---

## 8. Model outputs

```sql
CREATE TABLE model_runs (
  run_id BIGINT PRIMARY KEY, created_at TIMESTAMPTZ,
  model_version VARCHAR, git_sha VARCHAR,
  config JSON, as_of_snapshot_id BIGINT,
  as_of_deadline TIMESTAMPTZ            -- leakage guard for backtests
);

CREATE TABLE minutes_projections (
  run_id BIGINT, element_id INTEGER, event SMALLINT,
  p_start DECIMAL(5,4), p_bench DECIMAL(5,4), p_unused DECIMAL(5,4),
  expected_minutes DECIMAL(5,2), p_60_plus DECIMAL(5,4),
  wc_returnee_flag BOOLEAN, rotation_risk DECIMAL(5,4),
  PRIMARY KEY (run_id, element_id, event)
);

CREATE TABLE team_ratings (
  run_id BIGINT, team_id SMALLINT, event SMALLINT,
  attack DECIMAL(8,4), defence DECIMAL(8,4), home_advantage DECIMAL(8,4),
  expected_goals_for DECIMAL(6,3), expected_goals_against DECIMAL(6,3),
  PRIMARY KEY (run_id, team_id, event)
);

CREATE TABLE xp_projections (
  run_id BIGINT, element_id INTEGER, event SMALLINT, horizon_index SMALLINT,
  xp_mean DECIMAL(7,3), xp_median DECIMAL(7,3),
  xp_p10 DECIMAL(7,3), xp_p90 DECIMAL(7,3),
  p_haul_10plus DECIMAL(5,4), p_blank_2minus DECIMAL(5,4),
  -- mandatory component breakdown
  c_minutes DECIMAL(7,3), c_goals DECIMAL(7,3), c_assists DECIMAL(7,3),
  c_clean_sheet DECIMAL(7,3), c_defcon DECIMAL(7,3), c_bonus DECIMAL(7,3),
  c_saves DECIMAL(7,3), c_negatives DECIMAL(7,3),
  p_defcon_hit DECIMAL(5,4),
  PRIMARY KEY (run_id, element_id, event)
);

CREATE TABLE recommendations (
  run_id BIGINT, rec_id BIGINT, event SMALLINT,
  mode VARCHAR,                          -- transfers|captain|chips|wildcard|freehit
  rank SMALLINT, action JSON,
  ev_gain DECIMAL(7,3), ev_gain_net_of_hits DECIMAL(7,3),
  risk_posture VARCHAR, reasoning JSON,  -- named components driving it
  PRIMARY KEY (run_id, rec_id)
);

CREATE TABLE decision_log (              -- what I actually did, vs what was advised
  manager_id BIGINT, event SMALLINT, decided_at TIMESTAMPTZ,
  recommended_rec_id BIGINT, action_taken JSON,
  followed_recommendation BOOLEAN,
  actual_points INTEGER, counterfactual_points INTEGER, note VARCHAR,
  PRIMARY KEY (manager_id, event, decided_at)
);

CREATE TABLE accuracy_log (
  run_id BIGINT, element_id INTEGER, event SMALLINT,
  predicted_xp DECIMAL(7,3), actual_points SMALLINT,
  error DECIMAL(7,3), abs_error DECIMAL(7,3),
  element_type SMALLINT, price_band VARCHAR,
  predicted_p60 DECIMAL(5,4), actual_60plus BOOLEAN,
  PRIMARY KEY (run_id, element_id, event)
);

CREATE TABLE watchlist (
  element_id INTEGER, added_at TIMESTAMPTZ,
  note VARCHAR, tags VARCHAR[], active BOOLEAN,
  PRIMARY KEY (element_id, added_at)
);
```

---

## 9. Derived alert tables

Populated by diffing consecutive `players_state` versions:

```sql
CREATE TABLE set_piece_changes (
  detected_at TIMESTAMPTZ, element_id INTEGER, field VARCHAR,
  old_value SMALLINT, new_value SMALLINT, acknowledged BOOLEAN,
  PRIMARY KEY (element_id, field, detected_at)
);

CREATE TABLE availability_changes (
  detected_at TIMESTAMPTZ, element_id INTEGER,
  old_status VARCHAR, new_status VARCHAR,
  old_chance SMALLINT, new_chance SMALLINT,
  news VARCHAR, acknowledged BOOLEAN,
  PRIMARY KEY (element_id, detected_at)
);

CREATE TABLE price_changes (
  detected_at TIMESTAMPTZ, element_id INTEGER,
  old_cost SMALLINT, new_cost SMALLINT, event SMALLINT,
  net_transfers_at_change BIGINT,
  PRIMARY KEY (element_id, detected_at)
);
```

---

## 10. Hardcoded rule tables (API does not supply these)

```sql
CREATE TABLE defcon_thresholds (          -- seeded from official rules, version-stamped
  season VARCHAR, element_type SMALLINT,
  threshold SMALLINT,                     -- DEF 10, MID/FWD 12, GKP NULL
  counts_recoveries BOOLEAN,              -- FALSE for DEF, TRUE for MID/FWD
  points SMALLINT, source VARCHAR, verified_at TIMESTAMPTZ,
  PRIMARY KEY (season, element_type)
);

CREATE TABLE bps_weights (
  season VARCHAR, action VARCHAR, element_type SMALLINT,  -- NULL = all positions
  bps_value INTEGER,
  source VARCHAR,                         -- 'official_rules' | 'fitted'
  fitted_r2 DECIMAL(5,4), verified_at TIMESTAMPTZ,
  PRIMARY KEY (season, action, element_type)
);
```

Both are marked `source` so the tool can always say whether a number came from the
published rules or was fitted from observed matches. See caveats 2 and 3.

---

## 11. Views

```sql
CREATE VIEW latest_players AS SELECT * FROM players_state
  QUALIFY ROW_NUMBER() OVER (PARTITION BY element_id ORDER BY snapshot_at DESC) = 1;
-- likewise latest_teams, latest_events, latest_fixtures, latest_my_picks

CREATE VIEW v_player AS   -- identity + state + position + team, the everyday join
  SELECT ... FROM latest_players p
  JOIN latest_players_identity i USING (element_id)
  JOIN latest_teams t ON t.team_id = i.team_id
  JOIN latest_element_types et ON et.element_type = i.element_type;
```

---

## Storage estimate

Append-on-change, one refresh/hour for a full season: `players_state` is the only table
with real churn. Prices move once daily for ~30-60 players; ownership drifts continuously,
so realistically most players change every snapshot. Worst case ~570 × 24 × 300 days ≈
4.1M rows × ~150 bytes ≈ **600 MB**, comfortably inside DuckDB's range and Parquet will
compress it hard given the column redundancy. If we drop the four hourly-churning
ownership columns into a narrow `players_ownership` table, the wide table's change rate
collapses. *I'd recommend that split — say the word and I'll fold it in.*

---

## Open questions for sign-off

1. Append-on-change vs append-always for `players_state`?
2. Split hourly-churn ownership columns into their own narrow table?
3. Keep the `*_rank` columns (cheap, ~12 extra ints) or drop as derivable?
4. `FPL_MANAGER_ID` — needed before Milestone 1 can print your squad.
