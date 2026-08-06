"""HTTP client for the public FPL API, plus raw snapshot storage.

Two rules this module exists to enforce:

1.  The raw bytes hit disk before anything tries to parse them. A loader bug must
    never be able to lose a fetch.
2.  We are a guest on someone else's API. Rate limited, backed off, TTL cached.
    The API sends no ETags (FINDINGS.md caveat 6), so conditional requests are not
    available to us; TTL plus content hashing is the substitute.

Read-only. There is no code path here that issues anything but GET.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

BASE_URL = "https://fantasy.premierleague.com/api/"


@dataclass(frozen=True)
class FetchResult:
    endpoint: str
    params: dict[str, Any]
    url: str
    data: Any
    fetched_at: datetime
    http_status: int
    content_sha256: str
    raw_path: str
    n_bytes: int
    duration_ms: int
    from_cache: bool


class RateLimiter:
    """Minimum wall-clock spacing between requests, shared across threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class SnapshotStore:
    """Gzipped raw JSON on disk, one file per fetch, never overwritten."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, endpoint: str, params: dict[str, Any]) -> Path:
        parts = [endpoint]
        for key in ("player_id", "event", "manager_id", "league_id"):
            if key in params:
                parts.append(f"{key}={params[key]}")
        return self.root.joinpath(*parts)

    def write(self, endpoint: str, params: dict[str, Any], body: bytes, fetched_at: datetime) -> Path:
        target_dir = self._dir(endpoint, params)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
        path = target_dir / f"{stamp}.json.gz"
        with gzip.open(path, "wb", compresslevel=6) as fh:
            fh.write(body)
        return path

    def read(self, path: str | Path) -> Any:
        with gzip.open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root.parent.parent))
        except ValueError:
            return str(path)


class FPLClient:
    """GET-only client. Retries with exponential backoff on any non-200."""

    def __init__(
        self,
        store: SnapshotStore,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 5,
        min_interval: float = 0.15,
    ) -> None:
        self.store = store
        self.max_retries = max_retries
        self.limiter = RateLimiter(min_interval)
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FPLClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(
        self,
        endpoint: str,
        path: str,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> FetchResult:
        params = params or {}
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                response = self._client.get(path)
            except httpx.HTTPError as exc:
                last_error = exc
                self._backoff(attempt)
                continue

            if response.status_code == 200:
                body = response.content
                fetched_at = datetime.now(timezone.utc)
                raw_path = self.store.write(endpoint, params, body, fetched_at)
                return FetchResult(
                    endpoint=endpoint,
                    params=params,
                    url=str(response.url),
                    data=json.loads(body.decode("utf-8")),
                    fetched_at=fetched_at,
                    http_status=200,
                    content_sha256=hashlib.sha256(body).hexdigest(),
                    raw_path=self.store.relative(raw_path),
                    n_bytes=len(body),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    from_cache=False,
                )

            if response.status_code == 404 and allow_404:
                # Expected pre-season for picks; not an error worth retrying.
                return FetchResult(
                    endpoint=endpoint,
                    params=params,
                    url=str(response.url),
                    data=None,
                    fetched_at=datetime.now(timezone.utc),
                    http_status=404,
                    content_sha256="",
                    raw_path="",
                    n_bytes=0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    from_cache=False,
                )

            last_error = httpx.HTTPStatusError(
                f"{response.status_code} for {response.url}",
                request=response.request,
                response=response,
            )
            self._backoff(attempt)

        raise RuntimeError(f"giving up on {endpoint} after {self.max_retries} attempts") from last_error

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2.0**attempt, 30.0))

    def fetch_many(
        self,
        endpoint: str,
        jobs: Iterable[tuple[str, dict[str, Any]]],
        concurrency: int = 8,
    ) -> list[FetchResult]:
        """Parallel fetch, still rate limited globally by the shared limiter."""
        jobs = list(jobs)
        results: list[FetchResult] = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(self.fetch, endpoint, path, params) for path, params in jobs]
            for future in futures:
                results.append(future.result())
        return results


# Endpoint helpers, so paths are declared in exactly one place.
def path_bootstrap() -> str:
    return "bootstrap-static/"


def path_fixtures() -> str:
    return "fixtures/"


def path_element_summary(player_id: int) -> str:
    return f"element-summary/{player_id}/"


def path_event_live(event: int) -> str:
    return f"event/{event}/live/"


def path_entry(manager_id: int) -> str:
    return f"entry/{manager_id}/"


def path_entry_history(manager_id: int) -> str:
    return f"entry/{manager_id}/history/"


def path_entry_transfers(manager_id: int) -> str:
    return f"entry/{manager_id}/transfers/"


def path_entry_picks(manager_id: int, event: int) -> str:
    return f"entry/{manager_id}/event/{event}/picks/"


def path_league_standings(league_id: int) -> str:
    return f"leagues-classic/{league_id}/standings/"
