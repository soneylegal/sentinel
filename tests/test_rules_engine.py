"""
Sentinel - Rules Engine Tests

Tests the rules evaluation logic: container matching, condition
evaluation, sustained-duration tracking, and violation lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import (
    ActionConfig,
    ActionType,
    ConditionConfig,
    ConditionOperator,
    MatchConfig,
    NotifyConfig,
    RuleConfig,
    Severity,
)
from src.engine.rules import RulesEngine


def _make_metrics(
    container_name: str = "webapp",
    cpu_percent: float = 50.0,
    memory_percent: float = 40.0,
    health_status: str = "healthy",
) -> MagicMock:
    """Create a mock ContainerMetrics object."""
    m = MagicMock()
    m.container_id = "abc123"
    m.container_name = container_name
    m.image = "nginx:latest"
    m.status = "running"
    m.health_status = health_status
    m.cpu_percent = cpu_percent
    m.memory_percent = memory_percent
    m.memory_usage_mb = memory_percent * 10
    m.memory_limit_mb = 1024.0
    m.pids = 5
    m.timestamp = datetime.now(timezone.utc)
    return m


def _make_rule(
    name: str = "Test Rule",
    pattern: str = ".*",
    excludes: list[str] | None = None,
    metric: str = "cpu_percent",
    operator: str = ">",
    threshold: float | str = 80.0,
    sustained: int = 0,
    action_type: str = "restart",
) -> RuleConfig:
    """Create a RuleConfig for testing."""
    return RuleConfig(
        name=name,
        enabled=True,
        match=MatchConfig(
            container_name_pattern=pattern,
            exclude_patterns=excludes or [],
        ),
        condition=ConditionConfig(
            metric=metric,
            operator=ConditionOperator(operator),
            threshold=threshold,
            sustained_seconds=sustained,
        ),
        action=ActionConfig(type=ActionType(action_type)),
        notify=NotifyConfig(channels=["console"], severity=Severity.WARNING),
    )


def _make_engine(rules: list[RuleConfig] | None = None) -> tuple[RulesEngine, AsyncMock, AsyncMock]:
    """Create a RulesEngine with mocked dependencies."""
    state_manager = AsyncMock()
    state_manager.check_circuit_breaker = AsyncMock(return_value=None)
    state_manager.record_intervention = AsyncMock(return_value=1)

    action_mock = AsyncMock()
    action_mock.execute = AsyncMock()

    notifier_mock = AsyncMock()
    notifier_mock.send = AsyncMock()

    engine = RulesEngine(
        rules=rules or [_make_rule()],
        state_manager=state_manager,
        actions={"restart": action_mock, "stop": action_mock},
        notifiers={"console": notifier_mock},
    )

    return engine, action_mock, state_manager


class TestContainerMatching:
    """Test regex-based container matching."""

    def test_matches_all(self) -> None:
        engine, _, _ = _make_engine([_make_rule(pattern=".*")])
        assert engine._matches_container(engine._rules[0], "anything")

    def test_specific_pattern(self) -> None:
        engine, _, _ = _make_engine([_make_rule(pattern=r"^web-\d+$")])
        assert engine._matches_container(engine._rules[0], "web-1")
        assert not engine._matches_container(engine._rules[0], "api-server")

    def test_exclude_pattern(self) -> None:
        engine, _, _ = _make_engine([_make_rule(excludes=[r"^sentinel$"])])
        assert engine._matches_container(engine._rules[0], "webapp")
        assert not engine._matches_container(engine._rules[0], "sentinel")


class TestConditionEvaluation:
    """Test metric condition comparison logic."""

    def test_cpu_above_threshold(self) -> None:
        engine, _, _ = _make_engine([_make_rule(metric="cpu_percent", operator=">", threshold=80.0)])
        metrics = _make_metrics(cpu_percent=95.0)
        assert engine._condition_met(engine._rules[0], metrics)

    def test_cpu_below_threshold(self) -> None:
        engine, _, _ = _make_engine([_make_rule(metric="cpu_percent", operator=">", threshold=80.0)])
        metrics = _make_metrics(cpu_percent=50.0)
        assert not engine._condition_met(engine._rules[0], metrics)

    def test_health_status_comparison(self) -> None:
        engine, _, _ = _make_engine([
            _make_rule(metric="health_status", operator="==", threshold="unhealthy")
        ])
        metrics = _make_metrics(health_status="unhealthy")
        assert engine._condition_met(engine._rules[0], metrics)

    def test_less_than_operator(self) -> None:
        engine, _, _ = _make_engine([
            _make_rule(metric="memory_percent", operator="<", threshold=20.0)
        ])
        metrics = _make_metrics(memory_percent=10.0)
        assert engine._condition_met(engine._rules[0], metrics)


@pytest.mark.asyncio
class TestRuleEvaluation:
    """Test full rule evaluation with action triggering."""

    async def test_immediate_trigger(self) -> None:
        """Rule with sustained=0 should trigger immediately."""
        engine, action_mock, state_manager = _make_engine([
            _make_rule(threshold=80.0, sustained=0)
        ])

        metrics = [_make_metrics(cpu_percent=95.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_called_once()
        state_manager.record_intervention.assert_called_once()

    async def test_no_trigger_below_threshold(self) -> None:
        """Metrics below threshold should not trigger any action."""
        engine, action_mock, _ = _make_engine([
            _make_rule(threshold=80.0, sustained=0)
        ])

        metrics = [_make_metrics(cpu_percent=50.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_not_called()

    async def test_excluded_container_skipped(self) -> None:
        """Excluded containers should never trigger actions."""
        engine, action_mock, _ = _make_engine([
            _make_rule(excludes=[r"^sentinel$"], threshold=10.0, sustained=0)
        ])

        metrics = [_make_metrics(container_name="sentinel", cpu_percent=99.0)]
        await engine.evaluate(metrics)

        action_mock.execute.assert_not_called()
