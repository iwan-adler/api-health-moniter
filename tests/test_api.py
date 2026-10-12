import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def fake_internet(request: httpx.Request) -> httpx.Response:
    """Answers requests by hostname so each test scenario is deterministic and offline."""
    match request.url.host:
        case "up.test":
            return httpx.Response(200)
        case "down.test":
            return httpx.Response(503)
        case "slow.test":
            raise httpx.ReadTimeout("timed out", request=request)
        case _:
            raise httpx.ConnectError("Name or service not known", request=request)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def client(db_path):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake_internet))
    with TestClient(create_app(db_path=db_path, http_client=http_client)) as test_client:
        yield test_client


def add(client, url: str, **overrides) -> dict:
    response = client.post("/endpoints", json={"name": url, "url": url, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


# --- service ---------------------------------------------------------------


def test_root_and_self_health(client):
    assert client.get("/").json()["status"] == "running"
    assert client.get("/health").json() == {"status": "healthy"}


# --- adding and listing endpoints ------------------------------------------


def test_add_endpoint_applies_defaults(client):
    created = add(client, "https://up.test/health")

    assert created["id"] == 1
    assert created["url"] == "https://up.test/health"
    assert created["method"] == "GET"
    assert created["expected_status"] == 200
    assert created["timeout_seconds"] == 5.0


def test_list_and_get_endpoints(client):
    first = add(client, "https://up.test/a")
    add(client, "https://up.test/b")

    listed = client.get("/endpoints").json()
    assert [e["url"] for e in listed] == ["https://up.test/a", "https://up.test/b"]
    assert client.get(f"/endpoints/{first['id']}").json() == first


def test_name_whitespace_is_stripped(client):
    created = add(client, "https://up.test/", name="  My API  ")
    assert created["name"] == "My API"


def test_duplicate_url_is_rejected(client):
    add(client, "https://up.test/")
    response = client.post("/endpoints", json={"name": "again", "url": "https://up.test/"})
    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "url": "not a url"},
        {"name": "x", "url": "ftp://files.test/"},  # only http(s)
        {"name": "", "url": "https://up.test/"},
        {"name": "   ", "url": "https://up.test/"},  # blank after stripping
        {"name": "x" * 101, "url": "https://up.test/"},
        {"name": "x", "url": "https://up.test/", "method": "POST"},
        {"name": "x", "url": "https://up.test/", "timeout_seconds": 0},
        {"name": "x", "url": "https://up.test/", "timeout_seconds": 61},
        {"name": "x", "url": "https://up.test/", "expected_status": 99},
        {"name": "x", "url": "https://up.test/", "expected_status": 600},
        {"url": "https://up.test/"},  # missing name
    ],
)
def test_invalid_endpoint_input_is_rejected(client, payload):
    response = client.post("/endpoints", json=payload)
    assert response.status_code == 422
    assert client.get("/endpoints").json() == []


def test_boundary_values_are_accepted(client):
    add(client, "https://up.test/a", timeout_seconds=60, expected_status=599)
    add(client, "https://up.test/b", timeout_seconds=0.1, expected_status=100, name="x" * 100)


def test_unknown_endpoint_returns_404(client):
    assert client.get("/endpoints/999").status_code == 404
    assert client.delete("/endpoints/999").status_code == 404
    assert client.post("/endpoints/999/check").status_code == 404
    assert client.get("/endpoints/999/checks").status_code == 404


# --- running checks ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected_status", "status_code", "error"),
    [
        ("https://up.test/", "healthy", 200, None),
        ("https://down.test/", "unhealthy", 503, "Expected status 200, got 503"),
        ("https://slow.test/", "unreachable", None, "Timed out after 5.0s"),
        ("https://nowhere.test/", "unreachable", None, "Connection failed: Name or service not known"),
    ],
)
def test_check_endpoint_records_outcome(client, url, expected_status, status_code, error):
    endpoint = add(client, url)

    result = client.post(f"/endpoints/{endpoint['id']}/check").json()

    assert result["endpoint_id"] == endpoint["id"]
    assert result["status"] == expected_status
    assert result["status_code"] == status_code
    assert result["error"] == error
    assert result["checked_at"]


