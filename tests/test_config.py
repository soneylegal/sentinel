"""
Sentinel - Configuration Module Tests

Validates the Pydantic models, YAML parsing, and fail-fast behavior
for malformed configuration files.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.config import (
    ActionConfig,
    ActionType,
    ConditionConfig,
    ConditionOperator,
    MatchConfig,
    RuleConfig,
    RulesFile,
    SentinelSettings,
    load_rules,
)
from src.core.exceptions import ConfigurationError


class TestSentinelSettings:
    """Test environment settings validation."""

    def test_default_values(self) -> None:
        """Settings should have sane defaults."""
        settings = SentinelSettings(
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.docker_url == "unix:///var/run/docker.sock"
        assert settings.api_port == 9120
        assert settings.poll_interval == 15
        assert settings.circuit_breaker_threshold == 3
        assert settings.log_level == "INFO"

    def test_invalid_log_level(self) -> None:
        """Invalid log level should raise ValueError."""
        with pytest.raises(Exception):
            SentinelSettings(
                log_level="INVALID",  # type: ignore[call-arg]
                _env_file=None,  # type: ignore[call-arg]
            )


class TestMatchConfig:
    """Test container matching patterns."""

    def test_valid_regex(self) -> None:
        """Valid regex should pass validation."""
        config = MatchConfig(container_name_pattern=r"^web-\d+$")
        assert config.container_name_pattern == r"^web-\d+$"

    def test_invalid_regex(self) -> None:
        """Invalid regex should fail validation."""
        with pytest.raises(Exception):
            MatchConfig(container_name_pattern="[invalid")

    def test_exclude_patterns(self) -> None:
        """Exclude patterns should validate as regex."""
        config = MatchConfig(exclude_patterns=[r"^sentinel$", r"^traefik.*"])
        assert len(config.exclude_patterns) == 2


class TestConditionConfig:
    """Test rule condition validation."""

    def test_valid_metric(self) -> None:
        """Known metrics should pass validation."""
        config = ConditionConfig(
            metric="cpu_percent",
            operator=ConditionOperator.GT,
            threshold=90.0,
        )
        assert config.metric == "cpu_percent"

    def test_invalid_metric(self) -> None:
        """Unknown metrics should fail validation."""
        with pytest.raises(Exception):
            ConditionConfig(
                metric="disk_io",
                operator=ConditionOperator.GT,
                threshold=90.0,
            )


class TestActionConfig:
    """Test action configuration validation."""

    def test_exec_requires_command(self) -> None:
        """exec action type must have a command field."""
        with pytest.raises(Exception):
            ActionConfig(type=ActionType.EXEC)

    def test_scale_requires_replicas(self) -> None:
        """scale action type must have a replicas field."""
        with pytest.raises(Exception):
            ActionConfig(type=ActionType.SCALE)

    def test_restart_no_extra_fields(self) -> None:
        """restart action should not require extra fields."""
        config = ActionConfig(type=ActionType.RESTART)
        assert config.timeout == 30


class TestLoadRules:
    """Test YAML loading and validation."""

    def test_valid_rules_file(self) -> None:
        """A well-formed rules.yaml should parse correctly."""
        yaml_content = """
global:
  poll_interval: 10
  default_severity: warning

rules:
  - name: "Test Rule"
    enabled: true
    match:
      container_name_pattern: ".*"
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            rules = load_rules(f.name)

        assert len(rules.rules) == 1
        assert rules.rules[0].name == "Test Rule"
        assert rules.rules[0].condition.threshold == 80.0
        assert rules.global_config.poll_interval == 10

    def test_missing_file(self) -> None:
        """Missing rules file should raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match="not found"):
            load_rules("/nonexistent/rules.yaml")

    def test_empty_rules(self) -> None:
        """Rules file with no rules should fail validation."""
        yaml_content = """
global:
  poll_interval: 10
rules: []
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ConfigurationError):
                load_rules(f.name)

    def test_invalid_yaml_syntax(self) -> None:
        """Broken YAML should raise ConfigurationError."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("  invalid:\nyaml: [broken")
            f.flush()
            with pytest.raises(ConfigurationError):
                load_rules(f.name)
