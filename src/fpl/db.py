"""DuckDB store. Append-only, with append-on-change de-duplication."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import polars as pl

SCHEMA_SQL = Path(__file__).with_name("schema.sql")
VIEWS_SQL = Path(__file__).with_name("views.sql")

# Fields excluded from the change hash: they describe the fetch, not the entity.
_HASH_EXCLUDE = {"snapshot_id", "snapshot_at", "row_hash"}


def row_hash(row: dict[str, Any]) -> str:
    """Stable hash of a mapped row, so identical states are not re-appended."""
    payload = {k: v for k, v in sorted(row.items()) if k not in _HASH_EXCLUDE}
    encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = duckdb.connect(str(path), read_only=read_only)
        # The game runs on UTC (game_settings.timezone). Pin the session to it so a
        # deadline never renders differently depending on the machine's locale.
        self.con.execute("SET TimeZone='UTC'")
        if not read_only:
            self.migrate()

    def migrate(self) -> None:
        self.con.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        self.con.execute(VIEWS_SQL.read_text(encoding="utf-8"))

    def cursor(self) -> "Database":
        """A second handle on the same open database.

        DuckDB allows one writing process at a time, so a long-lived server cannot
        keep opening its own connections to the file — the moment it wants to write,
        it would be fighting itself. Instead the process opens the file once and hands
        out cursors, which share that instance and are safe to use per request.
        """
        child = object.__new__(Database)
        child.path = self.path
        child.con = self.con.cursor()
        # Session settings are per-connection, not inherited from the parent. Without
        # this a cursor hands back local time while everything is labelled UTC, which
        # silently misreports the deadline by an hour in British summer time.
        child.con.execute("SET TimeZone='UTC'")
        return child

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ provenance
    def next_snapshot_id(self) -> int:
        return int(self.con.execute("SELECT nextval('seq_snapshot_id')").fetchone()[0])

    def previous_sha(self, endpoint: str, params: dict[str, Any]) -> str | None:
        row = self.con.execute(
            """
            SELECT content_sha256 FROM snapshots
            WHERE endpoint = ? AND params = ?::JSON AND http_status = 200
            ORDER BY fetched_at DESC LIMIT 1
            """,
            [endpoint, json.dumps(params, sort_keys=True)],
        ).fetchone()
        return row[0] if row else None

    def last_fetch(self, endpoint: str, params: dict[str, Any]) -> tuple[datetime, str] | None:
        """Most recent successful fetch, for TTL decisions. Returns (when, raw_path)."""
        row = self.con.execute(
            """
            SELECT fetched_at, raw_path FROM snapshots
            WHERE endpoint = ? AND params = ?::JSON AND http_status = 200 AND raw_path <> ''
            ORDER BY fetched_at DESC LIMIT 1
            """,
            [endpoint, json.dumps(params, sort_keys=True)],
        ).fetchone()
        return (row[0], row[1]) if row else None

    def record_snapshot(
        self,
        snapshot_id: int,
        endpoint: str,
        params: dict[str, Any],
        url: str,
        fetched_at: datetime,
        http_status: int,
        content_sha256: str,
        raw_path: str,
        n_bytes: int,
        duration_ms: int,
        from_cache: bool,
    ) -> bool:
        """Insert the provenance row. Returns whether the content changed."""
        changed = content_sha256 != (self.previous_sha(endpoint, params) or "")
        self.con.execute(
            """
            INSERT INTO snapshots (snapshot_id, endpoint, url, params, fetched_at,
                                   http_status, content_sha256, raw_path, bytes,
                                   duration_ms, content_changed, from_cache)
            VALUES (?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [snapshot_id, endpoint, url, json.dumps(params, sort_keys=True), fetched_at,
             http_status, content_sha256, raw_path, n_bytes, duration_ms, changed, from_cache],
        )
        return changed

    # ---------------------------------------------------------------- writes
    def append(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        """Unconditional append. Used for immutable long-form tables."""
        if not rows:
            return 0
        frame = pl.DataFrame(rows, infer_schema_length=None, strict=False)
        self.con.register("_incoming", frame)
        try:
            self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _incoming")
        finally:
            self.con.unregister("_incoming")
        return len(rows)

    def append_on_change(
        self,
        table: str,
        rows: Sequence[dict[str, Any]],
        keys: Iterable[str],
    ) -> int:
        """Append only rows whose row_hash differs from that entity's latest version.

        This is the deviation from a literal "append every row every hour": >99% of
        player rows are byte-identical between refreshes outside price-change windows.
        History is still complete, because a row is written the moment anything moves.
        """
        if not rows:
            return 0
        keys = list(keys)
        frame = pl.DataFrame(rows, infer_schema_length=None, strict=False)
        join_pred = " AND ".join(f"cur.{k} = inc.{k}" for k in keys)
        key_cols = ", ".join(keys)

        self.con.register("_incoming", frame)
        try:
            before = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if before == 0:
                self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _incoming")
                return len(rows)

            self.con.execute(
                f"""
                INSERT INTO {table} BY NAME
                SELECT inc.* FROM _incoming inc
                WHERE NOT EXISTS (
                    SELECT 1 FROM (
                        SELECT {key_cols}, row_hash FROM {table}
                        QUALIFY ROW_NUMBER() OVER (
                            PARTITION BY {key_cols} ORDER BY snapshot_at DESC
                        ) = 1
                    ) cur
                    WHERE {join_pred} AND cur.row_hash = inc.row_hash
                )
                """
            )
            after = int(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return after - before
        finally:
            self.con.unregister("_incoming")

    # ----------------------------------------------------------------- reads
    def query(self, sql: str, params: Sequence[Any] | None = None) -> pl.DataFrame:
        return self.con.execute(sql, list(params) if params else []).pl()

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.con.execute(sql, list(params) if params else []).fetchone()
        return row[0] if row else None

    def table_counts(self) -> pl.DataFrame:
        tables = [
            r[0]
            for r in self.con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
            ).fetchall()
        ]
        rows = []
        for name in tables:
            count = self.con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            if count:
                rows.append({"table": name, "rows": int(count)})
        return pl.DataFrame(rows) if rows else pl.DataFrame({"table": [], "rows": []})
