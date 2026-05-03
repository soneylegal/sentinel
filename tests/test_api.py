"""
Sentinel - API Routes Tests

Tests the observability endpoints: /health, /history, /circuit-breakers.
Uses FastAPI's TestClient for synchronous testing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.routes import app_state
from src.api.server import create_app


@pytest.fixture
def client() -> TestClient:
    """Create a test client with mocked dependencies."""
    app = create_app()

    # Mock the collector
    mock_collector = MagicMock()
    mock_collector.is_connected = True
    app_state.collector = mock_collector

    # Mock the state manager
    mock_state = AsyncMock()
    mock_state.get_recent_history = AsyncMock(return_value=[
        {
            "id": 1,
            "container_id": "abc123",
            "container_name": "webapp",
            "rule_name": "High CPU",
            "action_type": "restart",
            "success": True,
            "error_message": None,
            "created_at": "2026-05-03T12:00:00Z",
        },
    ])
    mock_state.get_circuit_breaker_status = AsyncMock(return_value=[])
    mock_state.reset_circuit_breaker = AsyncMock()
    app_state.state_manager = mock_state
    app_state.start_time = datetime.now(timezone.utc)

    return TestClient(app)


class TestHealthEndpoint:
    """Test GET /health."""

    def test_health_ok(self, client: TestClient) -> None:
        """Health check should return 200 with connected status."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["docker_connected"] is True
        assert "uptime_seconds" in data

    def test_health_degraded(self, client: TestClient) -> None:
        """Health should report degraded when Docker is disconnected."""
        app_state.collector.is_connected = False
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["docker_connected"] is False


class TestHistoryEndpoint:
    """Test GET /history."""

    def test_history_returns_records(self, client: TestClient) -> None:
        """History should return intervention records."""
        resp = client.get("/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["records"][0]["container_name"] == "webapp"
        assert data["records"][0]["action_type"] == "restart"


class TestCircuitBreakerEndpoints:
    """Test circuit breaker endpoints."""

    def test_get_breakers_empty(self, client: TestClient) -> None:
        """Should return empty list when no breakers are tripped."""
        resp = client.get("/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["breakers"] == []

    def test_reset_breaker(self, client: TestClient) -> None:
        """Should return success when resetting a breaker."""
        resp = client.post("/circuit-breakers/webapp/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["container_name"] == "webapp"
