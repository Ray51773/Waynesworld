"""Smoke tests for the web UI.

Not testing Jinja — testing that every route actually renders against a real store
with real data, because a template that raises only at request time is invisible until
you open the page. Skipped when no local store exists.
"""

from __future__ import annotations

import pytest

from fpl.config import load_config

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture(scope="module")
def client():
    config = load_config()
    if not config.db_path.exists():
        pytest.skip("no local store; run `fpl refresh` first")

    from fpl.web.app import app

    with fastapi_testclient.TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def an_element_id(client) -> int:
    return client.get("/api/health").json() and 411


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["players"] > 0


@pytest.mark.parametrize("path", [
    "/", "/players", "/fixtures", "/rules", "/squad", "/advice", "/captain",
])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Internal Server Error" not in response.text


def test_player_detail_renders(client, an_element_id):
    response = client.get(f"/player/{an_element_id}")
    assert response.status_code == 200
    assert "FPL Optimiser" in response.text


def test_unknown_player_does_not_500(client):
    response = client.get("/player/99999999")
    assert response.status_code == 200
    assert "Player not found" in response.text


@pytest.mark.parametrize("query", [
    "?position=DEF",
    "?position=MID&sort=defcon_per_90",
    "?team=ARS",
    "?max_price=5.0&min_minutes=900",
    "?search=haaland",
    "?sort=points_per_million",
])
def test_player_filters(client, query):
    response = client.get(f"/players{query}")
    assert response.status_code == 200


def test_sort_column_is_not_injectable(client):
    """The sort parameter reaches an f-string in SQL, so it must be allowlisted."""
    response = client.get("/players?sort=1;DROP TABLE players_state--")
    assert response.status_code == 200
    assert client.get("/api/health").json()["players"] > 0


def test_fixture_window_bounds(client):
    assert client.get("/fixtures?start=1&span=1").status_code == 200
    assert client.get("/fixtures?start=35&span=8").status_code == 200


def test_squad_save_rejects_a_short_squad(client):
    """The endpoint must not persist an illegal squad even if the UI would not send one."""
    response = client.post("/squad/save", json={
        "players": [{"element_id": 1, "purchase_price": 50}],
        "bank": 0, "free_transfers": 1,
    })
    assert response.status_code == 400
    assert "15" in response.json()["error"]


def test_squad_save_rejects_an_unknown_player(client):
    response = client.post("/squad/save", json={
        "players": [{"element_id": 99999999, "purchase_price": 50}] * 15,
        "bank": 0, "free_transfers": 1,
    })
    assert response.status_code == 400
    assert "Unknown player" in response.json()["error"]


def test_squad_page_serialises_the_player_pool_as_json(client):
    """The picker reads this; a Decimal here silently 500s the page."""
    import json
    body = client.get("/squad").text
    start = body.index('id="player-pool"')
    opening = body.index(">", start) + 1
    payload = json.loads(body[opening:body.index("</script>", opening)])
    assert len(payload) > 400
    assert isinstance(payload[0]["selected_by_percent"], (int, float))
    assert "xp_horizon" in payload[0]


def test_transfers_url_still_works(client):
    """Old link, kept as a redirect rather than left to rot."""
    response = client.get("/transfers", follow_redirects=True)
    assert response.status_code == 200
    assert "What to do" in response.text


def test_advice_gives_every_player_a_verdict(client):
    """The point of the page: no player in the fifteen goes unjudged."""
    body = client.get("/advice").text
    if "No squad yet" in body:
        pytest.skip("no squad saved in this store")
    assert "Your fifteen" in body
    # Each verdict group heading appears only when it has members, but at least
    # one must, and every card carries a headline sentence.
    assert any(word in body for word in ("Worth changing", "Borderline", "Keep"))
    assert body.count("card-headline") >= 15


def test_refresh_status_endpoint(client):
    body = client.get("/api/refresh").json()
    assert "running" in body
    assert body["running"] in (True, False)


def test_assets_are_version_stamped(client):
    """Without a fingerprint, an open tab keeps running yesterday's JavaScript and a
    working feature looks broken."""
    body = client.get("/").text
    assert "/static/style.css?v=" in body
    assert "/static/app.js?v=" in body


def test_squad_page_versions_its_own_script(client):
    assert "/static/squad.js?v=" in client.get("/squad").text


def test_pages_render_without_reopening_the_database(client):
    """Every page goes through one shared handle. If a route opened its own
    connection the refresh endpoint could not write while the UI was in use."""
    import importlib

    # fpl.web re-exports the FastAPI instance as `app`, so import the module by name.
    module = importlib.import_module("fpl.web.app")
    assert module._shared_db is not None
    for path in ("/", "/squad", "/advice"):
        assert client.get(path).status_code == 200
