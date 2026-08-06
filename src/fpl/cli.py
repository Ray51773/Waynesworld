"""Command line interface. Milestone 1 subset: refresh, status, deadline, squad, rules."""

from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .db import Database
from .refresh import run_refresh

app = typer.Typer(add_completion=False, help="Fantasy Premier League optimiser (local, read-only).")
console = Console()


def _open_db(read_only: bool = False) -> tuple[Database, object]:
    config = load_config()
    db = Database(config.db_path, read_only=read_only)
    return db, config


def _fmt_delta(target: datetime) -> str:
    delta = target - datetime.now(timezone.utc)
    if delta.total_seconds() < 0:
        return "passed"
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


@app.command()
def refresh(
    players: bool = typer.Option(False, "--players", help="Also fetch per-player summaries (slow)."),
    live: bool = typer.Option(False, "--live", help="Also fetch live gameweek scoring."),
    force: bool = typer.Option(False, "--force", help="Ignore TTL cache and refetch."),
) -> None:
    """Pull the latest data, snapshot it, and load it into the local store."""
    db, config = _open_db()
    try:
        with console.status("[bold]refreshing[/]", spinner="dots") as status:
            def progress(done: int, total: int) -> None:
                status.update(f"[bold]refreshing[/] player summaries {done}/{total}")

            report = run_refresh(
                config, db,
                include_players=players, include_live=live,
                force=force, progress=progress,
            )

        table = Table(title="rows appended", box=None, header_style="bold")
        table.add_column("table"); table.add_column("rows", justify="right")
        for name, count in sorted(report.rows_written.items(), key=lambda kv: -kv[1]):
            table.add_row(name, f"{count:,}")
        if not report.rows_written:
            table.add_row("[dim]nothing changed[/]", "0")
        console.print(table)

        console.print(
            f"\n[bold]{report.fetches}[/] fetches, "
            f"[bold]{report.cache_hits}[/] cache hits, "
            f"in [bold]{report.duration_seconds:.1f}s[/]"
        )
        if report.unchanged_endpoints:
            console.print(f"[dim]unchanged: {', '.join(report.unchanged_endpoints)}[/]")
        for note in report.notes:
            console.print(f"[yellow]note[/] {note}")
        for err in report.errors:
            console.print(f"[red]error[/] {err}")
    finally:
        db.close()


@app.command()
def status() -> None:
    """What is in the local store, and how fresh is it."""
    db, config = _open_db()
    try:
        counts = db.table_counts()
        table = Table(title="local store", box=None, header_style="bold")
        table.add_column("table"); table.add_column("rows", justify="right")
        for row in counts.iter_rows(named=True):
            table.add_row(row["table"], f"{row['rows']:,}")
        console.print(table)

        last = db.query(
            "SELECT endpoint, MAX(fetched_at) AS last_fetch, COUNT(*) AS fetches "
            "FROM snapshots GROUP BY endpoint ORDER BY endpoint"
        )
        if len(last):
            snap = Table(title="\nsnapshots", box=None, header_style="bold")
            snap.add_column("endpoint"); snap.add_column("fetches", justify="right")
            snap.add_column("last fetch")
            for row in last.iter_rows(named=True):
                snap.add_row(row["endpoint"], f"{row['fetches']:,}",
                             row["last_fetch"].strftime("%Y-%m-%d %H:%M:%SZ"))
            console.print(snap)

        console.print(f"\n[dim]db: {config.db_path}[/]")
        console.print(f"[dim]risk: {config.risk_posture}  horizon: {config.horizon}[/]")
    finally:
        db.close()


@app.command()
def deadline() -> None:
    """Time to the next deadline, and what is outstanding."""
    db, config = _open_db(read_only=True)
    try:
        row = db.query(
            "SELECT event_id, name, deadline_time, finished, is_current, is_next "
            "FROM latest_events WHERE deadline_time > now() ORDER BY deadline_time LIMIT 1"
        )
        if not len(row):
            console.print("[yellow]no future deadline found - run `fpl refresh`[/]")
            return
        r = row.row(0, named=True)
        when = r["deadline_time"]
        console.print(Panel(
            f"[bold]{r['name']}[/]\n"
            f"deadline  {when.strftime('%a %d %b %Y %H:%M')} UTC\n"
            f"in        [bold cyan]{_fmt_delta(when)}[/]",
            title="next deadline", expand=False,
        ))

        chips = db.query(
            "SELECT name, chip_type, start_event, stop_event FROM latest_chips_config "
            "WHERE stop_event >= ? ORDER BY start_event, name", [r["event_id"]]
        )
        if len(chips):
            table = Table(title="chips available", box=None, header_style="bold")
            table.add_column("chip"); table.add_column("type")
            table.add_column("window"); table.add_column("expires")
            for c in chips.iter_rows(named=True):
                table.add_row(c["name"], c["chip_type"],
                              f"GW{c['start_event']}-{c['stop_event']}",
                              f"end of GW{c['stop_event']}")
            console.print(table)
    finally:
        db.close()


