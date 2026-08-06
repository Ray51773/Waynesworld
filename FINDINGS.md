# API Reconnaissance — 2026-08-05

Live inspection of the FPL API, 16 days before the GW1 deadline. Raw payloads are in
`data/raw_inspect/`. Everything below was read off the wire, not recalled.

## Endpoint status

| Endpoint | HTTP | Usable now? |
|---|---|---|
| `bootstrap-static/` | 200 | Yes — 570 players, 20 teams, 38 events, full rules |
| `fixtures/` | 200 | Yes — all 380 fixtures scheduled, GW1-38, `stats` empty |
| `element-summary/{id}/` | 200 | Partly — `fixtures` + `history_past` populated, `history` empty |
| `event/{gw}/live/` | 200 | Empty (`{"elements":[]}`) until matches start |
| `entry/{id}/` | 200 | Yes, but season fields null pre-season |
| `entry/{id}/history/` | 200 | `past` populated; `current` and `chips` empty |
| `entry/{id}/transfers/` | 200 | Empty array pre-season |
| `entry/{id}/event/{gw}/picks/` | **404** | Not until after the GW1 deadline |
| `leagues-classic/{id}/standings/` | 200 | Shape confirmed, `results` empty |

Confirmed against the spec: GW1 deadline is `2026-08-21T17:30:00Z`, first kickoff
`2026-08-21T19:00:00Z` — exactly 90 minutes, as stated.

## Caveats (referenced by SCHEMA.md)

### Caveat 1 — Team strength ratings are all zero pre-season

`strength` is `null` and `strength_attack_home/away`, `strength_defence_home/away` are all
`0` for every team. The spec proposes these as the starting prior for the clean sheet
model. **That prior is unavailable.** `strength_overall_home/away` *is* populated (Arsenal
4/5), but it is a single coarse number with no attack/defence split.

Consequence: the Dixon-Coles model must be fitted from historical results and xG from the
outset — it is the primary path, not the "or better" upgrade the spec frames it as. For the
three promoted sides this means fitting from Championship data or applying a promoted-team
prior. I will flag that as a modelling judgement call when we get there.

### Caveat 2 — BPS weightings are NOT readable from the API

The spec says to pull 2026/27 BPS weightings "from the game settings rather than
hardcoded". I searched every key in the entire bootstrap payload: there is no BPS weighting
table. `game_config.scoring.bps = 0` is the points-per-BPS conversion (zero, since BPS
converts to bonus, not points directly), not a weighting map.

There is no way to avoid seeding these from the published rules. Worse, they cannot be
fitted from historical data either — 2025/26 matches were played under the **old** BPS
rules, which the spec itself says to distrust. So:

- Seed `bps_weights` from the official 2026/27 rules page, `source = 'official_rules'`.
- After roughly GW3-5, refit by regression against observed per-match `bps` and flag any
  weight where the fitted value disagrees with the seeded one.
- Until that refit lands, every bonus projection carries real uncertainty. The tool should
  say so rather than presenting bonus xP with false precision.

### Caveat 3 — DEFCON thresholds are not in the API either, but the components are

`game_config.scoring.defensive_contribution` gives only the **points** (`{DEF:2, MID:2,
FWD:2, GKP:0}`). The 10 / 12 action thresholds are not published anywhere in the payload
and must be hardcoded.

However I did reverse-engineer the `defensive_contribution` field's definition from
2025/26 season totals, and it is unambiguous:

| Position | Formula | Verified |
|---|---|---|
| GKP | always 0 | Petrović, Verbruggen, Pickford all 0 despite 280+ recoveries |
| DEF | `tackles + clearances_blocks_interceptions` | 4/4 exact (Gabriel 38+239=277) |
| MID | `tackles + CBI + recoveries` | 6/6 exact (Rice 196+180=376) |
| FWD | `tackles + CBI + recoveries` | 3/3 exact (Watkins 22+23+49=94) |

So `defensive_contribution` is a **raw action count**, not points and not a count of
threshold hits. The per-match counts needed to model `P(count >= threshold)` are available
in `element-summary` history and in the historical CSVs.

### Caveat 3b — Reclassified players carry DEFCON totals under their *old* position

Found while writing the Milestone 1 tests, not by inspection. `defensive_contribution`
was accrued under the position a player held **at the time**, but `element_type` is their
**current** listing. Five players with 900+ minutes have season totals computed under the
wrong rule for their new position:

| Player | Listed 2026/27 | Total matches |
|---|---|---|
| Bogarde | MID | the DEF rule |
| Lewis-Potter | MID | the DEF rule |
| Dorgu | MID | the DEF rule |
| Wieffer | DEF | the MID/FWD rule |
| Sessegnon | DEF | the MID/FWD rule |

This is exactly the profile where DEFCON drives value — converted wing-backs and
full-backs at low prices. Reading a per-90 DEFCON rate off the season total for these
players would be wrong in both directions: the three now listed as midfielders would look
worse than they are (their total omits recoveries, but their new threshold counts them),
and the two now listed as defenders would look better.

