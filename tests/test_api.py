"""
Sentinel - API Integration Tests

Tests the observability endpoints using FastAPI TestClient:
  GET  /health              → daemon health + Docker connection status
  GET  /history             → intervention history with pagination
  GET  /circuit-breakers    → current breaker states
  POST /circuit-breakers/{name}/reset → manual breaker reset
  GET  /docs                → Swagger UI
  GET  /redoc               → ReDoc
  GET  /openapi.json        → OpenAPI 3.x schema

All tests hit the actual FastAPI router with mocked backend
dependencies (collector, state_manager) injected via the
``api_client`` fixture from conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.api.routes import app_state
from src.api.server import create_app


# ═════════════════════════════════════════════════════════
# GET /health
# ═════════════════════════════════════════════════════════
class TestHealthEndpoint:
    """Test the daemon health-check endpoint."""

    def test_status_code_200(self, api_client: TestClient) -> None:
        assert api_client.get("/health").status_code == 200

    def test_response_schema(self, api_client: TestClient) -> None:
        """Response must match the HealthResponse Pydantic model."""
        data = api_client.get("/health").json()
        expected = {"status", "docker_connected", "uptime_seconds", "version", "timestamp"}
        assert set(data.keys()) == expected

    def test_status_ok_when_connected(self, api_client: TestClient) -> None:
        data = api_client.get("/health").json()
        assert data["status"] == "ok"
        assert data["docker_connected"] is True

    def test_status_degraded_when_disconnected(self, api_client: TestClient) -> None:
        app_state.collector.is_connected = False
        data = api_client.get("/health").json()
        assert data["status"] == "degraded"
        assert data["docker_connected"] is False

    def test_status_degraded_when_collector_missing(self) -> None:
        """If the collector was never injected, health should still respond."""
        app = create_app()
        original = app_state.collector
        app_state.collector = None
        try:
            client = TestClient(app)
            data = client.get("/health").json()
            assert data["status"] == "degraded"
            assert data["docker_connected"] is False
        finally:
            app_state.collector = original

    def test_version_is_semver(self, api_client: TestClient) -> None:
        version = api_client.get("/health").json()["version"]
        parts = version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_version_matches_1_0_0(self, api_client: TestClient) -> None:
        assert api_client.get("/health").json()["version"] == "1.0.0"

    def test_uptime_is_non_negative(self, api_client: TestClient) -> None:
        uptime = api_client.get("/health").json()["uptime_seconds"]
        assert isinstance(uptime, float)
        assert uptime >= 0.0

    def test_timestamp_is_iso_format(self, api_client: TestClient) -> None:
        ts = api_client.get("/health").json()["timestamp"]
        # Should parse without error
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.year >= 2026

    def test_content_type_is_json(self, api_client: TestClient) -> None:
        resp = api_client.get("/health")
        assert "application/json" in resp.headers["content-type"]


# ═════════════════════════════════════════════════════════
# GET /history
# ═════════════════════════════════════════════════════════
class TestHistoryEndpoint:
    """Test the intervention history endpoint."""

    def test_status_code_200(self, api_client: TestClient) -> None:
        assert api_client.get("/history").status_code == 200

    def test_response_schema(self, api_client: TestClient) -> None:
        data = api_client.get("/history").json()
        assert "count" in data
        assert "records" in data
        assert isinstance(data["count"], int)
        assert isinstance(data["records"], list)

    def test_count_matches_records_length(self, api_client: TestClient) -> None:
        data = api_client.get("/history").json()
        assert data["count"] == len(data["records"])

    def test_returns_conftest_sample_data(self, api_client: TestClient) -> None:
        """conftest.py injects 2 sample records."""
        data = api_client.get("/history").json()
        assert data["count"] == 2
        assert data["records"][0]["container_name"] == "webapp"
        assert data["records"][0]["rule_name"] == "High CPU Auto-Restart"
        assert data["records"][0]["action_type"] == "restart"

    def test_success_and_failure_records(self, api_client: TestClient) -> None:
        records = api_client.get("/history").json()["records"]
        success_rec = records[0]
        failure_rec = records[1]
        assert success_rec["success"] is True
        assert success_rec["error_message"] is None
        assert failure_rec["success"] is False
        assert failure_rec["error_message"] == "Container not found"

    def test_record_field_completeness(self, api_client: TestClient) -> None:
        """Each record must have all InterventionRecord fields."""
        record = api_client.get("/history").json()["records"][0]
        expected = {
            "id",
            "container_id",
            "container_name",
            "rule_name",
            "action_type",
            "success",
            "error_message",
            "created_at",
        }
        assert set(record.keys()) == expected

    def test_record_id_is_integer(self, api_client: TestClient) -> None:
        record = api_client.get("/history").json()["records"][0]
        assert isinstance(record["id"], int)

    def test_empty_history(self) -> None:
        """When state_manager returns no records, count should be 0."""
        app = create_app()
        mock_state = AsyncMock()
        mock_state.get_recent_history = AsyncMock(return_value=[])
        mock_collector = MagicMock()
        mock_collector.is_connected = True
        app_state.state_manager = mock_state
        app_state.collector = mock_collector
        app_state.start_time = datetime.now(UTC)

        client = TestClient(app)
        data = client.get("/history").json()
        assert data["count"] == 0
        assert data["records"] == []

    def test_503_when_state_manager_missing(self) -> None:
        """If state_manager was never injected, return 503."""
        app = create_app()
        original_sm = app_state.state_manager
        original_c = app_state.collector
        app_state.state_manager = None
        app_state.collector = MagicMock()
        try:
            client = TestClient(app)
            resp = client.get("/history")
            assert resp.status_code == 503
        finally:
            app_state.state_manager = original_sm
            app_state.collector = original_c


# ═════════════════════════════════════════════════════════
# GET /circuit-breakers
# ═════════════════════════════════════════════════════════
class TestCircuitBreakerGetEndpoint:
    """Test GET /circuit-breakers."""

    def test_status_code_200(self, api_client: TestClient) -> None:
        assert api_client.get("/circuit-breakers").status_code == 200

    def test_empty_breakers_list(self, api_client: TestClient) -> None:
        """conftest returns empty breaker list by default."""
        data = api_client.get("/circuit-breakers").json()
        assert data["breakers"] == []

    def test_response_with_tripped_breakers(self) -> None:
        """When breakers are tripped, the response should list them."""
        app = create_app()
        mock_state = AsyncMock()
        mock_state.get_circuit_breaker_status = AsyncMock(
            return_value=[
                {
                    "container_name": "webapp",
                    "trip_count": 3,
                    "last_tripped": "2026-05-07T00:00:00Z",
                    "is_open": True,
                },
                {
                    "container_name": "redis",
                    "trip_count": 1,
                    "last_tripped": "2026-05-07T00:05:00Z",
                    "is_open": False,
                },
            ]
        )
        app_state.state_manager = mock_state
        app_state.collector = MagicMock()
        app_state.start_time = datetime.now(UTC)

        client = TestClient(app)
        data = client.get("/circuit-breakers").json()
        assert len(data["breakers"]) == 2

        webapp = data["breakers"][0]
        assert webapp["container_name"] == "webapp"
        assert webapp["trip_count"] == 3
        assert webapp["is_open"] is True
        assert webapp["last_tripped"] is not None

        redis = data["breakers"][1]
        assert redis["container_name"] == "redis"
        assert redis["is_open"] is False

    def test_breaker_record_schema(self) -> None:
        """Each breaker record must have all CircuitBreakerRecord fields."""
        app = create_app()
        mock_state = AsyncMock()
        mock_state.get_circuit_breaker_status = AsyncMock(
            return_value=[
                {
                    "container_name": "webapp",
                    "trip_count": 1,
                    "last_tripped": "2026-05-07T00:00:00Z",
                    "is_open": True,
                },
            ]
        )
        app_state.state_manager = mock_state
        app_state.collector = MagicMock()
        app_state.start_time = datetime.now(UTC)

        client = TestClient(app)
        record = client.get("/circuit-breakers").json()["breakers"][0]
        expected = {"container_name", "trip_count", "last_tripped", "is_open"}
        assert set(record.keys()) == expected

    def test_503_when_state_manager_missing(self) -> None:
        app = create_app()
        original = app_state.state_manager
        app_state.state_manager = None
        app_state.collector = MagicMock()
        try:
            resp = TestClient(app).get("/circuit-breakers")
            assert resp.status_code == 503
        finally:
            app_state.state_manager = original


# ═════════════════════════════════════════════════════════
# POST /circuit-breakers/{name}/reset
# ═════════════════════════════════════════════════════════
class TestCircuitBreakerResetEndpoint:
    """Test POST /circuit-breakers/{name}/reset."""

    def test_status_code_200(self, api_client: TestClient) -> None:
        resp = api_client.post("/circuit-breakers/webapp/reset")
        assert resp.status_code == 200

    def test_response_schema(self, api_client: TestClient) -> None:
        data = api_client.post("/circuit-breakers/webapp/reset").json()
        expected = {"status", "container_name", "message"}
        assert set(data.keys()) == expected

    def test_response_echoes_container_name(self, api_client: TestClient) -> None:
        data = api_client.post("/circuit-breakers/my-service/reset").json()
        assert data["container_name"] == "my-service"

    def test_status_is_ok(self, api_client: TestClient) -> None:
        data = api_client.post("/circuit-breakers/webapp/reset").json()
        assert data["status"] == "ok"

    def test_message_mentions_re_enabled(self, api_client: TestClient) -> None:
        msg = api_client.post("/circuit-breakers/webapp/reset").json()["message"]
        assert "re-enabled" in msg

    def test_message_contains_container_name(self, api_client: TestClient) -> None:
        msg = api_client.post("/circuit-breakers/redis-cache/reset").json()["message"]
        assert "redis-cache" in msg

    def test_delegates_to_state_manager(self, api_client: TestClient) -> None:
        """The endpoint must call state_manager.reset_circuit_breaker."""
        api_client.post("/circuit-breakers/worker-3/reset")
        app_state.state_manager.reset_circuit_breaker.assert_called_with("worker-3")

    def test_multiple_resets_idempotent(self, api_client: TestClient) -> None:
        """Resetting the same container multiple times should always succeed."""
        for _ in range(5):
            resp = api_client.post("/circuit-breakers/webapp/reset")
            assert resp.status_code == 200

    def test_special_characters_in_name(self, api_client: TestClient) -> None:
        """Container names with dots/hyphens should be handled."""
        resp = api_client.post("/circuit-breakers/my.service-v2/reset")
        assert resp.status_code == 200
        assert resp.json()["container_name"] == "my.service-v2"

    def test_503_when_state_manager_missing(self) -> None:
        app = create_app()
        original = app_state.state_manager
        app_state.state_manager = None
        app_state.collector = MagicMock()
        try:
            resp = TestClient(app).post("/circuit-breakers/webapp/reset")
            assert resp.status_code == 503
        finally:
            app_state.state_manager = original


# ═════════════════════════════════════════════════════════
# API Documentation & OpenAPI Schema
# ═════════════════════════════════════════════════════════
class TestAPIDocumentation:
    """Test that documentation endpoints are accessible and correct."""

    def test_swagger_ui_200(self, api_client: TestClient) -> None:
        assert api_client.get("/docs").status_code == 200

    def test_redoc_200(self, api_client: TestClient) -> None:
        assert api_client.get("/redoc").status_code == 200

    def test_openapi_json_200(self, api_client: TestClient) -> None:
        assert api_client.get("/openapi.json").status_code == 200

    def test_openapi_title(self, api_client: TestClient) -> None:
        schema = api_client.get("/openapi.json").json()
        assert schema["info"]["title"] == "Sentinel - Observability API"

    def test_openapi_version(self, api_client: TestClient) -> None:
        schema = api_client.get("/openapi.json").json()
        assert schema["info"]["version"] == "1.0.0"

    def test_openapi_lists_all_paths(self, api_client: TestClient) -> None:
        """All 4 endpoints must appear in the OpenAPI schema."""
        paths = api_client.get("/openapi.json").json()["paths"]
        assert "/health" in paths
        assert "/history" in paths
        assert "/circuit-breakers" in paths
        assert "/circuit-breakers/{container_name}/reset" in paths

    def test_openapi_health_is_get(self, api_client: TestClient) -> None:
        paths = api_client.get("/openapi.json").json()["paths"]
        assert "get" in paths["/health"]

    def test_openapi_reset_is_post(self, api_client: TestClient) -> None:
        paths = api_client.get("/openapi.json").json()["paths"]
        assert "post" in paths["/circuit-breakers/{container_name}/reset"]


# ═════════════════════════════════════════════════════════
# CORS & Headers
# ═════════════════════════════════════════════════════════
class TestCORSAndHeaders:
    """Test that CORS middleware is configured correctly."""

    def test_cors_allows_any_origin(self, api_client: TestClient) -> None:
        resp = api_client.get("/health", headers={"Origin": "http://grafana.local:3000"})
        # CORS with allow_credentials=True reflects the requesting origin
        origin = resp.headers.get("access-control-allow-origin")
        assert origin in ("*", "http://grafana.local:3000")

    def test_response_is_json(self, api_client: TestClient) -> None:
        for path in ["/health", "/history", "/circuit-breakers"]:
            resp = api_client.get(path)
            assert "application/json" in resp.headers["content-type"]


# ═════════════════════════════════════════════════════════
# Invalid Routes
# ═════════════════════════════════════════════════════════
class TestInvalidRoutes:
    """Test that undefined routes return proper errors."""

    def test_404_on_unknown_path(self, api_client: TestClient) -> None:
        resp = api_client.get("/nonexistent")
        assert resp.status_code == 404

    def test_405_on_wrong_method(self, api_client: TestClient) -> None:
        """POST to a GET-only endpoint should return 405."""
        resp = api_client.post("/health")
        assert resp.status_code == 405

    def test_405_get_on_reset(self, api_client: TestClient) -> None:
        """GET on the reset endpoint (POST-only) should return 405."""
        resp = api_client.get("/circuit-breakers/webapp/reset")
        assert resp.status_code == 405