@app.command()
def squad() -> None:
    """Your fifteen, with projected points for the next gameweek."""
    from .model import build_model
    from .squad import best_eleven, load_squad, validate

    config = load_config()
    db = Database(config.db_path, read_only=True)
    try:
        model = build_model(db, horizon=config.horizon, cache_dir=config.data_dir / "history")
        current = load_squad(db, config.manager_id, model.horizon_start)
        if current is None:
            console.print("[yellow]no squad yet.[/] Pick your fifteen at "
                          "http://127.0.0.1:8000/squad (run `fpl serve`).")
            return

        xp = {p.element_id: model.project_player(p.element_id, model.horizon_start).xp_mean
              for p in current.players}
        starting, bench, formation = best_eleven(current, xp)
        starters = {p.element_id for p in starting}

        table = Table(title=f"squad - {formation[1]}-{formation[2]}-{formation[3]}",
                      box=None, header_style="bold")
        table.add_column("player"); table.add_column("pos"); table.add_column("team")
        table.add_column("price", justify="right"); table.add_column("xP", justify="right")
        table.add_column("")

        for player in starting + bench:
            table.add_row(
                player.web_name, player.position, player.team,
                f"{player.now_cost / 10:.1f}", f"{xp[player.element_id]:.2f}",
                "" if player.element_id in starters else "bench",
            )
        console.print(table)

        projected = sum(xp[p.element_id] for p in starting)
        console.print(f"\nprojected [bold]{projected:.1f}[/] this gameweek  "
                      f"value [bold]{current.sell_value / 10:.1f}[/]  "
                      f"bank [bold]{current.bank / 10:.1f}[/]  "
                      f"free transfers [bold]{current.free_transfers}[/]")

        problems = validate(current)
        if problems:
            console.print(f"[yellow]not legal:[/] {'; '.join(problems)}")
        console.print("\n[dim]`fpl transfers` for what to change.[/]")
    finally:
        db.close()


@app.command()
def rules() -> None:
    """The 2026/27 scoring rules, as read from the API rather than hardcoded."""
    db, _ = _open_db(read_only=True)
    try:
        rows = db.query(
            "SELECT rule_name, position, points FROM latest_scoring_rules "
            "WHERE points <> 0 ORDER BY rule_name, position"
        )
        if not len(rows):
            console.print("[yellow]no scoring rules - run `fpl refresh`[/]")
            return
        table = Table(title="scoring rules (non-zero)", box=None, header_style="bold")
        table.add_column("rule"); table.add_column("position"); table.add_column("points", justify="right")
        for r in rows.iter_rows(named=True):
            table.add_row(r["rule_name"], r["position"], str(r["points"]))
        console.print(table)
        console.print("\n[dim]Thresholds for defensive_contribution (10 DEF / 12 MID-FWD) and the "
                      "BPS weightings are NOT in the API - see FINDINGS.md caveats 2 and 3.[/]")
    finally:
        db.close()


@app.command()
def alerts(limit: int = typer.Option(20, help="Max rows per section.")) -> None:
    """Set-piece, availability and price changes detected across snapshots."""
    db, _ = _open_db(read_only=True)
    try:
        sections = [
            ("set-piece changes", """
                SELECT c.detected_at, v.web_name, v.team, c.field, c.old_value, c.new_value
                FROM set_piece_changes c JOIN v_player v USING (element_id)
                ORDER BY c.detected_at DESC LIMIT ?"""),
            ("availability changes", """
                SELECT c.detected_at, v.web_name, v.team, c.old_status, c.new_status,
                       c.new_chance, c.news
                FROM availability_changes c JOIN v_player v USING (element_id)
                ORDER BY c.detected_at DESC LIMIT ?"""),
            ("price changes", """
                SELECT c.detected_at, v.web_name, v.team, c.old_cost, c.new_cost, c.delta
                FROM price_changes c JOIN v_player v USING (element_id)
                ORDER BY c.detected_at DESC LIMIT ?"""),
        ]
        any_rows = False
        for title, sql in sections:
            frame = db.query(sql, [limit])
            if not len(frame):
                continue
            any_rows = True
            table = Table(title=title, box=None, header_style="bold")
            for col in frame.columns:
                table.add_column(col)
            for r in frame.iter_rows():
                table.add_row(*[str(x) if x is not None else "-" for x in r])
            console.print(table)
            console.print()
        if not any_rows:
            console.print("[dim]no changes detected yet - alerts need at least two "
                          "refreshes with different data[/]")
    finally:
        db.close()




