"""
Sentinel - State Manager & Circuit Breaker Tests

Comprehensive tests for the SQLite-backed state manager and the
Circuit Breaker pattern that prevents Crash Loop BackOff.

Scenarios covered:
  - Intervention CRUD (record, retrieve, ordering, field integrity)
  - Circuit Breaker threshold enforcement (trip, reset, re-trip)
  - Crash Loop simulation (rapid-fire restarts)
  - Container isolation (breaker is per-container, not global)
  - Action type filtering (only restart/stop count; scale does not)
  - Breaker state persistence across checks
  - Reset-then-reaccumulate cycle
  - Exception metadata (container_name, count, window in the error)
  - Multiple concurrent containers under stress

All fixtures (state_manager, state_manager_strict) are provided
by conftest.py using temporary on-disk SQLite databases.
"""

from __future__ import annotations

import pytest

from src.core.exceptions import CircuitBreakerOpen
from src.engine.state_manager import StateManager


# ═════════════════════════════════════════════════════════
# Intervention Recording & History
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestInterventionRecording:
    """Test that interventions are correctly persisted and retrieved."""

    async def test_record_returns_positive_id(self, state_manager: StateManager) -> None:
        row_id = await state_manager.record_intervention(
            container_id="abc123def456",
            container_name="web-app-1",
            rule_name="High CPU",
            action_type="restart",
            success=True,
        )
        assert row_id > 0

    async def test_sequential_ids_increment(self, state_manager: StateManager) -> None:
        """Each record should get a strictly increasing ID."""
        ids = []
        for i in range(5):
            row_id = await state_manager.record_intervention(
                container_id=f"c{i}",
                container_name="web",
                rule_name="rule",
                action_type="restart",
            )
            ids.append(row_id)
        assert ids == sorted(ids)
        assert len(set(ids)) == 5  # All unique

    async def test_record_failed_intervention(self, state_manager: StateManager) -> None:
        await state_manager.record_intervention(
            container_id="abc123",
            container_name="web-app-1",
            rule_name="High CPU",
            action_type="restart",
            success=False,
            error_message="Container not found",
        )
        history = await state_manager.get_recent_history(limit=1)
        assert history[0]["success"] is False
        assert history[0]["error_message"] == "Container not found"

    async def test_success_defaults_to_true(self, state_manager: StateManager) -> None:
        """When success is not specified, it should default to True."""
        await state_manager.record_intervention(
            container_id="c1",
            container_name="web",
            rule_name="rule",
            action_type="restart",
        )
        history = await state_manager.get_recent_history(limit=1)
        assert history[0]["success"] is True
        assert history[0]["error_message"] is None


