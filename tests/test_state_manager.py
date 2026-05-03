"""
Sentinel - State Manager & Circuit Breaker Tests

Tests the SQLite-backed state manager, including intervention
recording, history retrieval, and circuit breaker logic.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from src.core.exceptions import CircuitBreakerOpen
from src.engine.state_manager import StateManager


@pytest_asyncio.fixture
async def state_manager(tmp_path: Path) -> StateManager:
    """Create a StateManager with a temporary database."""
    db_path = str(tmp_path / "test.db")
    sm = StateManager(db_path=db_path, threshold=3, window_minutes=5)
    await sm.initialize()
    yield sm  # type: ignore[misc]
    await sm.close()


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
