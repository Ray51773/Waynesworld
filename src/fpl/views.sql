-- Views are CREATE OR REPLACE'd on every connect, so they always match the code.

CREATE OR REPLACE VIEW latest_teams AS
  SELECT * FROM teams
  QUALIFY ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_element_types AS
  SELECT * FROM element_types
  QUALIFY ROW_NUMBER() OVER (PARTITION BY element_type ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_events AS
  SELECT * FROM events
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_players_identity AS
  SELECT * FROM players_identity
  QUALIFY ROW_NUMBER() OVER (PARTITION BY element_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_players_state AS
  SELECT * FROM players_state
  QUALIFY ROW_NUMBER() OVER (PARTITION BY element_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_fixtures AS
  SELECT * FROM fixtures
  QUALIFY ROW_NUMBER() OVER (PARTITION BY fixture_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_scoring_rules AS
  SELECT * FROM scoring_rules
  QUALIFY ROW_NUMBER() OVER (PARTITION BY rule_name, position ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_game_rules AS
  SELECT * FROM game_rules
  QUALIFY ROW_NUMBER() OVER (PARTITION BY rule_name ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_chips_config AS
  SELECT * FROM chips_config
  QUALIFY ROW_NUMBER() OVER (PARTITION BY chip_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_my_entry AS
  SELECT * FROM my_entry
  QUALIFY ROW_NUMBER() OVER (PARTITION BY manager_id ORDER BY snapshot_at DESC) = 1;

CREATE OR REPLACE VIEW latest_my_picks AS
  SELECT * FROM my_picks
  QUALIFY ROW_NUMBER() OVER (PARTITION BY manager_id, event, element_id ORDER BY snapshot_at DESC) = 1;

-- The everyday join: who a player is, what they cost, what shirt they wear.
CREATE OR REPLACE VIEW v_player AS
  SELECT
    i.element_id,
    i.code               AS element_code,
    i.web_name,
    i.first_name || ' ' || i.second_name AS full_name,
    et.singular_name_short AS position,
    i.element_type,
    t.short_name         AS team,
    t.name               AS team_name,
    i.team_id,
    s.now_cost / 10.0    AS price,
    s.now_cost,
    s.status,
    s.news,
    s.chance_of_playing_next_round,
    s.selected_by_percent,
    s.minutes, s.starts,
    s.total_points, s.points_per_game, s.form,
    s.expected_goals_per_90, s.expected_assists_per_90,
    s.expected_goal_involvements_per_90, s.expected_goals_conceded_per_90,
    s.defensive_contribution, s.defensive_contribution_per_90,
    s.tackles, s.clearances_blocks_interceptions, s.recoveries,
    s.bps, s.bonus, s.saves,
    s.yellow_cards, s.red_cards, s.own_goals,
    s.goals_scored, s.assists, s.clean_sheets, s.goals_conceded,
    s.penalties_order, s.direct_freekicks_order,
    s.corners_and_indirect_freekicks_order,
    s.transfers_in_event, s.transfers_out_event,
    s.cost_change_event, s.cost_change_start,
    s.ep_next, s.snapshot_at
  FROM latest_players_state s
  JOIN latest_players_identity i USING (element_id)
  JOIN latest_teams t ON t.team_id = i.team_id
  JOIN latest_element_types et ON et.element_type = i.element_type;

-- Rates derived from season totals. DEFCON actions are recomputed from components
-- under the player's CURRENT position rule, never read off `defensive_contribution`,
-- which was accrued under whatever position they held at the time (FINDINGS.md 3b).
-- `defcon_reclassified` flags the players where those two disagree.
CREATE OR REPLACE VIEW v_player_rates AS
  SELECT
    p.*,
    CASE
      WHEN p.element_type = 1 THEN 0
      WHEN p.element_type = 2 THEN p.tackles + p.clearances_blocks_interceptions
      ELSE p.tackles + p.clearances_blocks_interceptions + p.recoveries
    END AS defcon_actions,
    CASE WHEN p.minutes > 0 THEN
      ROUND(CASE
        WHEN p.element_type = 1 THEN 0
        WHEN p.element_type = 2 THEN p.tackles + p.clearances_blocks_interceptions
        ELSE p.tackles + p.clearances_blocks_interceptions + p.recoveries
      END / (p.minutes / 90.0), 2)
    END AS defcon_per_90,
    CASE WHEN p.element_type = 2 THEN 10 WHEN p.element_type = 1 THEN NULL ELSE 12 END
      AS defcon_threshold,
    p.defensive_contribution <> CASE
      WHEN p.element_type = 1 THEN 0
      WHEN p.element_type = 2 THEN p.tackles + p.clearances_blocks_interceptions
      ELSE p.tackles + p.clearances_blocks_interceptions + p.recoveries
    END AS defcon_reclassified,
    CASE WHEN p.now_cost > 0 THEN ROUND(p.total_points / (p.now_cost / 10.0), 1) END
      AS points_per_million,
    CASE WHEN p.minutes > 0 THEN ROUND(p.total_points / (p.minutes / 90.0), 2) END
      AS points_per_90
  FROM v_player p;

-- Fixture list with team names attached, one row per team per fixture.
CREATE OR REPLACE VIEW v_team_fixtures AS
  SELECT f.fixture_id, f.event, f.kickoff_time,
         f.team_h AS team_id, th.short_name AS team,
         f.team_a AS opponent_id, ta.short_name AS opponent,
         TRUE AS is_home, f.team_h_difficulty AS difficulty,
         f.finished, f.started
  FROM latest_fixtures f
  JOIN latest_teams th ON th.team_id = f.team_h
  JOIN latest_teams ta ON ta.team_id = f.team_a
  UNION ALL
  SELECT f.fixture_id, f.event, f.kickoff_time,
         f.team_a, ta.short_name,
         f.team_h, th.short_name,
         FALSE, f.team_a_difficulty,
         f.finished, f.started
  FROM latest_fixtures f
  JOIN latest_teams th ON th.team_id = f.team_h
  JOIN latest_teams ta ON ta.team_id = f.team_a;

-- ------------------------------------------------------- change detection
-- Pure functions of the players_state history, so they are views: no population
-- step to forget to run, and they can never drift from the underlying data.
-- (SCHEMA.md proposed tables; views are strictly better here. Acknowledgement
-- state, when we want it, goes in a separate small table keyed by detected_at.)

-- The `seq > 1` guard matters: without it every player's first-ever snapshot reads
-- as a change from NULL, and the genesis refresh emits 570 phantom alerts.

CREATE OR REPLACE VIEW set_piece_changes AS
  WITH stepped AS (
    SELECT element_id, snapshot_at,
           penalties_order, direct_freekicks_order,
           corners_and_indirect_freekicks_order,
           LAG(penalties_order) OVER w AS prev_pen,
           LAG(direct_freekicks_order) OVER w AS prev_fk,
           LAG(corners_and_indirect_freekicks_order) OVER w AS prev_ck,
           ROW_NUMBER() OVER w AS seq
    FROM players_state
    WINDOW w AS (PARTITION BY element_id ORDER BY snapshot_at)
  )
  SELECT snapshot_at AS detected_at, element_id, 'penalties_order' AS field,
         prev_pen AS old_value, penalties_order AS new_value
  FROM stepped WHERE seq > 1 AND prev_pen IS DISTINCT FROM penalties_order
  UNION ALL
  SELECT snapshot_at, element_id, 'direct_freekicks_order',
         prev_fk, direct_freekicks_order
  FROM stepped WHERE seq > 1 AND prev_fk IS DISTINCT FROM direct_freekicks_order
  UNION ALL
  SELECT snapshot_at, element_id, 'corners_and_indirect_freekicks_order',
         prev_ck, corners_and_indirect_freekicks_order
  FROM stepped WHERE seq > 1 AND prev_ck IS DISTINCT FROM corners_and_indirect_freekicks_order;

CREATE OR REPLACE VIEW availability_changes AS
  WITH stepped AS (
    SELECT element_id, snapshot_at, status, chance_of_playing_next_round, news,
           LAG(status) OVER w AS prev_status,
           LAG(chance_of_playing_next_round) OVER w AS prev_chance,
           LAG(news) OVER w AS prev_news,
           ROW_NUMBER() OVER w AS seq
    FROM players_state
    WINDOW w AS (PARTITION BY element_id ORDER BY snapshot_at)
  )
  SELECT snapshot_at AS detected_at, element_id,
         prev_status AS old_status, status AS new_status,
         prev_chance AS old_chance, chance_of_playing_next_round AS new_chance,
         news
  FROM stepped
  WHERE seq > 1
    AND (prev_status IS DISTINCT FROM status
      OR prev_chance IS DISTINCT FROM chance_of_playing_next_round
      OR prev_news IS DISTINCT FROM news);

CREATE OR REPLACE VIEW price_changes AS
  WITH stepped AS (
    SELECT element_id, snapshot_at, now_cost,
           transfers_in_event, transfers_out_event,
           LAG(now_cost) OVER w AS prev_cost,
           ROW_NUMBER() OVER w AS seq
    FROM players_state
    WINDOW w AS (PARTITION BY element_id ORDER BY snapshot_at)
  )
  SELECT snapshot_at AS detected_at, element_id,
         prev_cost AS old_cost, now_cost AS new_cost,
         now_cost - prev_cost AS delta,
         transfers_in_event - transfers_out_event AS net_transfers_at_change
  FROM stepped
  WHERE seq > 1 AND prev_cost IS DISTINCT FROM now_cost;