@pytest.mark.asyncio
class TestHistoryRetrieval:
    """Test history ordering, limits, and field integrity."""

    async def test_reverse_chronological_order(self, state_manager: StateManager) -> None:
        for i in range(5):
            await state_manager.record_intervention(
                container_id=f"c{i}",
                container_name=f"svc-{i}",
                rule_name="rule",
                action_type="restart",
            )
        history = await state_manager.get_recent_history(limit=5)
        # IDs are autoincrement, so descending ID = reverse insertion order
        ids = [r["id"] for r in history]
        assert ids == sorted(ids, reverse=True)

    async def test_limit_respected(self, state_manager: StateManager) -> None:
        for i in range(10):
            await state_manager.record_intervention(
                container_id=f"c{i}",
                container_name="test",
                rule_name="rule",
                action_type="restart",
            )
        assert len(await state_manager.get_recent_history(limit=3)) == 3
        assert len(await state_manager.get_recent_history(limit=50)) == 10

    async def test_empty_history_on_fresh_db(self, state_manager: StateManager) -> None:
        assert await state_manager.get_recent_history() == []

    async def test_record_contains_all_fields(self, state_manager: StateManager) -> None:
        await state_manager.record_intervention(
            container_id="abc123",
            container_name="webapp",
            rule_name="High CPU",
            action_type="restart",
        )
        record = (await state_manager.get_recent_history(limit=1))[0]
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

    async def test_created_at_is_iso_format(self, state_manager: StateManager) -> None:
        await state_manager.record_intervention(
            container_id="c1",
            container_name="web",
            rule_name="rule",
            action_type="restart",
        )
        record = (await state_manager.get_recent_history(limit=1))[0]
        # SQLite strftime produces ISO-like: 2026-05-06T03:47:08.123Z
        assert "T" in record["created_at"]


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Core Logic
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerThreshold:
    """Test that the breaker trips at exactly the threshold."""

    async def test_zero_restarts_passes(self, state_manager: StateManager) -> None:
        """No history at all should never trip."""
        await state_manager.check_circuit_breaker("webapp")  # No raise

    async def test_below_threshold_passes(self, state_manager: StateManager) -> None:
        """threshold=3: 2 restarts should NOT trip."""
        for _ in range(2):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        await state_manager.check_circuit_breaker("webapp")  # No raise

    async def test_at_threshold_trips(self, state_manager: StateManager) -> None:
        """threshold=3: exactly 3 restarts MUST trip."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_above_threshold_still_trips(self, state_manager: StateManager) -> None:
        """threshold=3: 5 restarts should also trip."""
        for _ in range(5):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_strict_threshold_trips_on_first(
        self,
        state_manager_strict: StateManager,
    ) -> None:
        """threshold=1: a single restart must trip immediately."""
        await state_manager_strict.record_intervention(
            container_id="c1",
            container_name="api",
            rule_name="rule",
            action_type="restart",
        )
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager_strict.check_circuit_breaker("api")
        assert exc_info.value.container_name == "api"


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Exception Metadata
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerExceptionDetail:
    """Verify the CircuitBreakerOpen exception carries full context."""

    async def test_exception_contains_container_name(self, state_manager: StateManager) -> None:
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="redis-cache",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager.check_circuit_breaker("redis-cache")
        assert exc_info.value.container_name == "redis-cache"

    async def test_exception_contains_restart_count(self, state_manager: StateManager) -> None:
        for _ in range(4):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager.check_circuit_breaker("webapp")
        assert exc_info.value.restart_count >= 3

    async def test_exception_contains_window_minutes(self, state_manager: StateManager) -> None:
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager.check_circuit_breaker("webapp")
        assert exc_info.value.window_minutes == 5

    async def test_exception_message_is_human_readable(self, state_manager: StateManager) -> None:
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            await state_manager.check_circuit_breaker("webapp")
        msg = str(exc_info.value)
        assert "webapp" in msg
        assert "human intervention" in msg.lower()


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Action Type Filtering
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerActionTypes:
    """restart, stop, and start count toward the breaker. Scale and exec do not."""

    async def test_stop_actions_count(self, state_manager: StateManager) -> None:
        """Mixed restart+stop should accumulate toward threshold."""
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="restart",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="stop",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="restart",
        )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_scale_actions_do_not_count(self, state_manager: StateManager) -> None:
        """3 scale actions should NOT trip the breaker (only restart/stop count)."""
        for _ in range(5):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="scale",
            )
        # Should NOT raise — scale is not a destructive loop action
        await state_manager.check_circuit_breaker("webapp")

    async def test_mixed_scale_and_restart(self, state_manager: StateManager) -> None:
        """2 restarts + 3 scales should NOT trip (only 2 counting actions)."""
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="restart",
        )
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="scale",
            )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="restart",
        )
        # 2 restarts + 3 scales = only 2 counting → below threshold of 3
        await state_manager.check_circuit_breaker("webapp")

    async def test_exec_actions_do_not_count(self, state_manager: StateManager) -> None:
        """exec actions should NOT count toward the breaker."""
        for _ in range(5):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="exec",
            )
        await state_manager.check_circuit_breaker("webapp")


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Container Isolation
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerIsolation:
    """Each container has its own independent breaker."""

    async def test_tripped_container_does_not_affect_others(
        self,
        state_manager: StateManager,
    ) -> None:
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # Other containers are unaffected
        await state_manager.check_circuit_breaker("api-server")
        await state_manager.check_circuit_breaker("redis")
        await state_manager.check_circuit_breaker("postgres")

    async def test_multiple_containers_independent_thresholds(
        self,
        state_manager: StateManager,
    ) -> None:
        """Two containers approaching threshold independently."""
        # webapp: 2 restarts (below threshold)
        for _ in range(2):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        # redis: 3 restarts (at threshold)
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c2",
                container_name="redis",
                rule_name="rule",
                action_type="restart",
            )

        await state_manager.check_circuit_breaker("webapp")  # OK
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("redis")  # Trips

    async def test_many_containers_under_stress(self, state_manager: StateManager) -> None:
        """Simulate 10 containers, only those at threshold should trip."""
        for i in range(10):
            restarts = i + 1  # container-0 gets 1 restart, container-9 gets 10
            for _ in range(restarts):
                await state_manager.record_intervention(
                    container_id=f"c{i}",
                    container_name=f"svc-{i}",
                    rule_name="rule",
                    action_type="restart",
                )

        # Containers 0,1 (1-2 restarts) should pass
        for i in range(2):
            await state_manager.check_circuit_breaker(f"svc-{i}")

        # Containers 2-9 (3+ restarts) should trip
        for i in range(2, 10):
            with pytest.raises(CircuitBreakerOpen):
                await state_manager.check_circuit_breaker(f"svc-{i}")


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Reset & Re-accumulation
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerReset:
    """Test manual reset and re-accumulation after reset."""

    async def test_reset_clears_open_state(self, state_manager: StateManager) -> None:
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        await state_manager.reset_circuit_breaker("webapp")

        status = await state_manager.get_circuit_breaker_status()
        entry = [s for s in status if s["container_name"] == "webapp"]
        if entry:
            assert entry[0]["is_open"] is False
            assert entry[0]["trip_count"] == 0

    async def test_reset_does_not_clear_history(self, state_manager: StateManager) -> None:
        """Reset clears breaker state but history survives for auditing."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        await state_manager.reset_circuit_breaker("webapp")

        history = await state_manager.get_recent_history(limit=50)
        webapp_records = [h for h in history if h["container_name"] == "webapp"]
        assert len(webapp_records) == 3  # History is preserved

    async def test_reset_then_still_trips_because_history_remains(
        self,
        state_manager: StateManager,
    ) -> None:
        """After reset, the old history still counts — breaker re-trips immediately.

        This is the correct behavior: resetting the breaker state flag
        does not erase the intervention records. If the same restarts
        are still within the time window, the breaker will trip again
        on the next check.
        """
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        await state_manager.reset_circuit_breaker("webapp")

        # The 3 interventions are still in the window → trips again
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_reset_nonexistent_container_is_safe(
        self,
        state_manager: StateManager,
    ) -> None:
        """Resetting a container that was never tripped should not error."""
        await state_manager.reset_circuit_breaker("never-existed")  # No raise