@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address. Localhost only by default."),
    port: int = typer.Option(8000, help="Port to serve on."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
) -> None:
    """Start the local web UI."""
    import uvicorn

    config = load_config()
    if not config.db_path.exists():
        console.print("[yellow]no local store yet[/] - run `fpl refresh` first.")
        raise typer.Exit(1)

    console.print(f"[bold]FPL Optimiser[/] on [cyan]http://{host}:{port}[/]  (ctrl-c to stop)")
    uvicorn.run("fpl.web.app:app", host=host, port=port, reload=reload, log_level="warning")


def _build(read_only: bool = True):
    """Open the store and fit the model. Shared by the decision commands."""
    from .model import build_model
    from .optimiser import Optimiser

    config = load_config()
    db = Database(config.db_path, read_only=read_only)
    model = build_model(db, horizon=config.horizon, cache_dir=config.data_dir / "history")
    optimiser = Optimiser(model, decay=config.decay, bench_weight=config.bench_weight)
    return config, db, model, optimiser


@app.command("import-history")
def import_history(season: str = typer.Option("2025-26", help="Season to import.")) -> None:
    """Load past-season match data, which every model is fitted on."""
    from .history import import_season

    config = load_config()
    db = Database(config.db_path)
    try:
        with console.status(f"[bold]importing {season}[/]", spinner="dots"):
            report = import_season(db, season, config.data_dir / "history")
        console.print(
            f"[bold]{report.rows:,}[/] rows, "
            f"[bold]{report.matched:,}[/] linked to current players "
            f"({report.match_rate:.0%}), {report.team_matches:,} team matches"
        )
        console.print("[dim]unmatched are mostly players who left the league[/]")
    finally:
        db.close()


@app.command()
def transfers(
    all_players: bool = typer.Option(False, "--all", help="Also list players worth keeping."),
) -> None:
    """Who to swap, who to keep, and why."""
    from .squad import load_squad, validate

    config, db, model, optimiser = _build()
    try:
        squad = load_squad(db, config.manager_id, model.horizon_start)
        if squad is None:
            console.print("[yellow]no squad saved.[/] Pick one at "
                          "http://127.0.0.1:8000/squad (run `fpl serve`).")
            raise typer.Exit(1)

        problems = validate(squad)
        if problems:
            joined = "; ".join(problems)
            console.print(f"[yellow]squad is not legal:[/] {joined}")

        verdicts = optimiser.review_squad(db, squad)
        swaps = [v for v in verdicts if v.verdict == "swap"]

        if swaps:
            top = swaps[0]
            console.print(Panel(
                f"Swap [bold]{top.player.web_name}[/] for "
                f"[bold]{top.best_swap.in_player.web_name}[/]"
                f"\nWorth about [bold]{top.gain:+.1f} points[/] over the next "
                f"{model.horizon} gameweeks.",
                title="what to do", expand=False,
            ))
        else:
            banked = min(squad.free_transfers + 1, 5)
            console.print(Panel(
                "Make no transfer."
                "\nNothing available beats what you have by enough to be worth it. "
                f"Roll it and you will have {banked} free next week.",
                title="what to do", expand=False,
            ))

        colours = {"swap": "red", "consider": "yellow", "keep": "green"}
        for group, heading in (("swap", "WORTH CHANGING"),
                               ("consider", "BORDERLINE"),
                               ("keep", "KEEP")):
            members = [v for v in verdicts if v.verdict == group]
            if not members or (group == "keep" and not all_players):
                continue
            console.print(f"\n[bold]{heading}[/]")
            for item in members:
                console.print(f"  [{colours[group]}]*[/] [bold]{item.player.web_name}[/] "
                              f"({item.player.position}, {item.player.team}, "
                              f"{item.horizon_xp:.0f} xP)")
                console.print(f"      {item.headline}")
                for line in item.detail:
                    console.print(f"      [dim]{line}[/]")
                if group == "swap" and item.best_swap:
                    for reason in item.best_swap.reasoning[:3]:
                        console.print(f"      [dim]{reason}[/]")

        if not all_players:
            keeps = sum(1 for v in verdicts if v.verdict == "keep")
            console.print(f"\n[dim]{keeps} players worth keeping; "
                          f"run with --all to see why.[/]")

        for note in model.notes:
            console.print(f"[dim]caveat: {note}[/]")
    finally:
        db.close()


