"""Local web UI. Server-rendered, no CDN, no build step.

Runs offline against the DuckDB store — the only thing that touches the network is
`fpl refresh`. Opens the database read-only so the UI can never corrupt the store,
and so it can be browsed while a refresh is running.

What is shown is only what is actually modelled. There is no expected-points model
yet, so no page pretends to have one; the projection columns arrive at Milestone 4.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config, load_config
from ..db import Database
from ..model import Model, build_model
from ..optimiser import Optimiser
from ..scoring import ScoringRules
from ..squad import (
    INITIAL_BUDGET,
    SQUAD_BY_POSITION,
    Squad,
    SquadPlayer,
    best_eleven,
    load_squad,
    save_squad,
    selling_price,
    validate,
)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="FPL Optimiser", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def get_config() -> Config:
    return load_config()


# One open handle for the whole process, shared out as cursors. See Database.cursor:
# DuckDB permits a single writing process, so the server cannot keep reopening the
# file — that is what made refreshing from the UI impossible before.
_shared_db: Database | None = None
_db_lock = threading.Lock()


def get_shared_db(config: Config) -> Database:
    global _shared_db
    with _db_lock:
        if _shared_db is None:
            _shared_db = Database(config.db_path)
        return _shared_db


def open_db(config: Config) -> Database:
    """A per-request cursor. Closing it releases the cursor, not the database."""
    return get_shared_db(config).cursor()


# ------------------------------------------------------------------ formatting
def _fmt_countdown(target: datetime) -> str:
    delta = target - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "passed"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


templates.env.filters["countdown"] = _fmt_countdown


def asset_version() -> str:
    """Fingerprint for the CSS and JS, so a browser never serves stale assets.

    Without this, editing app.js leaves every already-open tab running the old copy
    until someone hard-reloads — which is exactly how a working feature looks broken.
    """
    stamp = 0.0
    for name in ("static/style.css", "static/app.js", "static/squad.js"):
        path = HERE / name
        if path.exists():
            stamp = max(stamp, path.stat().st_mtime)
    return str(int(stamp))


def _base_context(request: Request, config: Config, db: Database) -> dict[str, Any]:
    """Deadline and freshness banner, shown on every page."""
    next_event = db.query(
        "SELECT event_id, name, deadline_time FROM latest_events "
        "WHERE deadline_time > now() ORDER BY deadline_time LIMIT 1"
    )
    last_refresh = db.scalar("SELECT MAX(fetched_at) FROM snapshots")
    event = next_event.row(0, named=True) if len(next_event) else None
    return {
        "request": request,
        "next_event": event,
        "last_refresh": last_refresh,
        "manager_id": config.manager_id,
        "has_manager": config.has_manager,
        "season": config.season,
        "asset_version": asset_version(),
    }


# ---------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)

        context["counts"] = {
            "players": db.scalar("SELECT COUNT(*) FROM latest_players_state") or 0,
            "fixtures": db.scalar("SELECT COUNT(*) FROM latest_fixtures") or 0,
            "snapshots": db.scalar("SELECT COUNT(*) FROM snapshots") or 0,
        }

        context["injuries"] = db.query(
            """
            SELECT web_name, team, position, price, status, news,
                   chance_of_playing_next_round AS chance, selected_by_percent AS owned
            FROM v_player
            WHERE status <> 'a' AND selected_by_percent > 1
            ORDER BY selected_by_percent DESC LIMIT 12
            """
        ).to_dicts()

        context["price_moves"] = db.query(
            """
            SELECT c.detected_at, v.web_name, v.team, v.position,
                   c.old_cost / 10.0 AS old_price, c.new_cost / 10.0 AS new_price, c.delta
            FROM price_changes c JOIN v_player v USING (element_id)
            ORDER BY c.detected_at DESC, ABS(c.delta) DESC LIMIT 12
            """
        ).to_dicts()

        context["set_piece_moves"] = db.query(
            """
            SELECT c.detected_at, v.web_name, v.team, c.field, c.old_value, c.new_value
            FROM set_piece_changes c JOIN v_player v USING (element_id)
            ORDER BY c.detected_at DESC LIMIT 12
            """
        ).to_dicts()

        context["penalty_takers"] = db.query(
            """
            SELECT web_name, team, position, price, selected_by_percent AS owned
            FROM v_player WHERE penalties_order = 1
            ORDER BY selected_by_percent DESC LIMIT 10
            """
        ).to_dicts()

        context["chips"] = db.query(
            "SELECT name, chip_type, start_event, stop_event FROM latest_chips_config "
            "ORDER BY stop_event, name"
        ).to_dicts()

        # Whatever squad we have — hand-entered, or the official picks once public.
        model = get_model(config, db)
        squad = load_squad(db, config.manager_id, model.horizon_start)
        context["squad"] = (
            [
                {
                    "slot": p.slot, "web_name": p.web_name, "team": p.team,
                    "position": p.position, "price": p.now_cost / 10.0,
                    "is_captain": p.is_captain, "is_vice_captain": p.is_vice_captain,
                }
                for p in squad.players
            ]
            if squad else None
        )

        entry = db.query(
            "SELECT * FROM latest_my_entry WHERE manager_id = ?", [config.manager_id]
        ) if config.has_manager else None
        context["entry"] = entry.row(0, named=True) if entry is not None and len(entry) else None

        return templates.TemplateResponse(request, "dashboard.html", context)
    finally:
        db.close()


@app.get("/players", response_class=HTMLResponse)
def players(
    request: Request,
    position: str = Query("all"),
    team: str = Query("all"),
    max_price: float = Query(15.5),
    min_minutes: int = Query(0),
    sort: str = Query("total_points"),
    search: str = Query(""),
):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)

        sortable = {
            "total_points", "price", "selected_by_percent", "points_per_million",
            "points_per_90", "defcon_per_90", "expected_goals_per_90",
            "expected_assists_per_90", "expected_goal_involvements_per_90",
            "minutes", "form", "web_name", "bps",
        }
        sort_col = sort if sort in sortable else "total_points"
        direction = "ASC" if sort_col == "web_name" else "DESC"

        clauses = ["now_cost <= ?", "minutes >= ?"]
        params: list[Any] = [int(round(max_price * 10)), min_minutes]
        if position != "all":
            clauses.append("position = ?")
            params.append(position)
        if team != "all":
            clauses.append("team = ?")
            params.append(team)
        if search.strip():
            clauses.append("(lower(web_name) LIKE ? OR lower(full_name) LIKE ?)")
            needle = f"%{search.strip().lower()}%"
            params += [needle, needle]

        rows = db.query(
            f"""
            SELECT element_id, web_name, team, position, price, status, news,
                   selected_by_percent, minutes, starts, total_points, form,
                   points_per_million, points_per_90,
                   expected_goals_per_90, expected_assists_per_90,
                   expected_goal_involvements_per_90,
                   defcon_per_90, defcon_threshold, defcon_reclassified,
                   penalties_order, direct_freekicks_order,
                   corners_and_indirect_freekicks_order,
                   cost_change_start, bps
            FROM v_player_rates
            WHERE {' AND '.join(clauses)}
            ORDER BY {sort_col} {direction} NULLS LAST
            LIMIT 300
            """,
            params,
        )

        context["players"] = rows.to_dicts()
        context["teams"] = [
            r["short_name"] for r in
            db.query("SELECT short_name FROM latest_teams ORDER BY short_name").to_dicts()
        ]
        context["filters"] = {
            "position": position, "team": team, "max_price": max_price,
            "min_minutes": min_minutes, "sort": sort_col, "search": search,
        }
        return templates.TemplateResponse(request, "players.html", context)
    finally:
        db.close()


@app.get("/player/{element_id}", response_class=HTMLResponse)
def player_detail(request: Request, element_id: int):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)

        row = db.query("SELECT * FROM v_player_rates WHERE element_id = ?", [element_id])
        context["player"] = row.row(0, named=True) if len(row) else None

        if context["player"]:
            player = context["player"]
            context["fixtures"] = db.query(
                """
                SELECT f.event, f.kickoff_time, f.is_home, f.difficulty, f.opponent
                FROM v_team_fixtures f
                WHERE f.team_id = ? AND f.event IS NOT NULL AND NOT f.finished
                ORDER BY f.event LIMIT 8
                """, [player["team_id"]]
            ).to_dicts()

            context["history"] = db.query(
                """
                SELECT event, minutes, goals_scored, assists, clean_sheets,
                       goals_conceded, saves, bonus, bps, defensive_contribution,
                       total_points
                FROM player_gw_history WHERE element_id = ?
                QUALIFY ROW_NUMBER() OVER (PARTITION BY fixture_id ORDER BY snapshot_at DESC) = 1
                ORDER BY event DESC LIMIT 15
                """, [element_id]
            ).to_dicts()

            context["past_seasons"] = db.query(
                """
                SELECT season_name, total_points, minutes, goals_scored, assists,
                       clean_sheets, bonus, defensive_contribution, expected_goals,
                       expected_assists
                FROM player_past_seasons WHERE element_code = ?
                QUALIFY ROW_NUMBER() OVER (PARTITION BY season_name ORDER BY snapshot_at DESC) = 1
                ORDER BY season_name DESC
                """, [player["element_code"]]
            ).to_dicts()

            context["price_history"] = db.query(
                """
                SELECT snapshot_at, now_cost / 10.0 AS price, selected_by_percent
                FROM players_state WHERE element_id = ?
                ORDER BY snapshot_at DESC LIMIT 30
                """, [element_id]
            ).to_dicts()

        return templates.TemplateResponse(request, "player.html", context)
    finally:
        db.close()


@app.get("/fixtures", response_class=HTMLResponse)
def fixtures(request: Request, start: int = Query(0), span: int = Query(8)):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)

        first = start or (context["next_event"]["event_id"] if context["next_event"] else 1)
        last = first + span - 1

        rows = db.query(
            """
            SELECT team, team_id, event, opponent, is_home, difficulty
            FROM v_team_fixtures
            WHERE event BETWEEN ? AND ?
            ORDER BY team, event
            """, [first, last]
        ).to_dicts()

        grid: dict[str, dict[int, list[dict]]] = {}
        for row in rows:
            grid.setdefault(row["team"], {}).setdefault(row["event"], []).append(row)

        # Sort by mean difficulty across the window: easiest run first.
        def mean_difficulty(team: str) -> float:
            values = [f["difficulty"] for events in [grid[team]] for fs in events.values() for f in fs]
            return sum(values) / len(values) if values else 99.0

        context["events"] = list(range(first, last + 1))
        context["grid"] = {t: grid[t] for t in sorted(grid, key=mean_difficulty)}
        context["mean_difficulty"] = {t: round(mean_difficulty(t), 2) for t in grid}
        context["window"] = {"start": first, "span": span, "end": last}
        return templates.TemplateResponse(request, "fixtures.html", context)
    finally:
        db.close()


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)
        context["scoring"] = db.query(
            "SELECT rule_name, position, points FROM latest_scoring_rules "
            "WHERE points <> 0 ORDER BY rule_name, position"
        ).to_dicts()
        context["game_rules"] = db.query(
            "SELECT rule_name, value FROM latest_game_rules ORDER BY rule_name"
        ).to_dicts()

        loaded = ScoringRules.from_db(db)
        context["constants"] = {
            "appearance_minutes": loaded.constants.appearance_minutes,
            "saves_per_point": loaded.constants.saves_per_point,
            "goals_conceded_per_penalty": loaded.constants.goals_conceded_per_penalty,
            "defcon_threshold_def": loaded.constants.defcon_threshold_def,
            "defcon_threshold_mid_fwd": loaded.constants.defcon_threshold_mid_fwd,
            "source": loaded.constants.source,
        }
        return templates.TemplateResponse(request, "rules.html", context)
    finally:
        db.close()


@app.get("/api/health")
def health():
    config = get_config()
    db = open_db(config)
    try:
        return {
            "ok": True,
            "db": str(config.db_path),
            "players": db.scalar("SELECT COUNT(*) FROM latest_players_state"),
            "last_refresh": db.scalar("SELECT MAX(fetched_at) FROM snapshots"),
        }
    finally:
        db.close()


# ------------------------------------------------------------- model caching
# Fitting takes a fraction of a second, but every page that values a squad needs it,
# so it is cached per process and invalidated when the store changes on disk.
_model_cache: dict[str, Any] = {"key": None, "model": None}


def get_model(config: Config, db: Database) -> Model:
    stamp = config.db_path.stat().st_mtime if config.db_path.exists() else 0
    key = f"{stamp}:{config.horizon}"
    if _model_cache["key"] != key:
        _model_cache["model"] = build_model(
            db, horizon=config.horizon, cache_dir=config.data_dir / "history"
        )
        _model_cache["key"] = key
    return _model_cache["model"]


def _squad_or_none(db: Database, config: Config, model: Model) -> Squad | None:
    """A hand-entered squad is local, so it does not need a manager_id.

    manager_id 0 is the local slot; a configured id lets the same squad line up with
    the API's picks once they become public after the first deadline.
    """
    return load_squad(db, config.manager_id, model.horizon_start)


@app.get("/squad", response_class=HTMLResponse)
def squad_page(request: Request):
    """Enter or edit your fifteen."""
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)
        model = get_model(config, db)

        # Cast the DECIMAL columns: they reach the page as JSON for the picker.
        pool = db.query(
            """
            SELECT element_id, web_name, full_name, position, team, now_cost,
                   status, COALESCE(news, '') AS news,
                   CAST(selected_by_percent AS DOUBLE) AS selected_by_percent,
                   CAST(total_points AS INTEGER) AS total_points,
                   CAST(minutes AS INTEGER) AS minutes
            FROM v_player ORDER BY now_cost DESC, total_points DESC
            """
        ).to_dicts()

        optimiser = Optimiser(model, decay=config.decay, bench_weight=config.bench_weight)
        for player in pool:
            player["price"] = player["now_cost"] / 10.0
            player["xp_next"] = round(optimiser.xp(player["element_id"], model.horizon_start), 2)
            player["xp_horizon"] = round(
                sum(optimiser.xp(player["element_id"], model.horizon_start + i)
                    for i in range(model.horizon)), 1
            )

        existing = _squad_or_none(db, config, model)
        context["pool"] = pool
        context["existing"] = (
            {
                "players": [
                    {"element_id": p.element_id, "purchase_price": p.purchase_price}
                    for p in existing.players
                ],
                "bank": existing.bank,
                "free_transfers": existing.free_transfers,
            }
            if existing else None
        )
        context["squad_requirements"] = SQUAD_BY_POSITION
        context["initial_budget"] = INITIAL_BUDGET
        context["model_notes"] = model.notes
        return templates.TemplateResponse(request, "squad.html", context)
    finally:
        db.close()


@app.post("/squad/save")
def squad_save(payload: dict = Body(...)):
    """Persist a hand-entered squad. Append-only, so earlier entries survive."""
    config = get_config()
    db = open_db(config)
    try:
        model = get_model(config, db)
        entries = payload.get("players") or []
        if len(entries) != 15:
            return JSONResponse(
                {"ok": False, "error": f"Need 15 players, got {len(entries)}."}, status_code=400
            )

        lookup = {
            row["element_id"]: row
            for row in db.query(
                "SELECT element_id, web_name, position, team, now_cost FROM v_player"
            ).to_dicts()
        }

        players = []
        for index, entry in enumerate(entries):
            element_id = int(entry["element_id"])
            row = lookup.get(element_id)
            if row is None:
                return JSONResponse(
                    {"ok": False, "error": f"Unknown player id {element_id}."}, status_code=400
                )
            purchase = int(entry.get("purchase_price") or row["now_cost"])
            players.append(SquadPlayer(
                element_id=element_id, web_name=row["web_name"], position=row["position"],
                team=row["team"], now_cost=row["now_cost"], purchase_price=purchase,
                slot=index + 1,
            ))

        bank = int(payload.get("bank") or 0)
        free_transfers = int(payload.get("free_transfers") or 1)
        candidate = Squad(players=players, bank=bank, free_transfers=free_transfers,
                          manager_id=config.manager_id, event=model.horizon_start)

        problems = validate(candidate)
        if problems:
            return JSONResponse({"ok": False, "error": "; ".join(problems)}, status_code=400)

        save_squad(
            db, config.manager_id, model.horizon_start,
            [
                {
                    "element_id": p.element_id, "slot": p.slot,
                    "purchase_price": p.purchase_price, "selling_price": p.sell_value,
                    "is_captain": False, "is_vice_captain": False,
                }
                for p in players
            ],
            bank=bank, free_transfers=free_transfers,
        )
        _model_cache["key"] = None
        return {"ok": True, "squad_value": candidate.sell_value, "bank": bank}
    finally:
        db.close()


@app.get("/transfers", response_class=HTMLResponse)
def transfers_redirect(request: Request):
    """The old, denser page. Kept as a redirect so links do not rot."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/advice", status_code=307)


