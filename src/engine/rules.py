"""
Sentinel - Rules Engine (The Brain)

Evaluates container metrics against configured rules, respecting
sustained-duration windows, exclusion patterns, and circuit breaker state.
Orchestrates the execution of actions and dispatch of notifications.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.core.config import ConditionOperator, RuleConfig, Severity
from src.core.exceptions import CircuitBreakerOpen
from src.core.logger import get_logger

if TYPE_CHECKING:
    from src.actions.base import BaseAction
    from src.collectors.docker_async import ContainerMetrics, ExitedContainerInfo
    from src.engine.state_manager import StateManager
    from src.notifiers.base import BaseNotifier

logger = get_logger()


@dataclass
class ViolationTracker:
    """Tracks how long a container has been violating a specific rule.

    Used to implement the `sustained_seconds` feature — a condition
    must persist for a minimum duration before triggering an action.
    """

    first_seen: float = field(default_factory=lambda: time.monotonic())
    last_seen: float = field(default_factory=lambda: time.monotonic())

    @property
    def duration_seconds(self) -> float:
        return self.last_seen - self.first_seen

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class RulesEngine:
    """Core rules evaluation engine.

    Responsibilities:
    - Match containers against rule patterns (include/exclude).
    - Evaluate metric conditions with operator comparison.
    - Track sustained violations before triggering.
    - Consult the circuit breaker before executing actions.
    - Delegate execution to Action strategies.
    - Dispatch notifications via Notifier strategies.
    """

    def __init__(
        self,
        rules: list[RuleConfig],
        state_manager: StateManager,
        actions: dict[str, BaseAction],
        notifiers: dict[str, BaseNotifier],
    ) -> None:
        self._rules = [r for r in rules if r.enabled]
        self._state_manager = state_manager
        self._actions = actions
        self._notifiers = notifiers

        # Violation tracking: (container_name, rule_name) -> ViolationTracker
        self._violations: dict[tuple[str, str], ViolationTracker] = {}

        # Metrics history tracking: (container_name, metric_name) -> list of (timestamp, value)
        self._metrics_history: dict[tuple[str, str], list[tuple[float, float]]] = {}

        logger.info(
            f"Rules engine initialized with {len(self._rules)} active rules",
            component="engine.rules",
        )

    async def evaluate(self, metrics_batch: list[ContainerMetrics]) -> None:
        """Evaluate all rules against a batch of container metrics.

        This is the main entry point called on each polling cycle.
        """
        active_violations: set[tuple[str, str]] = set()

        for metrics in metrics_batch:
            for rule in self._rules:
                if not self._matches_container(rule, metrics.container_name):
                    continue

                if self._condition_met(rule, metrics):
                    key = (metrics.container_name, rule.name)
                    active_violations.add(key)

                    if key not in self._violations:
                        self._violations[key] = ViolationTracker()
                        logger.info(
                            f"Violation detected: '{rule.name}' on '{metrics.container_name}'",
                            component="engine.rules",
                        )
                    else:
                        self._violations[key].touch()

                    tracker = self._violations[key]

                    # Check if sustained duration threshold is met
                    if tracker.duration_seconds >= rule.condition.sustained_seconds:
                        await self._trigger_action(rule, metrics)
                        # Reset tracker after action
                        self._violations.pop(key, None)

        # Prune violations that are no longer active (condition no longer met)
        stale_keys = set(self._violations.keys()) - active_violations
        for key in stale_keys:
            logger.debug(
                f"Violation cleared: rule='{key[1]}' on container='{key[0]}'",
                component="engine.rules",
            )
            self._violations.pop(key, None)

    def _matches_container(self, rule: RuleConfig, container_name: str) -> bool:
        """Check if a container name matches the rule's include/exclude patterns."""
        # Check include pattern
        if not re.search(rule.match.container_name_pattern, container_name):
            return False

        # Check exclude patterns
        for exclude_pattern in rule.match.exclude_patterns:
            if re.search(exclude_pattern, container_name):
                return False

        return True

    def _condition_met(self, rule: RuleConfig, metrics: ContainerMetrics) -> bool:
        """Evaluate whether a rule's condition is satisfied by the metrics."""
        metric_value = self._get_metric_value(rule.condition.metric, metrics)
        threshold = rule.condition.threshold

        # Handle string comparisons (e.g., health_status == "unhealthy")
        if isinstance(threshold, str):
            return str(metric_value) == threshold

        # Handle numeric smoothing / sliding window average
        metric_name = rule.condition.metric
        key = (metrics.container_name, metric_name)
        now = time.monotonic()

        if key not in self._metrics_history:
            self._metrics_history[key] = []

        history = self._metrics_history[key]
        history.append((now, float(metric_value)))

        # Prune readings older than now - sustained_seconds
        cutoff = now - rule.condition.sustained_seconds
        # Keep at least the latest reading to avoid an empty list
        while len(history) > 1 and history[0][0] < cutoff:
            history.pop(0)

        # Compute average value in the sliding window
        value = sum(val for _, val in history) / len(history)
        thresh = float(threshold)
        op = rule.condition.operator

        if op == ConditionOperator.GT:
            return value > thresh
        elif op == ConditionOperator.LT:
            return value < thresh
        elif op == ConditionOperator.GTE:
            return value >= thresh
        elif op == ConditionOperator.LTE:
            return value <= thresh
        elif op == ConditionOperator.EQ:
            return value == thresh

        return False

    @staticmethod
    def _get_metric_value(metric: str, metrics: ContainerMetrics) -> float | str:
        """Extract a named metric value from the ContainerMetrics dataclass."""
        mapping: dict[str, float | str] = {
            "cpu_percent": metrics.cpu_percent,
            "memory_percent": metrics.memory_percent,
            "memory_usage_mb": metrics.memory_usage_mb,
            "health_status": metrics.health_status,
            "status": metrics.status,
        }
        return mapping.get(metric, 0.0)

    async def _trigger_action(self, rule: RuleConfig, metrics: ContainerMetrics) -> None:
        """Execute the rule's action after consulting the circuit breaker."""
        container_name = metrics.container_name
        action_type = rule.action.type.value

        # ── Circuit Breaker Check ──
        try:
            await self._state_manager.check_circuit_breaker(container_name)
        except CircuitBreakerOpen as e:
            logger.warning(
                f"Circuit breaker OPEN: {e}",
                component="engine.rules",
            )
            await self._notify_all(
                rule,
                metrics,
                title="🔴 CIRCUIT BREAKER TRIPPED",
                message=(
                    f"Container '{container_name}' has been restarted too many times. "
                    f"Autonomous action SUSPENDED. Human intervention required."
                ),
                severity=Severity.CRITICAL,
            )
            return

        # ── Execute Action ──
        action = self._actions.get(action_type)
        if not action:
            logger.error(
                f"No action handler registered for type '{action_type}'",
                component="engine.rules",
            )
            return

        logger.warning(
            f"Executing action '{action_type}' on '{container_name}' (rule='{rule.name}')",
            component="engine.rules",
        )

        success = True
        error_msg: str | None = None

        try:
            await action.execute(
                container_id=metrics.container_id,
                container_name=container_name,
                timeout=rule.action.timeout,
            )
        except Exception as e:
            success = False
            error_msg = str(e)
            logger.error(
                f"Action '{action_type}' failed on '{container_name}': {e}",
                component="engine.rules",
            )

        # ── Record Intervention ──
        await self._state_manager.record_intervention(
            container_id=metrics.container_id,
            container_name=container_name,
            rule_name=rule.name,
            action_type=action_type,
            success=success,
            error_message=error_msg,
        )

        # ── Notify ──
        status_emoji = "✅" if success else "❌"
        await self._notify_all(
            rule,
            metrics,
            title=f"{status_emoji} Autonomous Action Executed",
            message=(
                f"**Action:** {action_type}\n"
                f"**Container:** {container_name}\n"
                f"**Rule:** {rule.name}\n"
                f"**Success:** {success}\n"
                f"**CPU:** {metrics.cpu_percent}% | **RAM:** {metrics.memory_percent}%"
            ),
            severity=rule.notify.severity,
        )

    async def _notify_all(
        self,
        rule: RuleConfig,
        metrics: ContainerMetrics,
        title: str,
        message: str,
        severity: Severity,
    ) -> None:
        """Send notifications to all channels configured for the rule."""
        tasks = []
        for channel_name in rule.notify.channels:
            notifier = self._notifiers.get(channel_name)
            if notifier:
                tasks.append(
                    notifier.send(
                        title=title,
                        message=message,
                        severity=severity.value,
                        container_name=metrics.container_name,
                    )
                )
            else:
                logger.debug(
                    f"Notifier '{channel_name}' not registered, skipping",
                    component="engine.rules",
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ─────────────────────────────────────────────────────────
    # Exited Container Evaluation
    # ─────────────────────────────────────────────────────────

    async def evaluate_exited(self, exited_batch: list[ExitedContainerInfo]) -> None:
        """Evaluate exited containers against rules with metric='exit_code'.

        This runs in parallel with evaluate() on each polling cycle.
        Only rules with `condition.metric == 'exit_code'` are considered.
        """
        exit_rules = [r for r in self._rules if r.condition.metric == "exit_code"]
        if not exit_rules:
            return

        for exited in exited_batch:
            for rule in exit_rules:
                if not self._matches_container(rule, exited.container_name):
                    continue

                # Check if this exit code is in the rule's allowlist
                if exited.exit_code not in rule.condition.exit_code_allowlist:
                    logger.debug(
                        f"Exit code {exited.exit_code} not in allowlist for "
                        f"'{exited.container_name}', skipping",
                        component="engine.rules",
                    )
                    continue

                await self._trigger_exited_action(rule, exited)

    async def _trigger_exited_action(
        self, rule: RuleConfig, exited: ExitedContainerInfo
    ) -> None:
        """Execute recovery action for an exited container.

        Flow:
        1. Check circuit breaker
        2. If open → notify CRITICAL with last logs (crash loop detected)
        3. If closed → execute start action → record intervention → notify
        """
        container_name = exited.container_name
        action_type = rule.action.type.value

        # ── Human-readable exit code description ──
        exit_descriptions: dict[int, str] = {
            1: "General error",
            137: "SIGKILL (OOM Killer or docker kill)",
            139: "SIGSEGV (Segmentation fault)",
            143: "SIGTERM (docker stop)",
            255: "Unknown/application-defined error",
        }
        exit_desc = exit_descriptions.get(exited.exit_code, "Unknown")

        # ── Circuit Breaker Check ──
        try:
            await self._state_manager.check_circuit_breaker(container_name)
        except CircuitBreakerOpen as e:
            logger.warning(
                f"Circuit breaker OPEN for exited container: {e}",
                component="engine.rules",
            )
            # Include last logs in the Circuit Breaker notification
            log_snippet = exited.last_logs[-1500:] if len(exited.last_logs) > 1500 else exited.last_logs
            await self._notify_all_by_name(
                rule,
                container_name=container_name,
                title="🔴 CIRCUIT BREAKER TRIPPED — Crash Loop Detected",
                message=(
                    f"**Container:** {container_name}\n"
                    f"**Exit Code:** {exited.exit_code} ({exit_desc})\n"
                    f"**Image:** {exited.image}\n"
                    f"**Parado desde:** {exited.finished_at}\n"
                    f"**Status:** Autonomous action SUSPENDED. Human intervention required.\n"
                    f"\n──── Últimos Logs ────\n```\n{log_snippet}\n```"
                ),
                severity=Severity.CRITICAL,
            )
            return

        # ── Execute Action ──
        action = self._actions.get(action_type)
        if not action:
            logger.error(
                f"No action handler registered for type '{action_type}'",
                component="engine.rules",
            )
            return

        logger.warning(
            f"Executing '{action_type}' on exited container '{container_name}' "
            f"(exit_code={exited.exit_code}, rule='{rule.name}')",
            component="engine.rules",
        )

        success = True
        error_msg: str | None = None

        try:
            await action.execute(
                container_id=exited.container_id,
                container_name=container_name,
                timeout=rule.action.timeout,
            )
        except Exception as e:
            success = False
            error_msg = str(e)
            logger.error(
                f"Action '{action_type}' failed on '{container_name}': {e}",
                component="engine.rules",
            )

        # ── Record Intervention ──
        await self._state_manager.record_intervention(
            container_id=exited.container_id,
            container_name=container_name,
            rule_name=rule.name,
            action_type=action_type,
            success=success,
            error_message=error_msg,
        )

        # ── Notify ──
        status_emoji = "✅" if success else "❌"
        log_snippet = exited.last_logs[-1500:] if len(exited.last_logs) > 1500 else exited.last_logs
        await self._notify_all_by_name(
            rule,
            container_name=container_name,
            title=f"{status_emoji} Exited Container Recovery",
            message=(
                f"**Action:** {action_type}\n"
                f"**Container:** {container_name}\n"
                f"**Exit Code:** {exited.exit_code} ({exit_desc})\n"
                f"**Image:** {exited.image}\n"
                f"**Rule:** {rule.name}\n"
                f"**Success:** {success}\n"
                f"\n──── Últimos Logs ────\n```\n{log_snippet}\n```"
            ),
            severity=rule.notify.severity,
        )

    async def _notify_all_by_name(
        self,
        rule: RuleConfig,
        container_name: str,
        title: str,
        message: str,
        severity: Severity,
    ) -> None:
        """Send notifications using a container name string.

        Similar to _notify_all but doesn't require a ContainerMetrics object.
        Used by evaluate_exited() where we have ExitedContainerInfo instead.
        """
        tasks = []
        for channel_name in rule.notify.channels:
            notifier = self._notifiers.get(channel_name)
            if notifier:
                tasks.append(
                    notifier.send(
                        title=title,
                        message=message,
                        severity=severity.value,
                        container_name=container_name,
                    )
                )
            else:
                logger.debug(
                    f"Notifier '{channel_name}' not registered, skipping",
                    component="engine.rules",
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
