"""
Sentinel - Shared Pytest Fixtures & Configuration

Centralizes all test infrastructure:
  - aiodocker mocks (no real Docker daemon required)
  - In-memory SQLite StateManager (via :memory:)
  - Reusable factories for ContainerMetrics, RuleConfig, and RulesEngine
  - FastAPI TestClient with pre-injected mocked dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.actions.base import BaseAction
from src.api.routes import app_state
from src.api.server import create_app
from src.collectors.docker_async import ContainerMetrics
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
from src.engine.state_manager import StateManager
from src.notifiers.base import BaseNotifier

# ─────────────────────────────────────────────────────────
# Docker Mock Infrastructure
# ─────────────────────────────────────────────────────────

# Realistic Docker stats payload (Linux cgroup v2 format)
SAMPLE_DOCKER_STATS: dict[str, Any] = {
    "cpu_stats": {
        "cpu_usage": {
            "total_usage": 500_000_000,
            "percpu_usage": [250_000_000, 250_000_000],
        },
        "system_cpu_usage": 10_000_000_000,
        "online_cpus": 2,
    },
    "precpu_stats": {
        "cpu_usage": {
            "total_usage": 400_000_000,
            "percpu_usage": [200_000_000, 200_000_000],
        },
        "system_cpu_usage": 9_000_000_000,
    },
    "memory_stats": {
        "usage": 104_857_600,  # 100 MiB
        "limit": 1_073_741_824,  # 1 GiB
        "stats": {
            "inactive_file": 10_485_760,  # 10 MiB cache
        },
    },
    "pids_stats": {
        "current": 12,
    },
}

SAMPLE_CONTAINER_INFO: dict[str, Any] = {
    "Id": "abc123def456789012345678",
    "Name": "/webapp",
    "Config": {
        "Image": "nginx:latest",
    },
    "State": {
        "Status": "running",
        "Health": {
            "Status": "healthy",
        },
    },
}


def _make_mock_docker_container(
    container_id: str = "abc123def456",
    name: str = "webapp",
    image: str = "nginx:latest",
    status: str = "running",
    health_status: str = "healthy",
    stats_override: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a fully mocked aiodocker container object.

    The mock implements the same interface as
    ``aiodocker.docker.DockerContainer`` so that
    ``DockerAsyncCollector._collect_single`` can consume it
    without touching a real Docker daemon.
    """
    container = MagicMock()
    container.id = container_id

    # container.show() → inspect info
    info = {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {"Image": image},
        "State": {
            "Status": status,
            "Health": {"Status": health_status},
        },
    }
    container.show = AsyncMock(return_value=info)

    # container.stats(stream=False) → async generator yielding one snapshot
    stats_data = stats_override or SAMPLE_DOCKER_STATS

    async def _fake_stats_stream(**kwargs: Any):  # type: ignore[no-untyped-def]
        yield stats_data

    container.stats = MagicMock(side_effect=lambda **kw: _fake_stats_stream(**kw))

    # container.restart(timeout=N) / container.stop(t=N)
    container.restart = AsyncMock()
    container.stop = AsyncMock()

    return container


@pytest.fixture
def mock_docker_container() -> MagicMock:
    """A single mocked Docker container with realistic stats."""
    return _make_mock_docker_container()


@pytest.fixture
def mock_docker_client(mock_docker_container: MagicMock) -> MagicMock:
    """Fully mocked aiodocker.Docker client.

    Provides:
      - client.version() → version dict
      - client.containers.list() → [mock_container]
      - client.containers.get(id) → mock_container
      - client.close() → coroutine
    """
    client = MagicMock()
    client.version = AsyncMock(return_value={"Version": "24.0.0", "ApiVersion": "1.43"})
    client.containers = MagicMock()
    client.containers.list = AsyncMock(return_value=[mock_docker_container])
    client.containers.get = AsyncMock(return_value=mock_docker_container)
    client.close = AsyncMock()
    return client


