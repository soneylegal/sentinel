"""
Sentinel - API Routes (Observability Endpoints)

Exposes the daemon's internal state for monitoring and debugging.
These endpoints are designed to be scraped by Prometheus, polled
by health-check systems, or inspected manually by operators.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.logger import get_logger

logger = get_logger()

router = APIRouter()


# ─────────────────────────────────────────────────────────
# Response Models
# ─────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: str
    docker_connected: bool
    uptime_seconds: float
    version: str
    timestamp: str


class InterventionRecord(BaseModel):
    id: int
    container_id: str
    container_name: str
    rule_name: str
    action_type: str
    success: bool
    error_message: str | None
    created_at: str


class HistoryResponse(BaseModel):
    count: int
    records: list[InterventionRecord]


class CircuitBreakerRecord(BaseModel):
    container_name: str
    trip_count: int
    last_tripped: str | None
    is_open: bool


class CircuitBreakerResponse(BaseModel):
    breakers: list[CircuitBreakerRecord]


class ResetResponse(BaseModel):
    status: str
    container_name: str
    message: str


# ─────────────────────────────────────────────────────────
# Shared State (injected at startup by the server module)
# ─────────────────────────────────────────────────────────
class _AppState:
    """Mutable singleton holding references to daemon components."""

    def __init__(self) -> None:
        self.collector: Any = None
        self.state_manager: Any = None
        self.start_time: datetime = datetime.now(UTC)
        self.version: str = "1.0.0"


app_state = _AppState()


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────
@router.get("/health", response_model=HealthResponse, tags=["Observability"])
async def health_check() -> HealthResponse:
    """Check if the Sentinel daemon is alive and connected to Docker.

    Returns 200 OK with connection status and uptime information.
    """
    docker_connected = False
    if app_state.collector:
        docker_connected = app_state.collector.is_connected

    uptime = (datetime.now(UTC) - app_state.start_time).total_seconds()

    return HealthResponse(
        status="ok" if docker_connected else "degraded",
        docker_connected=docker_connected,
        uptime_seconds=round(uptime, 2),
        version=app_state.version,
        timestamp=datetime.now(UTC).isoformat(),
    )


@router.get("/history", response_model=HistoryResponse, tags=["Observability"])
async def get_intervention_history() -> HistoryResponse:
    """Return the last 50 autonomous interventions taken by the daemon.

    Includes container name, action type, success status, and timestamps.
    """
    if not app_state.state_manager:
        raise HTTPException(status_code=503, detail="State manager not initialized")

    records = await app_state.state_manager.get_recent_history(limit=50)

    return HistoryResponse(
        count=len(records),
        records=[InterventionRecord(**r) for r in records],
    )


@router.get("/circuit-breakers", response_model=CircuitBreakerResponse, tags=["Observability"])
async def get_circuit_breaker_status() -> CircuitBreakerResponse:
    """Return the current state of all circuit breakers.

    Shows which containers have been tripped and are currently
    protected from further autonomous action.
    """
    if not app_state.state_manager:
        raise HTTPException(status_code=503, detail="State manager not initialized")

    breakers = await app_state.state_manager.get_circuit_breaker_status()

    return CircuitBreakerResponse(
        breakers=[CircuitBreakerRecord(**b) for b in breakers],
    )


@router.post(
    "/circuit-breakers/{container_name}/reset",
    response_model=ResetResponse,
    tags=["Operations"],
)
async def reset_circuit_breaker(container_name: str) -> ResetResponse:
    """Manually reset the circuit breaker for a specific container.

    This re-enables autonomous actions for a container that was
    previously tripped. Use with caution.
    """
    if not app_state.state_manager:
        raise HTTPException(status_code=503, detail="State manager not initialized")

    await app_state.state_manager.reset_circuit_breaker(container_name)

    logger.info(
        f"Circuit breaker manually reset for '{container_name}' via API",
        component="api.routes",
    )

    return ResetResponse(
        status="ok",
        container_name=container_name,
        message=f"Circuit breaker for '{container_name}' has been reset. "
        f"Autonomous actions are now re-enabled.",
    )
