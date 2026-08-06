"""Refresh orchestration: fetch, snapshot, normalise, append.

TTL rules live here rather than in the client, because deciding whether a fetch is
necessary needs the snapshot history, which is the database's job to know.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import loaders
from .client import (
    FPLClient,
    FetchResult,
    SnapshotStore,
    path_bootstrap,
    path_element_summary,
    path_entry,
    path_entry_history,
    path_entry_picks,
    path_entry_transfers,
    path_event_live,
    path_fixtures,
    path_league_standings,
)
from .config import Config
from .db import Database


@dataclass
class RefreshReport:
    started_at: datetime
    finished_at: datetime | None = None
    fetches: int = 0
    cache_hits: int = 0
    bytes_fetched: int = 0
    rows_written: dict[str, int] = field(default_factory=dict)
    unchanged_endpoints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def add_rows(self, table: str, n: int) -> None:
        if n:
            self.rows_written[table] = self.rows_written.get(table, 0) + n


class Refresher:
    def __init__(self, config: Config, db: Database, client: FPLClient) -> None:
        self.config = config
        self.db = db
        self.client = client

    # ------------------------------------------------------------- plumbing
    def _fetch(
        self,
        endpoint: str,
        path: str,
        params: dict[str, Any] | None = None,
        force: bool = False,
        allow_404: bool = False,
    ) -> tuple[Any, int, datetime, bool]:
        """Return (data, snapshot_id, snapshot_at, changed), honouring the TTL cache."""
        params = params or {}
        ttl = self.config.http.ttl(endpoint)

        if not force and ttl > 0:
            last = self.db.last_fetch(endpoint, params)
            if last is not None:
                when, raw_path = last
                age = (datetime.now(timezone.utc) - when).total_seconds()
                if age < ttl:
                    full = self.config.root / raw_path
                    if full.exists():
                        data = self.client.store.read(full)
                        return data, -1, when, False

        result = self.client.fetch(endpoint, path, params, allow_404=allow_404)
        snapshot_id = self.db.next_snapshot_id()
        changed = self.db.record_snapshot(
            snapshot_id=snapshot_id,
            endpoint=endpoint,
            params=params,
            url=result.url,
            fetched_at=result.fetched_at,
            http_status=result.http_status,
            content_sha256=result.content_sha256,
            raw_path=result.raw_path,
            n_bytes=result.n_bytes,
            duration_ms=result.duration_ms,
            from_cache=False,
        )
        return result.data, snapshot_id, result.fetched_at, changed

    # ------------------------------------------------------------ endpoints
    def refresh_bootstrap(self, report: RefreshReport, force: bool = False) -> dict | None:
        data, sid, at, changed = self._fetch("bootstrap-static", path_bootstrap(), force=force)
        if data is None:
            return None
        if sid == -1:
            report.cache_hits += 1
            report.notes.append("bootstrap-static served from TTL cache")
            return data
        report.fetches += 1
        if not changed:
            report.unchanged_endpoints.append("bootstrap-static")

        report.add_rows("teams", self.db.append_on_change(
            "teams", loaders.load_teams(data, sid, at), ["team_id"]))
        report.add_rows("element_types", self.db.append_on_change(
            "element_types", loaders.load_element_types(data, sid, at), ["element_type"]))
        report.add_rows("events", self.db.append_on_change(
            "events", loaders.load_events(data, sid, at), ["event_id"]))
        report.add_rows("chips_config", self.db.append_on_change(
            "chips_config", loaders.load_chips_config(data, sid, at), ["chip_id"]))
        report.add_rows("game_rules", self.db.append_on_change(
            "game_rules", loaders.load_game_rules(data, sid, at), ["rule_name"]))
        report.add_rows("scoring_rules", self.db.append_on_change(
            "scoring_rules", loaders.load_scoring_rules(data, sid, at), ["rule_name", "position"]))
        report.add_rows("players_identity", self.db.append_on_change(
            "players_identity", loaders.load_players_identity(data, sid, at), ["element_id"]))
        report.add_rows("players_state", self.db.append_on_change(
            "players_state", loaders.load_players_state(data, sid, at), ["element_id"]))
        return data

    def refresh_fixtures(self, report: RefreshReport, force: bool = False) -> list | None:
        data, sid, at, changed = self._fetch("fixtures", path_fixtures(), force=force)
        if data is None:
            return None
        if sid == -1:
            report.cache_hits += 1
            report.notes.append("fixtures served from TTL cache")
            return data
        report.fetches += 1
        if not changed:
            report.unchanged_endpoints.append("fixtures")

        report.add_rows("fixtures", self.db.append_on_change(
            "fixtures", loaders.load_fixtures(data, sid, at), ["fixture_id"]))
        stats = loaders.load_fixture_stats(data, sid, at)
        report.add_rows("fixture_stats", self.db.append("fixture_stats", stats))
        if not stats:
            report.notes.append("no post-match fixture stats yet (pre-season)")
        return data

    def refresh_players(
        self,
        report: RefreshReport,
        element_ids: list[int],
        force: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """element-summary for every player: 570 requests, the expensive path.

        Fetched concurrently because sequentially it cannot meet the two-minute cold
        refresh target. Writes stay on this thread — the DuckDB connection is not
        shared — and the client's rate limiter is global, so concurrency raises
        throughput without raising the request rate beyond what we set.
        """
        total = len(element_ids)
        to_fetch: list[int] = []
        cached: list[tuple[int, Any, datetime]] = []

        # TTL decisions need the DB, so make them all up front on this thread.
        for element_id in element_ids:
            params = {"player_id": element_id}
            hit = None
            if not force and self.config.http.ttl("element-summary") > 0:
                last = self.db.last_fetch("element-summary", params)
                if last is not None:
                    when, raw_path = last
                    age = (datetime.now(timezone.utc) - when).total_seconds()
                    full = self.config.root / raw_path
                    if age < self.config.http.ttl("element-summary") and full.exists():
                        hit = (element_id, self.client.store.read(full), when)
            if hit is not None:
                cached.append(hit)
                report.cache_hits += 1
            else:
                to_fetch.append(element_id)

        done = len(cached)
        if progress and done:
            progress(done, total)

        results: list[tuple[int, Any, int, datetime]] = []
        if to_fetch:
            jobs = [(path_element_summary(pid), {"player_id": pid}) for pid in to_fetch]
            try:
                fetched = self.client.fetch_many(
                    "element-summary", jobs, concurrency=self.config.http.player_concurrency
                )
            except RuntimeError as exc:
                report.errors.append(str(exc))
                fetched = []

            for result in fetched:
                element_id = int(result.params["player_id"])
                snapshot_id = self.db.next_snapshot_id()
                self.db.record_snapshot(
                    snapshot_id=snapshot_id, endpoint="element-summary",
                    params=result.params, url=result.url, fetched_at=result.fetched_at,
                    http_status=result.http_status, content_sha256=result.content_sha256,
                    raw_path=result.raw_path, n_bytes=result.n_bytes,
                    duration_ms=result.duration_ms, from_cache=False,
                )
                report.fetches += 1
                report.bytes_fetched += result.n_bytes
                results.append((element_id, result.data, snapshot_id, result.fetched_at))
                done += 1
                if progress:
                    progress(done, total)

        # Batch the loads: one append per table for the whole squad list beats 570
        # round trips through polars and DuckDB.
        history_rows: list[dict] = []
        past_rows: list[dict] = []
        upcoming_rows: list[dict] = []
        for element_id, data, snapshot_id, at in results:
            if not data:
                continue
            history_rows += loaders.load_player_gw_history(data, snapshot_id, at)
            past_rows += loaders.load_player_past_seasons(data, snapshot_id, at)
            upcoming_rows += loaders.load_player_upcoming_fixtures(data, element_id, snapshot_id, at)

        report.add_rows("player_gw_history", self.db.append_on_change(
            "player_gw_history", history_rows, ["element_id", "fixture_id"]))
        report.add_rows("player_past_seasons", self.db.append_on_change(
            "player_past_seasons", past_rows, ["element_code", "season_name"]))
        report.add_rows("player_upcoming_fixtures", self.db.append_on_change(
            "player_upcoming_fixtures", upcoming_rows, ["element_id", "fixture_id"]))

    def refresh_entry(self, report: RefreshReport, force: bool = False) -> None:
        manager_id = self.config.manager_id
        if not self.config.has_manager:
            # Optional: everything works from a hand-entered squad. Nothing to say.
            return

        params = {"manager_id": manager_id}
        data, sid, at, _ = self._fetch("entry", path_entry(manager_id), params, force=force)
        if sid != -1 and data:
            report.fetches += 1
            report.add_rows("my_entry", self.db.append_on_change(
                "my_entry", loaders.load_my_entry(data, sid, at), ["manager_id"]))
        elif sid == -1:
            report.cache_hits += 1

        hist, sid, at, _ = self._fetch("entry", path_entry_history(manager_id),
                                       {"manager_id": manager_id, "part": "history"}, force=force)
        if sid != -1 and hist:
            report.fetches += 1
            report.add_rows("my_entry_history", self.db.append_on_change(
                "my_entry_history", loaders.load_my_entry_history(hist, manager_id, sid, at),
                ["manager_id", "event"]))
            report.add_rows("my_past_seasons", self.db.append_on_change(
                "my_past_seasons", loaders.load_my_past_seasons(hist, manager_id, sid, at),
                ["manager_id", "season_name"]))
            report.add_rows("my_chips", self.db.append_on_change(
                "my_chips",
                loaders.load_my_chips(hist, manager_id,
                                      self.config.first_chip_set_last_event, sid, at),
                ["manager_id", "chip_name", "chip_set"]))
        elif sid == -1:
            report.cache_hits += 1

        tr, sid, at, _ = self._fetch("entry", path_entry_transfers(manager_id),
                                     {"manager_id": manager_id, "part": "transfers"}, force=force)
        if sid != -1 and tr is not None:
            report.fetches += 1
            report.add_rows("my_transfers", self.db.append_on_change(
                "my_transfers", loaders.load_my_transfers(tr, manager_id, sid, at),
                ["manager_id", "element_in", "element_out", "transfer_time"]))
        elif sid == -1:
            report.cache_hits += 1

    def refresh_picks(self, report: RefreshReport, event: int, force: bool = False) -> None:
        if not self.config.has_manager:
            return
        manager_id = self.config.manager_id
        params = {"manager_id": manager_id, "event": event}
        data, sid, at, _ = self._fetch(
            "entry", path_entry_picks(manager_id, event), params, force=force, allow_404=True
        )
        if sid == -1:
            report.cache_hits += 1
            return
        report.fetches += 1
        if data is None:
            report.notes.append(
                f"picks for GW{event} not published yet (404) - expected before the deadline"
            )
            return
        report.add_rows("my_picks", self.db.append_on_change(
            "my_picks", loaders.load_my_picks(data, manager_id, event, sid, at),
            ["manager_id", "event", "element_id"]))

    def refresh_live(self, report: RefreshReport, event: int, is_final: bool,
                     force: bool = False) -> None:
        params = {"event": event}
        data, sid, at, _ = self._fetch("event-live", path_event_live(event), params, force=force)
        if sid == -1:
            report.cache_hits += 1
            return
        report.fetches += 1
        if not data or not data.get("elements"):
            report.notes.append(f"no live data for GW{event} yet")
            return
        report.add_rows("event_live", self.db.append_on_change(
            "event_live", loaders.load_event_live(data, event, is_final, sid, at),
            ["event", "element_id"]))
        report.add_rows("event_live_explain", self.db.append(
            "event_live_explain", loaders.load_event_live_explain(data, event, sid, at)))

    def refresh_leagues(self, report: RefreshReport, force: bool = False) -> None:
        for league_id in self.config.mini_league_ids:
            params = {"league_id": league_id}
            data, sid, at, _ = self._fetch("league", path_league_standings(league_id),
                                           params, force=force)
            if sid == -1:
                report.cache_hits += 1
                continue
            report.fetches += 1
            report.add_rows("league_standings", self.db.append_on_change(
                "league_standings", loaders.load_league_standings(data, league_id, sid, at),
                ["league_id", "entry_id"]))


def current_and_next_event(db: Database) -> tuple[int | None, int | None]:
    row = db.con.execute(
        "SELECT MAX(CASE WHEN is_current THEN event_id END), "
        "       MAX(CASE WHEN is_next THEN event_id END) FROM latest_events"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def run_refresh(
    config: Config,
    db: Database,
    include_players: bool = False,
    include_live: bool = False,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> RefreshReport:
    report = RefreshReport(started_at=datetime.now(timezone.utc))
    store = SnapshotStore(config.snapshot_dir)
    with FPLClient(
        store=store,
        user_agent=config.http.user_agent,
        timeout=config.http.timeout_seconds,
        max_retries=config.http.max_retries,
        min_interval=config.http.min_interval_seconds,
    ) as client:
        refresher = Refresher(config, db, client)
        bootstrap = refresher.refresh_bootstrap(report, force=force)
        refresher.refresh_fixtures(report, force=force)
        refresher.refresh_entry(report, force=force)
        refresher.refresh_leagues(report, force=force)

        current, nxt = current_and_next_event(db)
        if current:
            refresher.refresh_picks(report, current, force=force)
            if include_live:
                is_final = bool(db.scalar(
                    "SELECT data_checked FROM latest_events WHERE event_id = ?", [current]))
                refresher.refresh_live(report, current, is_final, force=force)
        elif nxt:
            report.notes.append(
                f"season not started; next deadline is GW{nxt}. "
                "Squad picks become public after that deadline."
            )

        if include_players and bootstrap:
            ids = [e["id"] for e in bootstrap["elements"]]
            refresher.refresh_players(report, ids, force=force, progress=progress)

    report.finished_at = datetime.now(timezone.utc)
    return report
