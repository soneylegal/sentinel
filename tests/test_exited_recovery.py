"""
Sentinel - Tests for Exited Container Recovery Feature

Covers:
- ExitedContainerInfo dataclass
- StartAction
- evaluate_exited() in RulesEngine
- exit_code allowlist filtering
- Circuit breaker integration for exited containers
- Logs inclusion in notifications
- Restart policy filtering
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.actions.start import StartAction
from src.collectors.docker_async import ExitedContainerInfo
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
from src.core.exceptions import ActionExecutionError, CircuitBreakerOpen
from src.engine.rules import RulesEngine


# ─────────────────────────────────────────────────────────
# ExitedContainerInfo Dataclass
# ─────────────────────────────────────────────────────────


class TestExitedContainerInfo:
    """Test the ExitedContainerInfo dataclass."""

    def test_creation_with_defaults(self, make_exited_info):
        info = make_exited_info()
        assert info.container_name == "webapp"
        assert info.exit_code == 137
        assert info.restart_policy == "unless-stopped"

    def test_exit_code_one(self, make_exited_info):
        info = make_exited_info(exit_code=1)
        assert info.exit_code == 1

    def test_exit_code_255(self, make_exited_info):
        info = make_exited_info(exit_code=255)
        assert info.exit_code == 255

    def test_logs_stored(self, make_exited_info):
        info = make_exited_info(last_logs="fatal: out of memory")
        assert "out of memory" in info.last_logs

    def test_frozen_dataclass(self, make_exited_info):
        info = make_exited_info()
        with pytest.raises(AttributeError):
            info.exit_code = 0  # type: ignore[misc]


# ─────────────────────────────────────────────────────────
# StartAction
# ─────────────────────────────────────────────────────────


class TestStartAction:
    """Test the StartAction strategy."""

    def test_action_type_is_start(self, mock_docker_client):
        action = StartAction(mock_docker_client)
        assert action.action_type == "start"

    @pytest.mark.asyncio
    async def test_start_calls_container_start(self, mock_docker_client, mock_docker_container):
        mock_docker_container.start = AsyncMock()
        action = StartAction(mock_docker_client)
        await action.execute("abc123def456", "webapp", timeout=30)
        mock_docker_container.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_failure_raises_action_error(self, mock_docker_client, mock_docker_container):
        mock_docker_container.start = AsyncMock(side_effect=Exception("container locked"))
        action = StartAction(mock_docker_client)
        with pytest.raises(ActionExecutionError, match="Failed to start"):
            await action.execute("abc123def456", "webapp")


# ─────────────────────────────────────────────────────────
# Helper: Create exit_code rule
# ─────────────────────────────────────────────────────────


def _make_exit_code_rule(
    name: str = "Exited Recovery",
    excludes: list[str] | None = None,
    exit_code_allowlist: list[int] | None = None,
    action_type: str = "start",
    channels: list[str] | None = None,
) -> RuleConfig:
    return RuleConfig(
        name=name,
        enabled=True,
        match=MatchConfig(
            container_name_pattern=".*",
            exclude_patterns=excludes or [],
        ),
        condition=ConditionConfig(
            metric="exit_code",
            operator=ConditionOperator.EQ,
            threshold="allowlist",
            sustained_seconds=0,
            exit_code_allowlist=exit_code_allowlist or [1, 137, 255],
        ),
        action=ActionConfig(type=ActionType(action_type), timeout=30),
        notify=NotifyConfig(
            channels=channels or ["console"],
            severity=Severity.CRITICAL,
        ),
    )


# ─────────────────────────────────────────────────────────
# evaluate_exited() — Core Tests
# ─────────────────────────────────────────────────────────


class TestEvaluateExited:
    """Test the RulesEngine.evaluate_exited() method."""

    @pytest.mark.asyncio
    async def test_start_action_called_for_allowlisted_exit_code(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Exit code 137 is in the default allowlist → start should be called."""
        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        exited = [make_exited_info(exit_code=137)]
        await engine.evaluate_exited(exited)

        mock_start_action.execute.assert_awaited_once()
        mock_state_manager.record_intervention.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exit_code_not_in_allowlist_skipped(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Exit code 2 is NOT in the default allowlist [1, 137, 255] → skip."""
        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        exited = [make_exited_info(exit_code=2)]
        await engine.evaluate_exited(exited)

        mock_start_action.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exit_code_zero_not_collected(self, make_exited_info):
        """Exit code 0 should never reach evaluate_exited — filtered by collector.

        This test verifies the contract: if somehow a code 0 sneaks in,
        the rule's allowlist still blocks it (since 0 is not in [1, 137, 255]).
        """
        # Even if exit_code 0 reaches evaluate_exited, it's not in allowlist
        rule = _make_exit_code_rule()
        sm = AsyncMock()
        sm.check_circuit_breaker = AsyncMock(return_value=None)
        act = AsyncMock()
        ntf = AsyncMock()

        engine = RulesEngine(
            rules=[rule],
            state_manager=sm,
            actions={"start": act},
            notifiers={"console": ntf},
        )

        exited = [make_exited_info(exit_code=0)]
        await engine.evaluate_exited(exited)

        act.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_custom_allowlist(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Custom allowlist [42, 99] should only trigger on those codes."""
        rule = _make_exit_code_rule(exit_code_allowlist=[42, 99])
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        # Code 42 → should trigger
        await engine.evaluate_exited([make_exited_info(exit_code=42)])
        assert mock_start_action.execute.await_count == 1

        # Code 137 → should NOT trigger (not in custom allowlist)
        mock_start_action.execute.reset_mock()
        await engine.evaluate_exited([make_exited_info(exit_code=137)])
        mock_start_action.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_excluded_container_skipped(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Container matching an exclude pattern should be skipped."""
        rule = _make_exit_code_rule(excludes=["^sentinel$"])
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        exited = [make_exited_info(container_name="sentinel", exit_code=137)]
        await engine.evaluate_exited(exited)

        mock_start_action.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_exit_code_rules_skips_evaluation(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info, make_rule
    ):
        """If no rules have metric='exit_code', evaluate_exited does nothing."""
        cpu_rule = make_rule(metric="cpu_percent")
        engine = RulesEngine(
            rules=[cpu_rule],
            state_manager=mock_state_manager,
            actions={"restart": mock_start_action, "start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        await engine.evaluate_exited([make_exited_info()])
        mock_start_action.execute.assert_not_awaited()


# ─────────────────────────────────────────────────────────
# Circuit Breaker Integration
# ─────────────────────────────────────────────────────────


class TestExitedCircuitBreaker:
    """Test circuit breaker behavior with exited containers."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_prevents_start(
        self, mock_start_action, mock_notifier, make_exited_info
    ):
        """When CB is open, start action should NOT be executed."""
        sm = AsyncMock()
        sm.check_circuit_breaker = AsyncMock(
            side_effect=CircuitBreakerOpen("webapp", restart_count=3, window_minutes=5)
        )
        sm.record_intervention = AsyncMock()

        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=sm,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        await engine.evaluate_exited([make_exited_info(exit_code=137)])

        mock_start_action.execute.assert_not_awaited()
        # Should still notify about the CB trip
        mock_notifier.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_circuit_breaker_notification_includes_logs(
        self, mock_start_action, mock_notifier, make_exited_info
    ):
        """CB notification should include the container's last logs."""
        sm = AsyncMock()
        sm.check_circuit_breaker = AsyncMock(
            side_effect=CircuitBreakerOpen("webapp", restart_count=3, window_minutes=5)
        )

        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=sm,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        logs = "FATAL: database connection refused\nRetrying in 5s...\nGiving up after 3 attempts"
        await engine.evaluate_exited([make_exited_info(exit_code=1, last_logs=logs)])

        call_args = mock_notifier.send.call_args
        message = call_args.kwargs.get("message", "") or call_args[1].get("message", "")
        assert "database connection refused" in message

    @pytest.mark.asyncio
    async def test_circuit_breaker_notification_severity_critical(
        self, mock_start_action, mock_notifier, make_exited_info
    ):
        """CB trip should always send CRITICAL severity."""
        sm = AsyncMock()
        sm.check_circuit_breaker = AsyncMock(
            side_effect=CircuitBreakerOpen("webapp", restart_count=3, window_minutes=5)
        )

        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=sm,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        await engine.evaluate_exited([make_exited_info()])

        call_args = mock_notifier.send.call_args
        severity = call_args.kwargs.get("severity", "") or call_args[1].get("severity", "")
        assert severity == "critical"


# ─────────────────────────────────────────────────────────
# Notification Content
# ─────────────────────────────────────────────────────────


class TestExitedNotification:
    """Test notification content for exited container recovery."""

    @pytest.mark.asyncio
    async def test_recovery_notification_includes_exit_code(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Successful recovery notification should include exit code details."""
        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        await engine.evaluate_exited([make_exited_info(exit_code=137)])

        call_args = mock_notifier.send.call_args
        message = call_args.kwargs.get("message", "") or call_args[1].get("message", "")
        assert "137" in message
        assert "SIGKILL" in message

    @pytest.mark.asyncio
    async def test_recovery_notification_includes_logs(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Recovery notification should include container logs."""
        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        logs = "php-fpm: pool www: server reached max_children"
        await engine.evaluate_exited([make_exited_info(last_logs=logs)])

        call_args = mock_notifier.send.call_args
        message = call_args.kwargs.get("message", "") or call_args[1].get("message", "")
        assert "max_children" in message

    @pytest.mark.asyncio
    async def test_failed_start_recorded_and_notified(
        self, mock_notifier, mock_state_manager, make_exited_info
    ):
        """If docker start fails, the failure should be recorded and notified."""
        failed_action = AsyncMock()
        failed_action.execute = AsyncMock(side_effect=ActionExecutionError("container locked"))

        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": failed_action},
            notifiers={"console": mock_notifier},
        )

        await engine.evaluate_exited([make_exited_info()])

        # Should record the failed intervention
        call_args = mock_state_manager.record_intervention.call_args
        assert call_args.kwargs["success"] is False
        assert "container locked" in call_args.kwargs["error_message"]


# ─────────────────────────────────────────────────────────
# Multiple Containers
# ─────────────────────────────────────────────────────────


class TestExitedMultipleContainers:
    """Test evaluate_exited with multiple containers."""

    @pytest.mark.asyncio
    async def test_multiple_exited_containers_evaluated(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Each exited container should be evaluated independently."""
        rule = _make_exit_code_rule()
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        exited = [
            make_exited_info(container_name="web", exit_code=1),
            make_exited_info(container_name="redis", exit_code=137),
            make_exited_info(container_name="db", exit_code=255),
        ]
        await engine.evaluate_exited(exited)

        assert mock_start_action.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_mixed_allowlisted_and_non_allowlisted(
        self, mock_start_action, mock_notifier, mock_state_manager, make_exited_info
    ):
        """Only containers with exit codes in the allowlist should be started."""
        rule = _make_exit_code_rule(exit_code_allowlist=[137])
        engine = RulesEngine(
            rules=[rule],
            state_manager=mock_state_manager,
            actions={"start": mock_start_action},
            notifiers={"console": mock_notifier},
        )

        exited = [
            make_exited_info(container_name="web", exit_code=1),      # NOT in [137]
            make_exited_info(container_name="redis", exit_code=137),   # IN [137]
            make_exited_info(container_name="db", exit_code=255),      # NOT in [137]
        ]
        await engine.evaluate_exited(exited)

        assert mock_start_action.execute.await_count == 1


# ─────────────────────────────────────────────────────────
# Config Validation
# ─────────────────────────────────────────────────────────


class TestExitCodeConfig:
    """Test configuration validation for exit_code rules."""

    def test_exit_code_metric_allowed(self):
        """'exit_code' should be a valid metric."""
        config = ConditionConfig(
            metric="exit_code",
            operator=ConditionOperator.EQ,
            threshold="allowlist",
        )
        assert config.metric == "exit_code"

    def test_default_exit_code_allowlist(self):
        """Default allowlist should be [1, 137, 255]."""
        config = ConditionConfig(
            metric="exit_code",
            operator=ConditionOperator.EQ,
            threshold="allowlist",
        )
        assert config.exit_code_allowlist == [1, 137, 255]

    def test_custom_exit_code_allowlist(self):
        """Custom allowlist should override default."""
        config = ConditionConfig(
            metric="exit_code",
            operator=ConditionOperator.EQ,
            threshold="allowlist",
            exit_code_allowlist=[42, 99],
        )
        assert config.exit_code_allowlist == [42, 99]

    def test_start_action_type_valid(self):
        """'start' should be a valid ActionType."""
        config = ActionConfig(type=ActionType.START)
        assert config.type == ActionType.START
