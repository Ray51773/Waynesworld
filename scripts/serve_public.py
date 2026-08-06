"""Start the public web app, preparing public data if the store is empty.

Designed for hosts such as Render, Railway, or Fly.io. It does not need a private
FPL account: it loads public bootstrap/fixture data and the public historical
dataset used by the model, then starts FastAPI on the host-provided port.
"""

from __future__ import annotations

import os

import uvicorn

from fpl.config import load_config
from fpl.db import Database
from fpl.history import import_season
from fpl.refresh import run_refresh


def _count(db: Database, sql: str) -> int:
    return int(db.scalar(sql) or 0)


def prepare_store() -> None:
    config = load_config()
    db = Database(config.db_path)
    try:
        if _count(db, "SELECT COUNT(*) FROM latest_players_state") == 0:
            run_refresh(config, db, force=True)

        if _count(db, "SELECT COUNT(*) FROM hist_team_match WHERE season = '2025-26'") == 0:
            import_season(db, "2025-26", config.data_dir / "history")
    finally:
        db.close()


def main() -> None:
    prepare_store()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("fpl.web.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