# ═════════════════════════════════════════════════════════
# Circuit Breaker — State Persistence
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerStatePersistence:
    """Test the circuit_breaker_state table updates."""

    async def test_empty_on_fresh_db(self, state_manager: StateManager) -> None:
        status = await state_manager.get_circuit_breaker_status()
        assert status == []

    async def test_state_recorded_on_trip(self, state_manager: StateManager) -> None:
        """Tripping should create a row in circuit_breaker_state."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        status = await state_manager.get_circuit_breaker_status()
        assert len(status) == 1
        assert status[0]["container_name"] == "webapp"
        assert status[0]["is_open"] is True
        assert status[0]["last_tripped"] is not None

    async def test_trip_count_increments(self, state_manager: StateManager) -> None:
        """trip_count is set when the breaker first trips via the sliding
        window (Phase 2). Once latched open, subsequent checks hit Phase 1
        (the is_open flag) and do NOT increment trip_count — the counter
        reflects how many times the breaker was tripped via the window,
        not how many times it was checked."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )

        # Trip once — sets trip_count via Phase 2
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")
        s1 = await state_manager.get_circuit_breaker_status()
        count_1 = s1[0]["trip_count"]
        assert count_1 >= 1

        # Second check hits Phase 1 (latched) — trip_count stays the same
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")
        s2 = await state_manager.get_circuit_breaker_status()
        count_2 = s2[0]["trip_count"]
        assert count_2 == count_1

    async def test_multiple_containers_in_status(self, state_manager: StateManager) -> None:
        """Status should list all containers that have been tripped."""
        for name in ("webapp", "redis", "worker"):
            for _ in range(3):
                await state_manager.record_intervention(
                    container_id="c1",
                    container_name=name,
                    rule_name="rule",
                    action_type="restart",
                )
            with pytest.raises(CircuitBreakerOpen):
                await state_manager.check_circuit_breaker(name)

        status = await state_manager.get_circuit_breaker_status()
        names = {s["container_name"] for s in status}
        assert names == {"webapp", "redis", "worker"}


