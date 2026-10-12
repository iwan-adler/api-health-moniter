import httpx
import pytest

from app.monitor import HealthStatus, check_endpoint

pytestmark = pytest.mark.anyio


def client_for(handler) -> httpx.AsyncClient:
    """An AsyncClient whose requests are answered by `handler` instead of the network."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_expected_status_is_healthy():
    async with client_for(lambda request: httpx.Response(200)) as client:
        result = await check_endpoint(client, "https://api.test/health")

    assert result.status is HealthStatus.HEALTHY
    assert result.status_code == 200
    assert result.response_time_ms is not None and result.response_time_ms >= 0
    assert result.error is None
    assert result.checked_at.tzinfo is not None


async def test_unexpected_status_is_unhealthy():
    async with client_for(lambda request: httpx.Response(503)) as client:
        result = await check_endpoint(client, "https://api.test/health")

    assert result.status is HealthStatus.UNHEALTHY
    assert result.status_code == 503
    assert result.error == "Expected status 200, got 503"


async def test_custom_expected_status():
    async with client_for(lambda request: httpx.Response(204)) as client:
        result = await check_endpoint(client, "https://api.test/ping", expected_status=204)

    assert result.status is HealthStatus.HEALTHY


async def test_uses_configured_method():
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200)

    async with client_for(handler) as client:
        await check_endpoint(client, "https://api.test/", method="HEAD")

    assert seen == ["HEAD"]


async def test_follows_redirects():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://api.test/new"})
        return httpx.Response(200)

    async with client_for(handler) as client:
        result = await check_endpoint(client, "https://api.test/old")

    assert result.status is HealthStatus.HEALTHY


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (httpx.ConnectTimeout("slow"), "Timed out after 2.0s"),
        (httpx.ReadTimeout("slow"), "Timed out after 2.0s"),
        (httpx.ConnectError("refused"), "Connection failed: refused"),
        (httpx.RemoteProtocolError("garbage"), "RemoteProtocolError: garbage"),
    ],
)
async def test_network_failures_are_unreachable(exception, expected_error):
    def handler(request):
        raise exception

    async with client_for(handler) as client:
        result = await check_endpoint(client, "https://api.test/", timeout_seconds=2.0)

    assert result.status is HealthStatus.UNREACHABLE
    assert result.status_code is None
    assert result.response_time_ms is None
    assert result.error == expected_error


async def test_real_closed_port_is_unreachable():
    """No mock: port 9 on localhost has nothing listening, so the OS refuses the connection."""
    async with httpx.AsyncClient() as client:
        result = await check_endpoint(client, "http://127.0.0.1:9/", timeout_seconds=2.0)

    assert result.status is HealthStatus.UNREACHABLE
