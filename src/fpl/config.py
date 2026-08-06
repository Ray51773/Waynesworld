"""Configuration loading. TOML file, overridable by environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _find_root(start: Path | None = None) -> Path:
    """Walk up from here looking for config.toml, so the CLI works from any cwd."""
    here = (start or Path(__file__).resolve()).parent
    for candidate in [here, *here.parents]:
        if (candidate / "config.toml").exists():
            return candidate
    return Path.cwd()


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str = "fpl-optimiser/0.1"
    timeout_seconds: float = 20.0
    max_retries: int = 5
    min_interval_seconds: float = 0.15
    player_concurrency: int = 8
    ttl_seconds: dict[str, int] = field(default_factory=dict)

    def ttl(self, endpoint: str) -> int:
        return int(self.ttl_seconds.get(endpoint, 300))


@dataclass(frozen=True)
class Config:
    root: Path
    manager_id: int
    mini_league_ids: list[int]
    season: str
    first_chip_set_last_event: int
    target_rank: str
    risk_posture: str
    horizon: int
    decay: float
    bench_weight: float
    data_dir: Path
    http: HttpConfig

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fpl.duckdb"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def has_manager(self) -> bool:
        return self.manager_id > 0


def load_config(root: Path | None = None) -> Config:
    root = root or _find_root()
    raw: dict = {}
    cfg_file = root / "config.toml"
    if cfg_file.exists():
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))

    manager = raw.get("manager", {})
    season = raw.get("season", {})
    strategy = raw.get("strategy", {})
    data = raw.get("data", {})
    http = raw.get("http", {})

    manager_id = int(os.environ.get("FPL_MANAGER_ID", manager.get("manager_id", 0)))

    data_dir = Path(os.environ.get("FPL_DATA_DIR", data.get("dir", "data")))
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    return Config(
        root=root,
        manager_id=manager_id,
        mini_league_ids=[int(x) for x in manager.get("mini_league_ids", [])],
        season=str(season.get("name", "2026/27")),
        first_chip_set_last_event=int(season.get("first_chip_set_last_event", 19)),
        target_rank=str(strategy.get("target_rank", "")),
        risk_posture=str(os.environ.get("FPL_RISK_POSTURE", strategy.get("risk_posture", "balanced"))),
        horizon=int(strategy.get("horizon", 6)),
        decay=float(strategy.get("decay", 0.85)),
        bench_weight=float(strategy.get("bench_weight", 0.15)),
        data_dir=data_dir,
        http=HttpConfig(
            user_agent=str(http.get("user_agent", "fpl-optimiser/0.1")),
            timeout_seconds=float(http.get("timeout_seconds", 20.0)),
            max_retries=int(http.get("max_retries", 5)),
            min_interval_seconds=float(http.get("min_interval_seconds", 0.15)),
            player_concurrency=int(http.get("player_concurrency", 8)),
            ttl_seconds={str(k): int(v) for k, v in http.get("ttl_seconds", {}).items()},
        ),
    )
