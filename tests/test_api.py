"""
Sentinel - API Routes Tests

Tests the observability endpoints: /health, /history, /circuit-breakers.
Uses the ``api_client`` fixture from conftest.py which provides a
FastAPI TestClient with pre-injected mocked dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.routes import app_state


class TestHealthEndpoint:
    """Test GET /health."""

    def test_health_ok(self, api_client: TestClient) -> None:
        """Health check should return 200 with connected status."""
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["docker_connected"] is True
        assert "uptime_seconds" in data
        assert "version" in data
        assert "timestamp" in data

    def test_health_degraded(self, api_client: TestClient) -> None:
        """Health should report degraded when Docker is disconnected."""
        app_state.collector.is_connected = False
        resp = api_client.get("/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["docker_connected"] is False

    def test_health_version_present(self, api_client: TestClient) -> None:
        """Health should include the daemon version."""
        resp = api_client.get("/health")
        data = resp.json()
        assert data["version"] == "1.0.0"


class TestHistoryEndpoint:
    """Test GET /history."""

    def test_history_returns_records(self, api_client: TestClient) -> None:
        """History should return intervention records."""
        resp = api_client.get("/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2  # conftest provides 2 sample records
        assert data["records"][0]["container_name"] == "webapp"
        assert data["records"][0]["action_type"] == "restart"

    def test_history_includes_failures(self, api_client: TestClient) -> None:
        """History should include both successful and failed interventions."""
        resp = api_client.get("/history")
        data = resp.json()
        # Second record (from conftest) is a failure
        failed = data["records"][1]
        assert failed["success"] is False
        assert failed["error_message"] == "Container not found"

    def test_history_record_schema(self, api_client: TestClient) -> None:
        """Each history record should have all required fields."""
        resp = api_client.get("/history")
        record = resp.json()["records"][0]
        expected_fields = {
            "id", "container_id", "container_name",
            "rule_name", "action_type", "success",
            "error_message", "created_at",
        }
        assert set(record.keys()) == expected_fields


class TestCircuitBreakerEndpoints:
    """Test circuit breaker endpoints."""

    def test_get_breakers_empty(self, api_client: TestClient) -> None:
        """Should return empty list when no breakers are tripped."""
        resp = api_client.get("/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["breakers"] == []

    def test_reset_breaker(self, api_client: TestClient) -> None:
        """Should return success when resetting a breaker."""
        resp = api_client.post("/circuit-breakers/webapp/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["container_name"] == "webapp"
        assert "re-enabled" in data["message"]

    def test_reset_calls_state_manager(self, api_client: TestClient) -> None:
        """Reset endpoint should delegate to state_manager.reset_circuit_breaker."""
        api_client.post("/circuit-breakers/redis/reset")
        app_state.state_manager.reset_circuit_breaker.assert_called_with("redis")


class TestSwaggerDocs:
    """Test that API documentation endpoints are accessible."""

    def test_swagger_ui_accessible(self, api_client: TestClient) -> None:
        """Swagger UI should return 200."""
        resp = api_client.get("/docs")
        assert resp.status_code == 200

    def test_openapi_schema(self, api_client: TestClient) -> None:
        """OpenAPI JSON schema should be accessible."""
        resp = api_client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["info"]["title"] == "Sentinel - Observability API"