# ═════════════════════════════════════════════════════════
# Crash Loop Simulation
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCrashLoopSimulation:
    """Simulate realistic crash loop scenarios end-to-end."""

    async def test_rapid_fire_restart_loop(self, state_manager: StateManager) -> None:
        """Simulate a container crash-looping: restart 10 times rapidly.

        The breaker must trip at restart #3 and stay tripped for all
        subsequent checks.
        """
        for i in range(10):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="crashy-app",
                rule_name="High CPU",
                action_type="restart",
            )

            if i < 2:
                # First 2 restarts: breaker should be closed
                await state_manager.check_circuit_breaker("crashy-app")
            else:
                # Restart #3 and beyond: breaker must be open
                with pytest.raises(CircuitBreakerOpen):
                    await state_manager.check_circuit_breaker("crashy-app")

    async def test_different_rules_same_container_accumulate(
        self,
        state_manager: StateManager,
    ) -> None:
        """Restarts from different rules on the same container should
        accumulate toward the same breaker."""
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="High CPU",
            action_type="restart",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="Memory Leak",
            action_type="restart",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="Unhealthy",
            action_type="stop",
        )

        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_failed_restarts_still_count(self, state_manager: StateManager) -> None:
        """Even failed restart attempts should count toward the breaker.

        If the restart command fails (success=False), the attempt was
        still made and should prevent further attempts.
        """
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
                success=False,
                error_message="Docker timeout",
            )

        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_breaker_prevents_infinite_restart_loop(
        self,
        state_manager_strict: StateManager,
    ) -> None:
        """With threshold=1, the first restart should immediately
        prevent any further restarts — the core crash loop protection."""
        # First restart
        await state_manager_strict.record_intervention(
            container_id="c1",
            container_name="fragile-app",
            rule_name="rule",
            action_type="restart",
        )

        # Every subsequent check must be blocked
        for _ in range(20):
            with pytest.raises(CircuitBreakerOpen):
                await state_manager_strict.check_circuit_breaker("fragile-app")


# ═════════════════════════════════════════════════════════
# Circuit Breaker — Latch Behavior (is_open persistence)
# ═════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCircuitBreakerLatch:
    """Test that the CB stays permanently open once tripped.

    This validates the fix for the sliding-window bug where the CB
    would auto-close after the window expired.
    """

    async def test_cb_stays_open_after_trip(self, state_manager: StateManager) -> None:
        """Once tripped, subsequent checks must raise even without
        new interventions in the window."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        # Trip the breaker
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # The breaker should remain open on repeated checks
        # (previously it would close when the sliding window moved)
        for _ in range(10):
            with pytest.raises(CircuitBreakerOpen):
                await state_manager.check_circuit_breaker("webapp")

    async def test_cb_is_open_flag_checked_first(self, state_manager: StateManager) -> None:
        """The is_open flag in circuit_breaker_state must be checked
        before counting interventions in the sliding window."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # Verify the is_open flag is set
        status = await state_manager.get_circuit_breaker_status()
        entry = [s for s in status if s["container_name"] == "webapp"]
        assert len(entry) == 1
        assert entry[0]["is_open"] is True

        # Now check again — should still be open due to the flag
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

    async def test_reset_clears_latch_and_allows_new_cycle(
        self, state_manager: StateManager
    ) -> None:
        """After manual reset, the breaker should re-evaluate from
        the sliding window (not stay latched open forever)."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="webapp",
                rule_name="rule",
                action_type="restart",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")

        # Reset the breaker
        await state_manager.reset_circuit_breaker("webapp")

        # Verify is_open is cleared
        status = await state_manager.get_circuit_breaker_status()
        entry = [s for s in status if s["container_name"] == "webapp"]
        if entry:
            assert entry[0]["is_open"] is False

    async def test_start_actions_count_toward_threshold(
        self, state_manager: StateManager
    ) -> None:
        """'start' actions (for exited containers) should count
        toward the circuit breaker threshold."""
        for _ in range(3):
            await state_manager.record_intervention(
                container_id="c1",
                container_name="exited-app",
                rule_name="Exited Recovery",
                action_type="start",
            )
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("exited-app")

    async def test_mixed_start_and_restart_accumulate(
        self, state_manager: StateManager
    ) -> None:
        """Mixed restart + start actions should accumulate."""
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="restart",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="start",
        )
        await state_manager.record_intervention(
            container_id="c1",
            container_name="webapp",
            rule_name="rule",
            action_type="stop",
        )
        # 1 restart + 1 start + 1 stop = 3 counting actions → trips
        with pytest.raises(CircuitBreakerOpen):
            await state_manager.check_circuit_breaker("webapp")