def test_expected_status_is_configurable(client):
    endpoint = add(client, "https://down.test/", expected_status=503)
    assert client.post(f"/endpoints/{endpoint['id']}/check").json()["status"] == "healthy"


def test_check_all_runs_every_endpoint(client):
    add(client, "https://up.test/")
    add(client, "https://down.test/")
    add(client, "https://nowhere.test/")

    results = client.post("/checks/run").json()

    assert sorted(r["status"] for r in results) == ["healthy", "unhealthy", "unreachable"]


def test_check_all_with_no_endpoints(client):
    assert client.post("/checks/run").json() == []


# --- history and status ------------------------------------------------------


def test_history_is_newest_first_and_limited(client):
    endpoint = add(client, "https://up.test/")
    ids = [client.post(f"/endpoints/{endpoint['id']}/check").json()["id"] for _ in range(3)]

    history = client.get(f"/endpoints/{endpoint['id']}/checks").json()
    assert [r["id"] for r in history] == list(reversed(ids))

    limited = client.get(f"/endpoints/{endpoint['id']}/checks", params={"limit": 2}).json()
    assert [r["id"] for r in limited] == [ids[2], ids[1]]


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_history_limit_is_validated(client, limit):
    endpoint = add(client, "https://up.test/")
    response = client.get(f"/endpoints/{endpoint['id']}/checks", params={"limit": limit})
    assert response.status_code == 422


def test_status_overview(client):
    up = add(client, "https://up.test/")
    flaky = add(client, "https://down.test/")
    add(client, "https://never-checked.test/")

    client.post(f"/endpoints/{up['id']}/check")
    client.post(f"/endpoints/{up['id']}/check")
    client.post(f"/endpoints/{flaky['id']}/check")

    overview = {s["endpoint"]["url"]: s for s in client.get("/status").json()}

    assert overview["https://up.test/"]["uptime_percent"] == 100.0
    assert overview["https://up.test/"]["checks_counted"] == 2
    assert overview["https://up.test/"]["latest_check"]["status"] == "healthy"
    assert overview["https://down.test/"]["uptime_percent"] == 0.0

    never = overview["https://never-checked.test/"]
    assert never["latest_check"] is None
    assert never["checks_counted"] == 0
    assert never["uptime_percent"] is None
    assert never["avg_response_time_ms"] is None


def test_delete_removes_endpoint_and_history(client, db_path):
    endpoint = add(client, "https://up.test/")
    client.post(f"/endpoints/{endpoint['id']}/check")

    assert client.delete(f"/endpoints/{endpoint['id']}").status_code == 204
    assert client.get("/endpoints").json() == []

    from app.db import Database

    assert Database(db_path).list_results(endpoint["id"]) == []  # cascaded


# --- persistence -------------------------------------------------------------


def test_data_survives_restart(db_path):
    """A new app instance pointed at the same file sees earlier endpoints and results."""
    transport = httpx.MockTransport(fake_internet)

    with TestClient(create_app(db_path, httpx.AsyncClient(transport=transport))) as first:
        endpoint = add(first, "https://up.test/")
        first.post(f"/endpoints/{endpoint['id']}/check")

    with TestClient(create_app(db_path, httpx.AsyncClient(transport=transport))) as second:
        assert [e["url"] for e in second.get("/endpoints").json()] == ["https://up.test/"]
        assert len(second.get(f"/endpoints/{endpoint['id']}/checks").json()) == 1


def test_periodic_checks_run_in_background(db_path):
    import time

    transport = httpx.MockTransport(fake_internet)
    app = create_app(db_path, httpx.AsyncClient(transport=transport), check_interval_seconds=0.05)

    with TestClient(app) as client:
        endpoint = add(client, "https://up.test/")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get(f"/endpoints/{endpoint['id']}/checks").json():
                break
            time.sleep(0.05)
        history = client.get(f"/endpoints/{endpoint['id']}/checks").json()

    assert history and history[0]["status"] == "healthy"