# ─────────────────────────────────────────────────────────
# State Manager (In-Memory SQLite)
# ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def state_manager(tmp_path) -> StateManager:  # type: ignore[misc]
    """StateManager backed by a temporary on-disk SQLite database.

    Uses ``tmp_path`` (pytest built-in) so each test gets a fresh,
    isolated database that is automatically cleaned up.

    Default circuit breaker config: threshold=3, window=5 min.
    """
    db_path = str(tmp_path / "sentinel_test.db")
    sm = StateManager(db_path=db_path, threshold=3, window_minutes=5)
    await sm.initialize()
    yield sm
    await sm.close()


@pytest_asyncio.fixture
async def state_manager_strict(tmp_path) -> StateManager:  # type: ignore[misc]
    """StateManager with a very strict circuit breaker (threshold=1).

    Useful for tests that need the breaker to trip on the first restart.
    """
    db_path = str(tmp_path / "sentinel_strict.db")
    sm = StateManager(db_path=db_path, threshold=1, window_minutes=10)
    await sm.initialize()
    yield sm
    await sm.close()


# ─────────────────────────────────────────────────────────
# ContainerMetrics Factory
# ─────────────────────────────────────────────────────────


@pytest.fixture
def make_metrics():  # type: ignore[no-untyped-def]
    """Factory fixture to create ContainerMetrics instances.

    Usage::

        def test_something(make_metrics):
            m = make_metrics(cpu_percent=95.0, container_name="web")
    """

    def _factory(
        container_id: str = "abc123def456",
        container_name: str = "webapp",
        image: str = "nginx:latest",
        status: str = "running",
        health_status: str = "healthy",
        cpu_percent: float = 25.0,
        memory_percent: float = 40.0,
        memory_usage_mb: float = 400.0,
        memory_limit_mb: float = 1024.0,
        pids: int = 5,
    ) -> ContainerMetrics:
        return ContainerMetrics(
            container_id=container_id,
            container_name=container_name,
            image=image,
            status=status,
            health_status=health_status,
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            memory_usage_mb=memory_usage_mb,
            memory_limit_mb=memory_limit_mb,
            pids=pids,
        )

    return _factory


# ─────────────────────────────────────────────────────────
# Rule Configuration Factory
# ─────────────────────────────────────────────────────────


@pytest.fixture
def make_rule():  # type: ignore[no-untyped-def]
    """Factory fixture to create RuleConfig instances.

    Usage::

        def test_something(make_rule):
            rule = make_rule(metric="cpu_percent", threshold=90.0)
    """

    def _factory(
        name: str = "Test Rule",
        enabled: bool = True,
        pattern: str = ".*",
        excludes: list[str] | None = None,
        metric: str = "cpu_percent",
        operator: str = ">",
        threshold: float | str = 80.0,
        sustained: int = 0,
        action_type: str = "restart",
        action_timeout: int = 30,
        channels: list[str] | None = None,
        severity: str = "warning",
    ) -> RuleConfig:
        return RuleConfig(
            name=name,
            enabled=enabled,
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
            action=ActionConfig(type=ActionType(action_type), timeout=action_timeout),
            notify=NotifyConfig(
                channels=channels or ["console"],
                severity=Severity(severity),
            ),
        )

    return _factory


# ─────────────────────────────────────────────────────────
# Mocked Action & Notifier Strategies
# ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_action() -> AsyncMock:
    """A mocked BaseAction with a tracked execute() method."""
    action = AsyncMock(spec=BaseAction)
    action.execute = AsyncMock()
    action.action_type = "restart"
    return action


@pytest.fixture
def mock_notifier() -> AsyncMock:
    """A mocked BaseNotifier with a tracked send() method."""
    notifier = AsyncMock(spec=BaseNotifier)
    notifier.send = AsyncMock()
    notifier.channel_name = "console"
    return notifier


@pytest.fixture
def mock_state_manager() -> AsyncMock:
    """A fully mocked StateManager (no SQLite, pure mock).

    Useful for engine tests that don't need real persistence.
    The circuit breaker is always CLOSED (check returns None).
    """
    sm = AsyncMock(spec=StateManager)
    sm.check_circuit_breaker = AsyncMock(return_value=None)
    sm.record_intervention = AsyncMock(return_value=1)
    sm.get_recent_history = AsyncMock(return_value=[])
    sm.get_circuit_breaker_status = AsyncMock(return_value=[])
    sm.reset_circuit_breaker = AsyncMock()
    return sm


