"""Core health-check logic: send one HTTP request and classify the outcome."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import httpx


class HealthStatus(StrEnum):
    HEALTHY = "healthy"  # responded with the expected status code
    UNHEALTHY = "unhealthy"  # responded, but with an unexpected status code
    UNREACHABLE = "unreachable"  # no HTTP response at all (DNS, refused, timeout, ...)


@dataclass(frozen=True)
class CheckResult:
    status: HealthStatus
    status_code: int | None
    response_time_ms: float | None
    error: str | None
    checked_at: datetime


async def check_endpoint(
    client: httpx.AsyncClient,
    url: str,
    method: str = "GET",
    timeout_seconds: float = 5.0,
    expected_status: int = 200,
) -> CheckResult:
    """Request `url` once and report whether it is healthy.

    The response body is streamed but never read, so response time measures
    time-to-headers and large pages don't waste bandwidth or memory.
    """
    checked_at = datetime.now(UTC)
    start = time.perf_counter()
    try:
        async with client.stream(
            method, url, timeout=timeout_seconds, follow_redirects=True
        ) as response:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = response.status_code
    except httpx.TimeoutException:
        return _unreachable(f"Timed out after {timeout_seconds}s", checked_at)
    except httpx.ConnectError as exc:
        return _unreachable(f"Connection failed: {exc}", checked_at)
    except httpx.HTTPError as exc:
        return _unreachable(f"{type(exc).__name__}: {exc}", checked_at)

    if status_code == expected_status:
        return CheckResult(HealthStatus.HEALTHY, status_code, elapsed_ms, None, checked_at)
    return CheckResult(
        HealthStatus.UNHEALTHY,
        status_code,
        elapsed_ms,
        f"Expected status {expected_status}, got {status_code}",
        checked_at,
    )


def _unreachable(error: str, checked_at: datetime) -> CheckResult:
    return CheckResult(HealthStatus.UNREACHABLE, None, None, error, checked_at)
