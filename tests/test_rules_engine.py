"""
Sentinel - Rules Engine Tests

Tests the rules evaluation logic: container matching, condition
evaluation, sustained-duration tracking, and violation lifecycle.

Uses factory fixtures from conftest.py (make_metrics, make_rule,
make_engine) to avoid duplicating mock setup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import CircuitBreakerOpen


class TestContainerMatching:
    """Test regex-based container matching."""

    def test_matches_all(self, make_engine, make_rule) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([make_rule(pattern=".*")])
        assert engine._matches_container(engine._rules[0], "anything")

    def test_specific_pattern(self, make_engine, make_rule) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([make_rule(pattern=r"^web-\d+$")])
        assert engine._matches_container(engine._rules[0], "web-1")
        assert engine._matches_container(engine._rules[0], "web-999")
        assert not engine._matches_container(engine._rules[0], "api-server")
        assert not engine._matches_container(engine._rules[0], "web-")

    def test_exclude_pattern(self, make_engine, make_rule) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([make_rule(excludes=[r"^sentinel$"])])
        assert engine._matches_container(engine._rules[0], "webapp")
        assert not engine._matches_container(engine._rules[0], "sentinel")

    def test_multiple_exclude_patterns(self, make_engine, make_rule) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([
            make_rule(excludes=[r"^sentinel$", r"^traefik.*", r"^monitoring-"])
        ])
        assert engine._matches_container(engine._rules[0], "webapp")
        assert not engine._matches_container(engine._rules[0], "sentinel")
        assert not engine._matches_container(engine._rules[0], "traefik-proxy")
        assert not engine._matches_container(engine._rules[0], "monitoring-grafana")

    def test_disabled_rule_filtered(self, make_engine, make_rule) -> None:  # type: ignore[no-untyped-def]
        """Disabled rules should be filtered out during engine initialization."""
        engine, _, _ = make_engine([
            make_rule(name="Active", enabled=True),
            make_rule(name="Disabled", enabled=False),
        ])
        assert len(engine._rules) == 1
        assert engine._rules[0].name == "Active"


class TestConditionEvaluation:
    """Test metric condition comparison logic."""

    def test_cpu_above_threshold(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([make_rule(metric="cpu_percent", operator=">", threshold=80.0)])
        metrics = make_metrics(cpu_percent=95.0)
        assert engine._condition_met(engine._rules[0], metrics)

    def test_cpu_below_threshold(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([make_rule(metric="cpu_percent", operator=">", threshold=80.0)])
        metrics = make_metrics(cpu_percent=50.0)
        assert not engine._condition_met(engine._rules[0], metrics)

    def test_cpu_at_exact_threshold(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Greater-than should NOT trigger at exactly the threshold."""
        engine, _, _ = make_engine([make_rule(metric="cpu_percent", operator=">", threshold=80.0)])
        metrics = make_metrics(cpu_percent=80.0)
        assert not engine._condition_met(engine._rules[0], metrics)

    def test_gte_at_threshold(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Greater-than-or-equal SHOULD trigger at threshold."""
        engine, _, _ = make_engine([make_rule(metric="cpu_percent", operator=">=", threshold=80.0)])
        metrics = make_metrics(cpu_percent=80.0)
        assert engine._condition_met(engine._rules[0], metrics)

    def test_health_status_comparison(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([
            make_rule(metric="health_status", operator="==", threshold="unhealthy")
        ])
        metrics = make_metrics(health_status="unhealthy")
        assert engine._condition_met(engine._rules[0], metrics)

    def test_health_status_healthy_no_match(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Healthy container should NOT match unhealthy condition."""
        engine, _, _ = make_engine([
            make_rule(metric="health_status", operator="==", threshold="unhealthy")
        ])
        metrics = make_metrics(health_status="healthy")
        assert not engine._condition_met(engine._rules[0], metrics)

    def test_less_than_operator(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([
            make_rule(metric="memory_percent", operator="<", threshold=20.0)
        ])
        metrics = make_metrics(memory_percent=10.0)
        assert engine._condition_met(engine._rules[0], metrics)

    def test_equal_operator_numeric(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([
            make_rule(metric="cpu_percent", operator="==", threshold=50.0)
        ])
        metrics = make_metrics(cpu_percent=50.0)
        assert engine._condition_met(engine._rules[0], metrics)

    def test_lte_operator(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        engine, _, _ = make_engine([
            make_rule(metric="memory_percent", operator="<=", threshold=50.0)
        ])
        assert engine._condition_met(engine._rules[0], make_metrics(memory_percent=50.0))
        assert engine._condition_met(engine._rules[0], make_metrics(memory_percent=30.0))
        assert not engine._condition_met(engine._rules[0], make_metrics(memory_percent=51.0))


@pytest.mark.asyncio
class TestRuleEvaluation:
    """Test full rule evaluation with action triggering."""

    async def test_immediate_trigger(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Rule with sustained=0 should trigger immediately."""
        engine, action_mock, state_manager = make_engine([
            make_rule(threshold=80.0, sustained=0)
        ])

        metrics = [make_metrics(cpu_percent=95.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_called_once()
        state_manager.record_intervention.assert_called_once()

    async def test_no_trigger_below_threshold(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Metrics below threshold should not trigger any action."""
        engine, action_mock, _ = make_engine([
            make_rule(threshold=80.0, sustained=0)
        ])

        metrics = [make_metrics(cpu_percent=50.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_not_called()

    async def test_excluded_container_skipped(self, make_engine, make_rule, make_metrics) -> None:  # type: ignore[no-untyped-def]
        """Excluded containers should never trigger actions."""
        engine, action_mock, _ = make_engine([
            make_rule(excludes=[r"^sentinel$"], threshold=10.0, sustained=0)
        ])

        metrics = [make_metrics(container_name="sentinel", cpu_percent=99.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_not_called()

    async def test_circuit_breaker_prevents_action(  # type: ignore[no-untyped-def]
        self, make_engine, make_rule, make_metrics,
    ) -> None:
        """When circuit breaker is open, action should NOT execute."""
        engine, action_mock, sm_mock = make_engine([
            make_rule(threshold=80.0, sustained=0)
        ])

        # Simulate an open circuit breaker
        sm_mock.check_circuit_breaker = AsyncMock(
            side_effect=CircuitBreakerOpen("webapp", 5, 5)
        )

        metrics = [make_metrics(cpu_percent=95.0)]
        await engine.evaluate(metrics)

        # Action must NOT have been called
        action_mock.execute.assert_not_called()
        # But intervention should NOT be recorded either (breaker prevented it)
        sm_mock.record_intervention.assert_not_called()

    async def test_multiple_containers_evaluated(  # type: ignore[no-untyped-def]
        self, make_engine, make_rule, make_metrics,
    ) -> None:
        """All containers in the batch should be evaluated against the rules."""
        engine, action_mock, _ = make_engine([
            make_rule(threshold=80.0, sustained=0)
        ])

        metrics = [
            make_metrics(container_name="web-1", cpu_percent=95.0),
            make_metrics(container_name="web-2", cpu_percent=30.0),
            make_metrics(container_name="web-3", cpu_percent=91.0),
        ]
        await engine.evaluate(metrics)

        # Only web-1 and web-3 exceed threshold
        assert action_mock.execute.call_count == 2

    async def test_violation_tracker_cleared_when_condition_resolves(
        self, make_engine, make_rule, make_metrics
    ) -> None:  # type: ignore[no-untyped-def]
        """When a violation stops, the tracker should be pruned."""
        engine, _, _ = make_engine([
            make_rule(threshold=80.0, sustained=60)  # Requires 60s sustained
        ])

        # First cycle: violation starts (but won't trigger due to sustained)
        await engine.evaluate([make_metrics(cpu_percent=95.0)])
        assert len(engine._violations) == 1

        # Second cycle: condition resolves
        await engine.evaluate([make_metrics(cpu_percent=50.0)])
        assert len(engine._violations) == 0
