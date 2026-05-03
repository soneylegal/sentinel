"""
Sentinel - State Manager & Circuit Breaker Tests

Tests the SQLite-backed state manager, including intervention
recording, history retrieval, and circuit breaker logic.

All fixtures (state_manager, state_manager_strict) are provided
by conftest.py using temporary on-disk SQLite databases.
"""

from __future__ import annotations

import pytest

from src.core.exceptions import CircuitBreakerOpen
from src.engine.state_manager import StateManager


@pytest.mark.asyncio
class TestStateManager:
    """Test intervention recording and retrieval."""

    async def test_record_intervention(self, state_manager: StateManager) -> None:
        """Should record an intervention and return a valid row ID."""
        row_id = await state_manager.record_intervention(
            container_id="abc123def456",
            container_name="web-app-1",
            rule_name="High CPU",
            action_type="restart",
            success=True,
        )
        assert row_id > 0

    async def test_record_failed_intervention(self, state_manager: StateManager) -> None:
        """Should record a failed intervention with error message."""
        row_id = await state_manager.record_intervention(
            container_id="abc123def456",
            container_name="web-app-1",
            rule_name="High CPU",
            action_type="restart",
            success=False,
            error_message="Container not found",
        )
        assert row_id > 0
        history = await state_manager.get_recent_history(limit=1)
        assert history[0]["success"] is False
        assert history[0]["error_message"] == "Container not found"

    async def test_get_recent_history(self, state_manager: StateManager) -> None:
        """Should return recorded interventions in reverse chronological order."""
        for i in range(5):
            await state_manager.record_intervention(
                container_id=f"container_{i}",
                container_name=f"service-{i}",
                rule_name="Test Rule",
                action_type="restart",
                success=True,
            )

        history = await state_manager.get_recent_history(limit=3)
        assert len(history) == 3
        assert history[0]["container_name"] == "service-4"  # Most recent

    async def test_history_limit(self, state_manager: StateManager) -> None:
        """History should respect the limit parameter."""
        for i in range(10):
            await state_manager.record_intervention(
                container_id=f"c_{i}",
                container_name="test",
                rule_name="rule",
                action_type="restart",
            )

        history = await state_manager.get_recent_history(limit=50)
        assert len(history) == 10

    async def test_history_empty_on_fresh_db(self, state_manager: StateManager) -> None:
        """Fresh database should return empty history."""
        history = await state_manager.get_recent_history()
        assert history == []

    async def test_history_contains_all_fields(self, state_manager: StateManager) -> None:
        """Each history record should contain all expected fields."""
        await state_manager.record_intervention(
            container_id="abc123",
            container_name="webapp",
            rule_name="High CPU",
            action_type="restart",
            success=True,
        )

        history = await state_manager.get_recent_history(limit=1)
        record = history[0]
        expected_keys = {
            "id", "container_id", "container_name",
            "rule_name", "action_type", "success",
            "error_message", "created_at",
        }
        assert set(record.keys()) == expected_keys


@pytest.mark.asyncio
class TestCircuitBreaker:
    """Test circuit breaker logic."""

    async def test_below_threshold_allows_action(self, state_manager: StateManager) -> None:
        """Under threshold should not trip the breaker."""
        # Record 2 interventions (threshold is 3)
        for _ in range(2):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )

        # Should NOT raise
        await state_manager.check_circuit_breaker("webapp")

    async def test_at_threshold_trips_breaker(self, state_manager: StateManager) -> None:
        """At or above threshold should trip the breaker."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )

        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager.check_circuit_breaker("webapp")

        assert exc_info.value.container_name == "webapp"
        assert exc_info.value.restart_count >= 3

    async def test_strict_threshold_trips_immediately(
        self, state_manager_strict: StateManager
    ) -> None:
        """Strict breaker (threshold=1) should trip after a single restart."""
        await state_manager_strict.record_intervention(
            container_id="c1",
            container_name="api",
            rule_name="rule",
            action_type="restart",
        )

        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager_strict.check_circuit_breaker("api")

        assert exc_info.value.container_name == "api"

    async def test_reset_circuit_breaker(self, state_manager: StateManager) -> None:
        """Resetting should re-enable the breaker check without clearing history."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )

        # Breaker is tripped
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # Reset only the breaker state — history remains
        await state_manager.reset_circuit_breaker("webapp")

        status = await state_manager.get_circuit_breaker_status()
        tripped = [s for s in status if s["container_name"] == "webapp"]
        if tripped:
            assert tripped[0]["is_open"] is False

    async def test_different_containers_independent(self, state_manager: StateManager) -> None:
        """Circuit breakers should be independent per container."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )

        # webapp is tripped
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # api-server is NOT tripped
        await state_manager.check_circuit_breaker("api-server")

    async def test_stop_actions_count_toward_breaker(self, state_manager: StateManager) -> None:
        """Stop actions should also count toward the circuit breaker threshold."""
        await state_manager.record_intervention(
            container_id="c1", container_name="webapp",
            rule_name="rule", action_type="restart",
        )
        await state_manager.record_intervention(
            container_id="c1", container_name="webapp",
            rule_name="rule", action_type="stop",
        )
        await state_manager.record_intervention(
            container_id="c1", container_name="webapp",
            rule_name="rule", action_type="restart",
        )

        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_circuit_breaker_status_empty_initially(
        self, state_manager: StateManager
    ) -> None:
        """Fresh database should have no circuit breaker state."""
        status = await state_manager.get_circuit_breaker_status()
        assert status == []