# ─────────────────────────────────────────────────────────
# Rules Engine (pre-wired with mocks)
# ─────────────────────────────────────────────────────────


@pytest.fixture
def make_engine(mock_action, mock_notifier, mock_state_manager):  # type: ignore[no-untyped-def]
    """Factory fixture to create a RulesEngine with mocked dependencies.

    Returns a tuple of ``(engine, action_mock, state_manager_mock)``
    so tests can assert on calls.

    Usage::

        def test_something(make_engine, make_rule):
            engine, action, sm = make_engine([make_rule(threshold=90)])
            await engine.evaluate([...])
            action.execute.assert_called_once()
    """

    def _factory(
        rules: list[RuleConfig] | None = None,
        action_mock: AsyncMock | None = None,
        notifier_mock: AsyncMock | None = None,
        sm_mock: AsyncMock | None = None,
    ) -> tuple[RulesEngine, AsyncMock, AsyncMock]:
        act = action_mock or mock_action
        ntf = notifier_mock or mock_notifier
        sm = sm_mock or mock_state_manager

        default_rule = RuleConfig(
            name="Default",
            enabled=True,
            match=MatchConfig(container_name_pattern=".*"),
            condition=ConditionConfig(
                metric="cpu_percent",
                operator=ConditionOperator.GT,
                threshold=80.0,
            ),
            action=ActionConfig(type=ActionType.RESTART),
            notify=NotifyConfig(channels=["console"], severity=Severity.WARNING),
        )

        engine = RulesEngine(
            rules=rules or [default_rule],
            state_manager=sm,
            actions={"restart": act, "stop": act, "scale": act},
            notifiers={"console": ntf},
        )
        return engine, act, sm

    return _factory


# ─────────────────────────────────────────────────────────
# FastAPI TestClient
# ─────────────────────────────────────────────────────────


@pytest.fixture
def api_client() -> TestClient:
    """FastAPI TestClient with mocked collector and state manager.

    The ``app_state`` singleton is populated with mocks so that
    all API routes function without a real Docker daemon or database.
    """
    app = create_app()

    # Mock Docker collector
    mock_collector = MagicMock()
    mock_collector.is_connected = True
    app_state.collector = mock_collector

    # Mock state manager with sample history
    mock_state = AsyncMock()
    mock_state.get_recent_history = AsyncMock(
        return_value=[
            {
                "id": 1,
                "container_id": "abc123def456",
                "container_name": "webapp",
                "rule_name": "High CPU Auto-Restart",
                "action_type": "restart",
                "success": True,
                "error_message": None,
                "created_at": "2026-05-03T12:00:00Z",
            },
            {
                "id": 2,
                "container_id": "def789abc012",
                "container_name": "redis",
                "rule_name": "Memory Leak Detection",
                "action_type": "restart",
                "success": False,
                "error_message": "Container not found",
                "created_at": "2026-05-03T11:30:00Z",
            },
        ]
    )
    mock_state.get_circuit_breaker_status = AsyncMock(return_value=[])
    mock_state.reset_circuit_breaker = AsyncMock()
    app_state.state_manager = mock_state
    app_state.start_time = datetime.now(UTC)

    return TestClient(app)


# ─────────────────────────────────────────────────────────
# YAML Rules File (temp file fixture)
# ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_rules_yaml(tmp_path) -> str:  # type: ignore[no-untyped-def]
    """Write a valid rules.yaml to a temp directory and return its path.

    Useful for testing ``load_rules()`` without polluting the workspace.
    """
    content = """\
global:
  poll_interval: 10
  default_severity: warning

rules:
  - name: "Test CPU Rule"
    enabled: true
    match:
      container_name_pattern: ".*"
      exclude_patterns:
        - "^sentinel$"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80.0
      sustained_seconds: 30
    action:
      type: restart
      timeout: 15
    notify:
      channels:
        - console
      severity: critical
"""
    path = tmp_path / "rules.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)
