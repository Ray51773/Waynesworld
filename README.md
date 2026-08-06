# FPL Optimiser

Local decision-support for Fantasy Premier League 2026/27. Projects points, recommends
transfers, captain and chips, and shows its reasoning.

Runs entirely on your machine. No accounts, no credentials, no writes to the FPL API ever.

**Status: end-to-end and usable.** Enter your squad, get ranked transfer
recommendations with reasoning, and pick a captain. Not yet backtested — see
"How much to trust this" below.

## Setup

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e .
```

That is the whole setup. You enter your squad in the web UI, so no account or ID is
needed. `config.toml` has an optional `manager_id` if you later want the tool to pull
your official picks and history once the season is under way.

## Commands

```bash
fpl serve                # start the web UI on http://127.0.0.1:8000
fpl transfers            # ranked moves with reasoning
fpl captain              # captain options with haul and blank probabilities
fpl player <name>        # one player's full xP breakdown
fpl import-history       # load past-season data the model is fitted on
fpl refresh              # pull latest data, snapshot it, load it
fpl refresh --players    # also pull all 570 per-player summaries (~55s cold)
fpl refresh --force      # ignore the TTL cache
fpl status               # what is in the local store and how fresh
fpl deadline             # next deadline, chips and their windows
fpl squad                # your squad (falls back to entry summary pre-season)
fpl rules                # 2026/27 scoring, read from the API not hardcoded
fpl alerts               # set-piece, availability and price changes
```

Run them via `.venv/Scripts/python -m fpl.cli <command>` if the `fpl` script is not on PATH.

## Using it

Two steps.

**1. Pick your squad.** Open http://127.0.0.1:8000/squad and click your fifteen in from
the list. It tracks your budget, blocks four-from-one-club, and tells you when the squad
is legal. Set the price you actually paid where it differs from today's — selling value
halves your profit, and the optimiser needs the real number.

**2. Read the advice.** http://127.0.0.1:8000/advice sorts your fifteen into three groups:

- **Worth changing** — a clearly better player is available and affordable
- **Borderline** — a small upgrade exists, probably not worth a transfer
- **Keep** — nothing affordable beats them

Each player gets a one-line verdict, and clicking the row opens the full reasoning: which
components drive the difference, how the fixtures compare, what the move costs, and any
caution worth knowing (a replacement who started half the time, or who has no Premier
League history at all).

At the top is the single thing to do this week — or "make no transfer", when nothing beats
holding. Below the fifteen: captaincy, your projected eleven, and the two-transfer option.

Same thing in a terminal:

```bash
fpl transfers        # who to swap and why
fpl transfers --all  # include the reasoning for players worth keeping
fpl captain
```

## Web UI

```bash
fpl serve
```

Four pages, server-rendered, no CDN and no build step, bound to localhost.

**My squad** and **Advice** are the two you will use. **Players** is a sortable table of
everyone with xG, xA and defensive-contribution rates for browsing; **Fixtures** is the
difficulty ticker. Status and scoring rules are linked in the footer.

## Keeping it current

Nothing here updates itself. Worth being clear about that, because "is it current?" has
three different answers depending on what you mean.

**The data.** The store is a snapshot. Prices, injuries, ownership and set-piece orders
only move when you fetch them. There is a **Refresh data** button in the header of every
page — press it and the page reloads with the new figures. It takes a few seconds, tells
you what changed, and is the normal way to keep things current.

The tool caches politely (five minutes for the main endpoint, six hours for per-player
detail), so pressing it repeatedly is harmless. The right rhythm is a press or two in the
days before a deadline, when prices and team news actually move.

To have it happen without you, register a scheduled task once, adjusting the path to your
checkout. Only worth it if you want data collected while the app is closed — note that
price history is only as complete as the snapshots you took:

```powershell
schtasks /create /tn "FPL refresh" /sc hourly /tr "'C:\Users\you\Desktop\ffl\.venv\Scripts\python.exe' -m fpl.cli refresh" /f
```

Stop `fpl serve` while a scheduled run fires, or the write will collide — see "One writer
at a time". If the app is open anyway, the button is simpler.

**The model.** This is the part that genuinely goes stale. Every rate is currently fitted
on the 2025/26 season, because no 2026/27 match has been played. Once games are played,
`fpl refresh --players` starts filling in this season's per-match history, but **the model
does not yet blend it with last season's** — that is unwritten work, not a setting. Until
it is written, projections keep reasoning from last season no matter how much of this one
has happened. The same goes for the BPS weightings, which need refitting from live matches
before bonus projections mean much.

**The code.** A clone is a copy; it does not pull updates. The FPL API also changes shape
between seasons — field names move, scoring rules change. `FINDINGS.md` records what the
API actually returned on the day it was checked, so the next person to touch this can diff
reality against that rather than guess.

## The model

Fitted entirely on last season, because no 2026/27 match has been played yet.

- **Team ratings** — attack, defence and home advantage fitted by Poisson iterative
  proportional fitting on last season's results and xG. Necessary rather than optional:
  the API's own strength ratings are all zero pre-season. Promoted sides get a flagged
  prior, since they have no top-flight history.
- **Minutes** — recency-weighted start and 60-minute rates from each player's own match
  log, scaled by the availability news. Everything multiplies by this, so it is computed
  first and its confidence is reported.
- **Attacking returns** — xG and xA per 90, shrunk toward positional means by minutes
  played, then scaled by the fixture.
- **Clean sheets** — from the fitted defence rating, conditioned on reaching 60 minutes.
  The concede penalty integrates the distribution rather than using the mean.
- **Defensive contribution** — a threshold, not a rate, so the model computes the
  probability of *clearing* 10 or 12 actions, blending Poisson with each player's observed
  hit rate.
- **Bonus** — a placeholder, and flagged as one everywhere it appears. See below.

## Scoring engine

Reimplemented from the rules table rather than hardcoded, and verified against reality:
it reproduces **all 11,498** of last season's player-gameweeks exactly, including the
derived clean-sheet path that projection will actually use. The spec asked for 200.

The engine is season-agnostic — pass 2026/27 rules to project, 2025/26 rules to verify —
so the same code path is exercised either way. Run the check yourself:

```bash
.venv/Scripts/python scripts/verify_scoring.py
```

## How data is stored

Two layers, because one of them has to be beyond doubt.

1. **Raw snapshots.** Every fetch is written to `data/snapshots/{endpoint}/{timestamp}.json.gz`
   before anything parses it. A loader bug can never lose a fetch.
2. **DuckDB** at `data/fpl.duckdb`, normalised, append-only, every row stamped with the
   snapshot that produced it. Prices, ownership and injury news are time series; the API
   only ever shows you *now*, so the history is only as good as what we kept.

Rows are appended **when something changes**, not blindly every hour — outside price-change
windows the vast majority of player rows are byte-identical between refreshes. History stays
complete because a row is written the instant anything moves. See `SCHEMA.md`.

Neither layer is ever mutated. Deleting `data/fpl.duckdb` starts the store clean; the raw
snapshots on disk still hold every byte ever fetched, but a replay command that rebuilds
the database from them is not written yet — for now a delete means refetching.

### One writer at a time

DuckDB allows many readers or one writer across the whole machine, so the server holds
the database open once for the whole process and hands out cursors internally. That is
what lets the **Refresh data** button write while you are using the app.

The limit still applies between processes: while `fpl serve` is running, a separate
`fpl refresh`, `fpl import-history` or test run in a terminal will fail to open the file.
Use the button instead, or stop the server first.

## Being a good guest on the API

The FPL API sends no ETags, so conditional requests are impossible. Instead: per-endpoint
TTLs (5 minutes for bootstrap, 6 hours for player summaries), SHA-256 content hashing to
skip no-op writes, a global rate limiter shared across threads, and exponential backoff on
any non-200. A warm `fpl refresh --players` makes zero requests.

## Read this before trusting anything

`FINDINGS.md` documents what the API actually provides, verified on 2026-08-05, and seven
places where reality differs from expectation. The ones that matter most:

- **BPS weightings are not in the API** and cannot be fitted from last season either, since
  the rules changed. They must be seeded from the published rules, then refitted once a few
  gameweeks exist. Bonus projections are provisional until then.
- **Team strength ratings are all zero pre-season**, so fixture difficulty has to come from a
  fitted model rather than the API's own numbers.
- **Free transfers are exposed by no endpoint** and must be tracked and manually correctable.
- **Squad picks are not public until the first deadline passes**, so manual squad entry is
  required for GW1.
- **`defensive_contribution` totals use the position the player held at the time**, which
  silently misprices reclassified full-backs. Rebuild the rate from components.

## Layout

```
src/fpl/
  client.py     HTTP, rate limiting, backoff, raw snapshot storage
  db.py         DuckDB, append-on-change
  schema.sql    tables
  views.sql     latest_* views, rates, change detection
  loaders.py    JSON to rows, with type coercion at the boundary
  refresh.py    orchestration and TTL policy
  scoring.py    the scoring engine
  cli.py        commands
  web/          FastAPI app, Jinja templates, one stylesheet
scripts/
  verify_scoring.py
tests/
  fixtures/     a real bootstrap-static payload, committed so tests never skip
SCHEMA.md       schema and the reasoning behind it
FINDINGS.md     what the API really returns
```

## How much to trust this

The scoring engine is verified exactly. The projection model on top of it is **not yet
backtested**, which the spec rightly calls non-negotiable before trusting output. Until
that exists, treat the recommendations as a well-reasoned argument rather than an answer,
and read the caveat list at the bottom of every page.

Known weaknesses, worst first:

1. **Bonus is a placeholder.** BPS weightings are absent from the API and cannot be fitted
   from last season, because the system was reworked for 2026/27. It is deliberately the
   smallest component and is flagged everywhere it appears.
2. **No 2026/27 evidence exists.** Every rate is last season's, so a player who changed
   club or role is mispriced until matches are played.
3. **Promoted sides use a prior**, not a fitted rating.
4. **Effective ownership is not modelled**, so the differential case rests on haul
   probability alone.

FPL variance is enormous. A good model buys a modest edge over a season. Nothing here
guarantees a rank.
