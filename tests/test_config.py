"""
Sentinel - Configuration Module Tests

Validates the Pydantic models, YAML parsing, and fail-fast behavior
for malformed configuration files.

Uses the ``sample_rules_yaml`` fixture from conftest.py for
valid YAML testing.
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

    def test_log_level_case_insensitive(self) -> None:
        """Log level should be normalized to uppercase."""
        settings = SentinelSettings(
            log_level="debug",  # type: ignore[call-arg]
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.log_level == "DEBUG"

    def test_port_boundaries(self) -> None:
        """API port must be between 1024 and 65535."""
        with pytest.raises(Exception):
            SentinelSettings(
                api_port=80,  # type: ignore[call-arg]
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

    def test_invalid_exclude_regex(self) -> None:
        """Invalid exclude regex should fail validation."""
        with pytest.raises(Exception):
            MatchConfig(exclude_patterns=[r"[broken"])

    def test_default_pattern_matches_all(self) -> None:
        """Default pattern '.*' should match everything."""
        config = MatchConfig()
        assert config.container_name_pattern == ".*"
        assert config.exclude_patterns == []


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

    def test_all_valid_metrics(self) -> None:
        """All documented metric types should pass validation."""
        for metric in ("cpu_percent", "memory_percent", "memory_usage_mb", "health_status"):
            config = ConditionConfig(
                metric=metric,
                operator=ConditionOperator.GT,
                threshold=50.0,
            )
            assert config.metric == metric

    def test_sustained_seconds_non_negative(self) -> None:
        """Sustained seconds must be >= 0."""
        config = ConditionConfig(
            metric="cpu_percent",
            operator=ConditionOperator.GT,
            threshold=90.0,
            sustained_seconds=0,
        )
        assert config.sustained_seconds == 0


class TestActionConfig:
    """Test action configuration validation."""

    def test_exec_requires_command(self) -> None:
        """exec action type must have a command field."""
        with pytest.raises(Exception):
            ActionConfig(type=ActionType.EXEC)

    def test_exec_with_command_valid(self) -> None:
        """exec action with command should pass."""
        config = ActionConfig(type=ActionType.EXEC, command="echo hello")
        assert config.command == "echo hello"

    def test_scale_requires_replicas(self) -> None:
        """scale action type must have a replicas field."""
        with pytest.raises(Exception):
            ActionConfig(type=ActionType.SCALE)

    def test_scale_with_replicas_valid(self) -> None:
        """scale action with replicas should pass."""
        config = ActionConfig(type=ActionType.SCALE, replicas=3)
        assert config.replicas == 3

    def test_restart_no_extra_fields(self) -> None:
        """restart action should not require extra fields."""
        config = ActionConfig(type=ActionType.RESTART)
        assert config.timeout == 30

    def test_stop_action_valid(self) -> None:
        """stop action should not require extra fields."""
        config = ActionConfig(type=ActionType.STOP)
        assert config.type == ActionType.STOP


class TestLoadRules:
    """Test YAML loading and validation."""

    def test_valid_rules_file(self, sample_rules_yaml: str) -> None:
        """A well-formed rules.yaml should parse correctly."""
        rules = load_rules(sample_rules_yaml)

        assert len(rules.rules) == 1
        assert rules.rules[0].name == "Test CPU Rule"
        assert rules.rules[0].condition.threshold == 80.0
        assert rules.global_config.poll_interval == 10

    def test_missing_file(self) -> None:
        """Missing rules file should raise ConfigurationError."""
        with pytest.raises(ConfigurationError, match="not found"):
            load_rules("/nonexistent/rules.yaml")

    def test_empty_rules(self, tmp_path: Path) -> None:
        """Rules file with no rules should fail validation."""
        yaml_content = """\
global:
  poll_interval: 10
rules: []
"""
        path = tmp_path / "empty.yaml"
        path.write_text(yaml_content)
        with pytest.raises(ConfigurationError):
            load_rules(str(path))

    def test_invalid_yaml_syntax(self, tmp_path: Path) -> None:
        """Broken YAML should raise ConfigurationError."""
        path = tmp_path / "broken.yaml"
        path.write_text("  invalid:\nyaml: [broken")
        with pytest.raises(ConfigurationError):
            load_rules(str(path))

    def test_rules_file_exclude_patterns_preserved(self, sample_rules_yaml: str) -> None:
        """Exclude patterns from YAML should be preserved after parsing."""
        rules = load_rules(sample_rules_yaml)
        assert "^sentinel$" in rules.rules[0].match.exclude_patterns

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        """YAML that is a list instead of a mapping should fail."""
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigurationError, match="mapping"):
            load_rules(str(path))
