"""Request/response models. Pydantic validates input and FastAPI turns failures into 422s."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.monitor import HealthStatus


class EndpointCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100, examples=["GitHub API"])
    url: HttpUrl = Field(examples=["https://api.github.com"])  # only http:// and https://
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class EndpointOut(BaseModel):
    id: int
    name: str
    url: str
    method: str
    expected_status: int
    timeout_seconds: float
    created_at: str


class CheckResultOut(BaseModel):
    id: int
    endpoint_id: int
    status: HealthStatus
    status_code: int | None
    response_time_ms: float | None
    error: str | None
    checked_at: str


class EndpointStatus(BaseModel):
    endpoint: EndpointOut
    latest_check: CheckResultOut | None
    checks_counted: int
    uptime_percent: float | None
    avg_response_time_ms: float | None