@app.get("/advice", response_class=HTMLResponse)
def advice_page(request: Request):
    """Keep or swap, player by player, with the reasoning."""
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)
        model = get_model(config, db)
        squad = _squad_or_none(db, config, model)
        context["squad"] = squad
        context["model_notes"] = model.notes

        if squad is None:
            return templates.TemplateResponse(request, "advice.html", context)

        optimiser = Optimiser(model, decay=config.decay, bench_weight=config.bench_weight)
        verdicts = optimiser.review_squad(db, squad)

        xp_now = {p.element_id: optimiser.xp(p.element_id, model.horizon_start)
                  for p in squad.players}
        xi, bench, formation = best_eleven(squad, xp_now)
        captain_options = optimiser.captain_options(squad, top=6)

        # Pairs are built from the individual best swaps, so they stay consistent
        # with the per-player advice above rather than being a separate search.
        singles = [v.best_swap for v in verdicts if v.best_swap and v.gain > 0]
        singles.sort(key=lambda m: -m.gain)
        pairs = optimiser.double_transfers(db, squad, singles, top=3) if len(singles) > 1 else []

        context.update({
            "verdicts": verdicts,
            "baseline": optimiser.value_squad(squad),
            "xi": xi, "bench": bench, "formation": formation, "xp_now": xp_now,
            "captain_options": captain_options,
            "captain": captain_options[0] if captain_options else None,
            "pairs": [p for p in pairs if p.gain > 0],
            "horizon": model.horizon,
            "horizon_start": model.horizon_start,
            "squad_problems": validate(squad),
        })
        return templates.TemplateResponse(request, "advice.html", context)
    finally:
        db.close()