@app.command()
def captain() -> None:
    """Captain options with expected points, haul chance and blank risk."""
    from .squad import load_squad

    config, db, model, optimiser = _build()
    try:
        squad = load_squad(db, config.manager_id, model.horizon_start)
        if squad is None:
            console.print("[yellow]no squad saved.[/] Enter one at "
                          "http://127.0.0.1:8000/squad (run `fpl serve`).")
            raise typer.Exit(1)

        options = optimiser.captain_options(squad, top=8)
        table = Table(title=f"captain options, GW{model.horizon_start}",
                      box=None, header_style="bold")
        table.add_column("player"); table.add_column("pos"); table.add_column("fixture")
        for column in ("xP", "as (C)", "P(10+)", "P(blank)"):
            table.add_column(column, justify="right")
        for option in options:
            table.add_row(
                option["web_name"], option["position"],
                f"{option['opponent']} {'H' if option['is_home'] else 'A'}",
                f"{option['xp']:.2f}", f"{option['captain_xp']:.2f}",
                f"{option['p_haul'] * 100:.1f}%", f"{option['p_blank'] * 100:.0f}%",
            )
        console.print(table)

        if options:
            safest = options[0]
            boldest = max(options, key=lambda o: o["p_haul"])
            console.print(f"\n[bold]Safest:[/] {safest['web_name']} "
                          f"({safest['xp']:.2f} xP, blanks {safest['p_blank'] * 100:.0f}%)")
            if boldest["element_id"] != safest["element_id"]:
                console.print(f"[bold]Highest ceiling:[/] {boldest['web_name']} "
                              f"({boldest['p_haul'] * 100:.1f}% chance of 10+, "
                              f"costs {safest['xp'] - boldest['xp']:.2f} xP)")
            else:
                console.print("[dim]The same player has both the highest expected points "
                              "and the best ceiling, so there is no trade-off this week.[/]")
    finally:
        db.close()


@app.command("player")
def player_detail(name: str = typer.Argument(..., help="Player name, or part of it.")) -> None:
    """Deep dive on one player: the full expected-points breakdown."""
    config, db, model, optimiser = _build()
    try:
        needle = f"%{name.lower()}%"
        matches = db.query(
            "SELECT element_id, web_name, full_name, position, team, price "
            "FROM v_player WHERE lower(web_name) LIKE ? OR lower(full_name) LIKE ? "
            "ORDER BY total_points DESC LIMIT 8",
            [needle, needle],
        ).to_dicts()

        if not matches:
            console.print(f"[yellow]no player matching '{name}'[/]")
            raise typer.Exit(1)
        if len(matches) > 1:
            others = ", ".join(m["web_name"] for m in matches[1:])
            console.print(f"[dim]{len(matches)} matches; showing "
                          f"{matches[0]['web_name']}. Others: {others}[/]\n")

        target = matches[0]
        console.print(f"[bold]{target['full_name']}[/] "
                      f"{target['position']} {target['team']} GBP{target['price']:.1f}m\n")

        table = Table(box=None, header_style="bold")
        table.add_column("GW", justify="right"); table.add_column("opponent")
        for column in ("diff", "xP", "mins", "P(CS)", "P(DC)"):
            table.add_column(column, justify="right")

        for projection in model.project_horizon(target["element_id"]):
            fixture = (f"{projection.opponent} {'H' if projection.is_home else 'A'}"
                       if projection.fixture_count else "blank")
            table.add_row(
                str(projection.event), fixture,
                f"{projection.difficulty:.1f}" if projection.fixture_count else "-",
                f"{projection.xp_mean:.2f}",
                f"{projection.expected_minutes:.0f}",
                f"{projection.p_clean_sheet:.2f}",
                f"{projection.p_defcon_hit:.2f}",
            )
        console.print(table)

        first = model.project_player(target["element_id"], model.horizon_start, simulate=6000)
        breakdown = Table(title=f"\nGW{model.horizon_start} components",
                          box=None, header_style="bold")
        breakdown.add_column("component"); breakdown.add_column("points", justify="right")
        for component, value in first.components().items():
            if abs(value) >= 0.005:
                breakdown.add_row(component, f"{value:+.2f}")
        breakdown.add_row("[bold]total[/]", f"[bold]{first.xp_mean:.2f}[/]")
        console.print(breakdown)
        console.print(f"\nP(haul 10+) [bold]{first.p_haul_10plus * 100:.1f}%[/]   "
                      f"P(blank) [bold]{first.p_blank_2minus * 100:.0f}%[/]   "
                      f"p10-p90 [bold]{first.xp_p10:.0f}-{first.xp_p90:.0f}[/]")
        for note in first.notes:
            console.print(f"[yellow]note[/] {note}")
    finally:
        db.close()


if __name__ == "__main__":
    app()