Rule for the model: never derive a DEFCON rate from the aggregate field. Rebuild it from
`tackles`, `clearances_blocks_interceptions` and `recoveries` under the player's *current*
position rule. `tests/test_data_layer.py` pins both the formula and the size of this
exception set, so a change in either fails loudly.

### Caveat 4 — `stats` / `live` / `picks` shapes cannot be verified yet

`fixtures[].stats` is `[]`, `event/1/live/` returns no elements, and the picks endpoint
404s. The schema for `fixture_stats`, `event_live` and `event_live_explain` is therefore
written from the documented structure and **must be re-verified against real data the
moment GW1 completes**, before the scoring engine is trusted. I have marked those three
tables as unverified rather than pretending otherwise.

This also means the spec's Milestone 1 acceptance test — "prove it by printing my current
squad from the API" — **cannot pass before the GW1 deadline on 21 August**. Suggested
substitute: prove the data layer by printing any public manager's past-season history plus
your own entry metadata, and add manual squad entry so the tool is usable on day one.

### Caveat 5 — Free transfers are not exposed anywhere

No public endpoint reports remaining free transfers. `entry/` has
`last_deadline_total_transfers` (cumulative) and the picks endpoint's `entry_history` has
`event_transfers`, but neither gives the current banked-free-transfer count, which is what
the optimiser needs. It must be derived from transfer history plus chip usage and be
manually correctable — hence `my_manual_state`. With up to five bankable transfers this
season, getting it wrong is a 4-point error, so the UI should always show the assumed value
and let you override it.

### Caveat 6 — No ETags on any endpoint

The spec says to respect ETags. There are none. `bootstrap-static/` returns
`cache-control: max-age=300, stale-while-revalidate=3600`; `fixtures/` returns
`no-cache, no-store, must-revalidate`. Neither sends `ETag` or `Last-Modified`, so
conditional requests are impossible.

Substitute: local TTL cache honouring the 300s max-age, plus SHA-256 content hashing to
detect no-op refreshes and skip redundant snapshot rows. Exponential backoff on non-200 as
specified.

## Spec assumptions that hold

- All expected-goals fields exist: `expected_goals`, `expected_assists`,
  `expected_goal_involvements`, `expected_goals_conceded`, each with a `_per_90` variant.
- All defensive metrics exist: `tackles`, `clearances_blocks_interceptions`, `recoveries`,
  `defensive_contribution` (+ `_per_90`).
- Set-piece fields exist exactly as named: `penalties_order`, `direct_freekicks_order`,
  `corners_and_indirect_freekicks_order`, each with a `_text` companion.
- Minutes-model inputs exist: `chance_of_playing_this_round`, `chance_of_playing_next_round`,
  `news`, `news_added`, `status`, `starts`, `starts_per_90`. Bonus: an undocumented
  `scout_risks` array and `scout_news_link` that may be worth watching.
- Chip structure is confirmed **from the API**, not assumed: two full sets, first set
  `stop_event` 19, second `start_event` 20. Note wildcard and free hit start at GW2, while
  bench boost and triple captain are available from GW1.
- Squad rules confirmed: `squad_total_spend` 1000, `squad_team_limit` 3, `squad_squadsize`
  15, `squad_squadplay` 11, per-position min/max play 1/1, 3/5, 2/5, 1/3.
- `transfers_sell_on_fee` is 0.5 and `element_sell_at_purchase_price` is false, confirming
  purchase prices must be tracked per player.
- `max_extra_free_transfers` is 4, i.e. 1 + 4 = **5 bankable transfers**, as the spec says.

## Scoring change the spec does not mention

`goals_scored` is `{GKP: 10, DEF: 6, MID: 5, FWD: 4}`. **Goalkeeper goals are worth 10
points** in 2026/27. Marginal in practice but it falls out of the rules table for free, so
the engine gets it right without special-casing.

Also worth noting: the `mng_*` manager-scoring rules are all present but set to 0, so
managers are not scoring entities this season. No need to model them.

## League composition

Promoted: Coventry (COV), Hull (HUL), Ipswich (IPS). Relegated from 2025/26: Burnley, West
Ham, Wolves. The three promoted sides have no Premier League xG history, which is the main
cold-start hole in the fixture-difficulty model.

## Cold-start data source verified

`vaastav/Fantasy-Premier-League` is live and current: seasons 2016-17 through 2026-27, with
`data/2025-26/gws/merged_gw.csv` at 5.4 MB / 29,757 rows. Columns include every scoring
component plus `bps`, `bonus`, `total_points` and all four DEFCON fields — enough to verify
the scoring engine per-match and to fit minutes and attacking models.

The repo also carries `data/world_cup_2026.csv` (1,489 rows: player, nation, position,
status, per-round points). That gives a direct route to the spec's late-returnee flag —
players from nations with `status` still active in later rounds returned latest. Names will
need fuzzy matching to FPL element codes; I will report the match rate rather than silently
dropping the misses.

## Environment note

No Python was installed on this machine. I installed Python 3.12.10 via winget to
`%LOCALAPPDATA%\Programs\Python\Python312`. It is not on `PATH` — the Windows Store alias
shadows it — so the project should pin the interpreter explicitly in a venv at Milestone 1.