@app.get("/captain", response_class=HTMLResponse)
def captain_page(request: Request):
    config = get_config()
    db = open_db(config)
    try:
        context = _base_context(request, config, db)
        model = get_model(config, db)
        squad = _squad_or_none(db, config, model)
        context["squad"] = squad
        context["model_notes"] = model.notes

        if squad is not None:
            optimiser = Optimiser(model, decay=config.decay, bench_weight=config.bench_weight)
            context["options"] = optimiser.captain_options(squad, top=8)
        return templates.TemplateResponse(request, "captain.html", context)
    finally:
        db.close()


# ---------------------------------------------------------------- refreshing
# Runs on a worker thread so the page stays responsive: a full refresh with player
# summaries takes about a minute. State lives here rather than in the database
# because it describes this process, not the data.
_refresh_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
    "notes": [],
    "rows": {},
}
_refresh_lock = threading.Lock()


def _do_refresh(config: Config, include_players: bool) -> None:
    from ..refresh import run_refresh

    db = open_db(config)
    try:
        report = run_refresh(config, db, include_players=include_players)
        changed = sum(report.rows_written.values())
        if changed:
            parts = ", ".join(
                f"{count:,} {table.replace('_', ' ')}"
                for table, count in sorted(report.rows_written.items(), key=lambda kv: -kv[1])[:4]
            )
            summary = f"Updated {parts}."
        elif report.cache_hits and not report.fetches:
            summary = "Already up to date — nothing had changed since the last check."
        else:
            summary = "Checked everything; nothing had changed."

        with _refresh_lock:
            _refresh_state.update({
                "summary": summary,
                "notes": report.notes,
                "rows": report.rows_written,
                "error": None,
            })
        # New data means the fitted model is stale.
        _model_cache["key"] = None
    except Exception as exc:                     # surfaced to the user, not swallowed
        with _refresh_lock:
            _refresh_state["error"] = f"{type(exc).__name__}: {exc}"
            _refresh_state["summary"] = None
    finally:
        db.close()
        with _refresh_lock:
            _refresh_state["running"] = False
            _refresh_state["finished_at"] = datetime.now(timezone.utc)


@app.post("/api/refresh")
def start_refresh(payload: dict = Body(default={})):
    """Kick off a data refresh. Returns immediately; poll /api/refresh for progress."""
    config = get_config()
    include_players = bool(payload.get("players"))

    with _refresh_lock:
        if _refresh_state["running"]:
            return JSONResponse({"ok": False, "error": "A refresh is already running."},
                                status_code=409)
        _refresh_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc),
            "finished_at": None,
            "summary": None,
            "error": None,
            "notes": [],
            "rows": {},
        })

    thread = threading.Thread(target=_do_refresh, args=(config, include_players), daemon=True)
    thread.start()
    return {"ok": True, "running": True, "players": include_players}


@app.get("/api/refresh")
def refresh_status():
    config = get_config()
    db = open_db(config)
    try:
        last = db.scalar("SELECT MAX(fetched_at) FROM snapshots")
    finally:
        db.close()

    with _refresh_lock:
        state = dict(_refresh_state)

    return {
        "running": state["running"],
        "summary": state["summary"],
        "error": state["error"],
        "notes": state["notes"],
        "last_refresh": last.isoformat() if last else None,
        "last_refresh_human": last.strftime("%d %b %H:%M") + " UTC" if last else None,
    }
