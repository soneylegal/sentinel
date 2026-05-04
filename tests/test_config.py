"""
Sentinel - Configuration Module Tests (Fail Fast)

Validates that the system rejects invalid configuration immediately,
refusing to start with malformed rules.yaml or .env parameters.

Every validation path in config.py has a corresponding rejection test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import (
    ActionConfig,
    ActionType,
    ConditionConfig,
    ConditionOperator,
    GlobalConfig,
    MatchConfig,
    NotifyConfig,
    RuleConfig,
    RulesFile,
    Severity,
    SentinelSettings,
    load_rules,
    load_settings,
)
from src.core.exceptions import ConfigurationError


# ═════════════════════════════════════════════════════════
# SentinelSettings (.env validation)
# ═════════════════════════════════════════════════════════
class TestSentinelSettings:
    """Test environment settings validation."""

    def test_default_values(self) -> None:
        """Settings should have sane defaults."""
        s = SentinelSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.docker_url == "unix:///var/run/docker.sock"
        assert s.api_port == 9120
        assert s.poll_interval == 15
        assert s.circuit_breaker_threshold == 3
        assert s.circuit_breaker_window_minutes == 5
        assert s.log_level == "INFO"
        assert s.log_format == "json"
        assert s.discord_webhook_url is None
        assert s.slack_webhook_url is None

    def test_log_level_case_insensitive(self) -> None:
        s = SentinelSettings(log_level="debug", _env_file=None)  # type: ignore[call-arg]
        assert s.log_level == "DEBUG"

    @pytest.mark.parametrize("bad_level", ["INVALID", "trace", "WARN", "verbose", ""])
    def test_invalid_log_level_rejected(self, bad_level: str) -> None:
        """Only DEBUG/INFO/WARNING/ERROR/CRITICAL are accepted."""
        with pytest.raises(Exception):
            SentinelSettings(log_level=bad_level, _env_file=None)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad_port", [0, 80, 443, 1023, 65536, -1])
    def test_port_out_of_range_rejected(self, bad_port: int) -> None:
        """API port must be 1024–65535."""
        with pytest.raises(Exception):
            SentinelSettings(api_port=bad_port, _env_file=None)  # type: ignore[call-arg]

    def test_port_boundaries_accepted(self) -> None:
        s1 = SentinelSettings(api_port=1024, _env_file=None)  # type: ignore[call-arg]
        s2 = SentinelSettings(api_port=65535, _env_file=None)  # type: ignore[call-arg]
        assert s1.api_port == 1024
        assert s2.api_port == 65535

    @pytest.mark.parametrize("bad_interval", [0, 1, 4, 301, -5])
    def test_poll_interval_out_of_range(self, bad_interval: int) -> None:
        """Poll interval must be 5–300 seconds."""
        with pytest.raises(Exception):
            SentinelSettings(poll_interval=bad_interval, _env_file=None)  # type: ignore[call-arg]

    def test_circuit_breaker_threshold_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            SentinelSettings(circuit_breaker_threshold=0, _env_file=None)  # type: ignore[call-arg]

    def test_circuit_breaker_window_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            SentinelSettings(circuit_breaker_window_minutes=0, _env_file=None)  # type: ignore[call-arg]


# ═════════════════════════════════════════════════════════
# MatchConfig (regex validation)
# ═════════════════════════════════════════════════════════
class TestMatchConfig:
    """Test container matching pattern validation."""

    def test_valid_regex(self) -> None:
        config = MatchConfig(container_name_pattern=r"^web-\d+$")
        assert config.container_name_pattern == r"^web-\d+$"

    def test_default_pattern_matches_all(self) -> None:
        config = MatchConfig()
        assert config.container_name_pattern == ".*"
        assert config.exclude_patterns == []

    @pytest.mark.parametrize("bad_regex", ["[invalid", "(unclosed", "***", "+"])
    def test_invalid_regex_rejected(self, bad_regex: str) -> None:
        """Broken regex must fail at validation time, not at runtime."""
        with pytest.raises(Exception):
            MatchConfig(container_name_pattern=bad_regex)

    def test_exclude_patterns_valid(self) -> None:
        config = MatchConfig(exclude_patterns=[r"^sentinel$", r"^traefik.*"])
        assert len(config.exclude_patterns) == 2

    @pytest.mark.parametrize("bad_regex", ["[broken", "(oops"])
    def test_invalid_exclude_regex_rejected(self, bad_regex: str) -> None:
        with pytest.raises(Exception):
            MatchConfig(exclude_patterns=[bad_regex])

    def test_mixed_valid_invalid_excludes_rejected(self) -> None:
        """Even one bad pattern in the list should fail the whole config."""
        with pytest.raises(Exception):
            MatchConfig(exclude_patterns=[r"^ok$", "[broken"])


# ═════════════════════════════════════════════════════════
# ConditionConfig (metric + operator + threshold)
# ═════════════════════════════════════════════════════════
class TestConditionConfig:
    """Test rule condition validation."""

    @pytest.mark.parametrize(
        "metric",
        ["cpu_percent", "memory_percent", "memory_usage_mb", "health_status"],
    )
    def test_all_valid_metrics_accepted(self, metric: str) -> None:
        config = ConditionConfig(
            metric=metric, operator=ConditionOperator.GT, threshold=50.0,
        )
        assert config.metric == metric

    @pytest.mark.parametrize(
        "bad_metric",
        ["disk_io", "network_rx", "cpu", "mem", "CPU_PERCENT", ""],
    )
    def test_unknown_metric_rejected(self, bad_metric: str) -> None:
        """Only the 4 documented metrics are accepted (case-sensitive)."""
        with pytest.raises(Exception):
            ConditionConfig(
                metric=bad_metric, operator=ConditionOperator.GT, threshold=90.0,
            )

    def test_invalid_operator_rejected(self) -> None:
        with pytest.raises(Exception):
            ConditionConfig(metric="cpu_percent", operator="!=", threshold=50.0)  # type: ignore[arg-type]

    def test_sustained_seconds_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            ConditionConfig(
                metric="cpu_percent", operator=ConditionOperator.GT,
                threshold=90.0, sustained_seconds=-1,
            )

    def test_string_threshold_for_health(self) -> None:
        """health_status conditions use string thresholds."""
        config = ConditionConfig(
            metric="health_status", operator=ConditionOperator.EQ,
            threshold="unhealthy",
        )
        assert config.threshold == "unhealthy"


# ═════════════════════════════════════════════════════════
# ActionConfig (model_validator cross-field)
# ═════════════════════════════════════════════════════════
class TestActionConfig:
    """Test action configuration cross-field validation."""

    def test_restart_valid(self) -> None:
        config = ActionConfig(type=ActionType.RESTART)
        assert config.timeout == 30

    def test_stop_valid(self) -> None:
        config = ActionConfig(type=ActionType.STOP)
        assert config.type == ActionType.STOP

    def test_exec_without_command_rejected(self) -> None:
        """exec type MUST have a command — this is a Fail Fast check."""
        with pytest.raises(Exception, match="command"):
            ActionConfig(type=ActionType.EXEC)

    def test_exec_with_command_valid(self) -> None:
        config = ActionConfig(type=ActionType.EXEC, command="/usr/local/bin/cleanup.sh")
        assert config.command == "/usr/local/bin/cleanup.sh"

    def test_scale_without_replicas_rejected(self) -> None:
        """scale type MUST have replicas — this is a Fail Fast check."""
        with pytest.raises(Exception, match="replicas"):
            ActionConfig(type=ActionType.SCALE)

    def test_scale_with_replicas_valid(self) -> None:
        config = ActionConfig(type=ActionType.SCALE, replicas=3)
        assert config.replicas == 3

    @pytest.mark.parametrize("bad_timeout", [0, 1, 4, 301, -10])
    def test_timeout_out_of_range_rejected(self, bad_timeout: int) -> None:
        """Timeout must be 5–300 seconds."""
        with pytest.raises(Exception):
            ActionConfig(type=ActionType.RESTART, timeout=bad_timeout)

    def test_timeout_boundaries_accepted(self) -> None:
        assert ActionConfig(type=ActionType.RESTART, timeout=5).timeout == 5
        assert ActionConfig(type=ActionType.RESTART, timeout=300).timeout == 300

    def test_invalid_action_type_rejected(self) -> None:
        with pytest.raises(Exception):
            ActionConfig(type="kill")  # type: ignore[arg-type]


# ═════════════════════════════════════════════════════════
# RuleConfig (name length, required fields)
# ═════════════════════════════════════════════════════════
class TestRuleConfig:
    """Test full rule-level validation."""

    def test_empty_name_rejected(self) -> None:
        """Rule name must have min_length=1."""
        with pytest.raises(Exception):
            RuleConfig(
                name="",
                match=MatchConfig(),
                condition=ConditionConfig(
                    metric="cpu_percent", operator=ConditionOperator.GT, threshold=80,
                ),
                action=ActionConfig(type=ActionType.RESTART),
            )

    def test_name_too_long_rejected(self) -> None:
        """Rule name must have max_length=200."""
        with pytest.raises(Exception):
            RuleConfig(
                name="A" * 201,
                match=MatchConfig(),
                condition=ConditionConfig(
                    metric="cpu_percent", operator=ConditionOperator.GT, threshold=80,
                ),
                action=ActionConfig(type=ActionType.RESTART),
            )

    def test_missing_condition_rejected(self) -> None:
        """condition is a required field."""
        with pytest.raises(Exception):
            RuleConfig(
                name="Bad Rule",
                match=MatchConfig(),
                action=ActionConfig(type=ActionType.RESTART),
            )  # type: ignore[call-arg]

    def test_missing_action_rejected(self) -> None:
        """action is a required field."""
        with pytest.raises(Exception):
            RuleConfig(
                name="Bad Rule",
                match=MatchConfig(),
                condition=ConditionConfig(
                    metric="cpu_percent", operator=ConditionOperator.GT, threshold=80,
                ),
            )  # type: ignore[call-arg]

    def test_missing_match_rejected(self) -> None:
        """match is a required field."""
        with pytest.raises(Exception):
            RuleConfig(
                name="Bad Rule",
                condition=ConditionConfig(
                    metric="cpu_percent", operator=ConditionOperator.GT, threshold=80,
                ),
                action=ActionConfig(type=ActionType.RESTART),
            )  # type: ignore[call-arg]

    def test_notify_defaults_to_console_warning(self) -> None:
        rule = RuleConfig(
            name="Test",
            match=MatchConfig(),
            condition=ConditionConfig(
                metric="cpu_percent", operator=ConditionOperator.GT, threshold=80,
            ),
            action=ActionConfig(type=ActionType.RESTART),
        )
        assert rule.notify.channels == ["console"]
        assert rule.notify.severity == Severity.WARNING

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(Exception):
            NotifyConfig(severity="panic")  # type: ignore[arg-type]


# ═════════════════════════════════════════════════════════
# GlobalConfig
# ═════════════════════════════════════════════════════════
class TestGlobalConfig:
    """Test global YAML settings validation."""

    @pytest.mark.parametrize("bad_interval", [0, 4, 301])
    def test_poll_interval_out_of_range(self, bad_interval: int) -> None:
        with pytest.raises(Exception):
            GlobalConfig(poll_interval=bad_interval)

    def test_invalid_default_severity(self) -> None:
        with pytest.raises(Exception):
            GlobalConfig(default_severity="emergency")  # type: ignore[arg-type]

    def test_defaults(self) -> None:
        g = GlobalConfig()
        assert g.poll_interval == 15
        assert g.default_severity == Severity.WARNING


# ═════════════════════════════════════════════════════════
# load_rules() — YAML Fail Fast Scenarios
# ═════════════════════════════════════════════════════════
class TestLoadRulesFailFast:
    """Test that malformed rules.yaml files raise ConfigurationError.

    Each test writes a specific broken YAML to tmp_path and verifies
    the daemon would refuse to start.
    """

    def _write_yaml(self, tmp_path: Path, content: str) -> str:
        path = tmp_path / "rules.yaml"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_valid_file(self, sample_rules_yaml: str) -> None:
        rules = load_rules(sample_rules_yaml)
        assert len(rules.rules) == 1
        assert rules.rules[0].name == "Test CPU Rule"
        assert rules.global_config.poll_interval == 10

    def test_missing_file(self) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_rules("/nonexistent/path/rules.yaml")

    def test_empty_file(self, tmp_path: Path) -> None:
        """An empty file parses to None, which is not a dict."""
        with pytest.raises(ConfigurationError, match="mapping"):
            load_rules(self._write_yaml(tmp_path, ""))

    def test_broken_yaml_syntax(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, "invalid:\nyaml: [broken"))

    def test_yaml_is_a_list(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="mapping"):
            load_rules(self._write_yaml(tmp_path, "- a\n- b\n"))

    def test_yaml_is_a_scalar(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="mapping"):
            load_rules(self._write_yaml(tmp_path, "just a string\n"))

    def test_empty_rules_list(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, "global:\n  poll_interval: 10\nrules: []\n"))

    def test_rules_key_missing(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, "global:\n  poll_interval: 10\n"))

    def test_rule_missing_name(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: restart
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_metric(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Metric"
    match:
      container_name_pattern: ".*"
    condition:
      metric: disk_throughput
      operator: ">"
      threshold: 80
    action:
      type: restart
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_operator(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Operator"
    match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: "!="
      threshold: 80
    action:
      type: restart
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_action_type(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Action"
    match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: kill
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_exec_without_command(self, tmp_path: Path) -> None:
        """exec action missing command field must fail at YAML load time."""
        yaml = """\
rules:
  - name: "Exec No Cmd"
    match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: exec
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_scale_without_replicas(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Scale No Replicas"
    match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: scale
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_regex_in_yaml(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Regex"
    match:
      container_name_pattern: "[invalid"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: restart
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_exclude_regex_in_yaml(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Exclude"
    match:
      container_name_pattern: ".*"
      exclude_patterns:
        - "(unclosed"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: restart
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_rule_invalid_severity(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Bad Severity"
    match:
      container_name_pattern: ".*"
    condition:
      metric: cpu_percent
      operator: ">"
      threshold: 80
    action:
      type: restart
    notify:
      severity: emergency
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_global_poll_interval_too_low(self, tmp_path: Path) -> None:
        yaml = """\
global:
  poll_interval: 1
rules:
  - name: "Valid"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80 }
    action: { type: restart }
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_global_invalid_severity(self, tmp_path: Path) -> None:
        yaml = """\
global:
  default_severity: fatal
rules:
  - name: "Valid"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80 }
    action: { type: restart }
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_action_timeout_too_low(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Low Timeout"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80 }
    action: { type: restart, timeout: 1 }
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_sustained_seconds_negative_in_yaml(self, tmp_path: Path) -> None:
        yaml = """\
rules:
  - name: "Negative Sustained"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80, sustained_seconds: -5 }
    action: { type: restart }
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))

    def test_exclude_patterns_preserved(self, sample_rules_yaml: str) -> None:
        rules = load_rules(sample_rules_yaml)
        assert "^sentinel$" in rules.rules[0].match.exclude_patterns

    def test_multiple_rules_valid(self, tmp_path: Path) -> None:
        """File with multiple valid rules should parse all of them."""
        yaml = """\
rules:
  - name: "Rule A"
    match: { container_name_pattern: "^web.*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 90 }
    action: { type: restart }
  - name: "Rule B"
    match: { container_name_pattern: "^api.*" }
    condition: { metric: memory_percent, operator: ">", threshold: 85 }
    action: { type: stop }
"""
        rules = load_rules(self._write_yaml(tmp_path, yaml))
        assert len(rules.rules) == 2
        assert rules.rules[0].name == "Rule A"
        assert rules.rules[1].name == "Rule B"

    def test_one_bad_rule_rejects_entire_file(self, tmp_path: Path) -> None:
        """One invalid rule among valid ones must reject the whole file."""
        yaml = """\
rules:
  - name: "Good Rule"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80 }
    action: { type: restart }
  - name: "Bad Rule"
    match: { container_name_pattern: ".*" }
    condition: { metric: cpu_percent, operator: ">", threshold: 80 }
    action: { type: exec }
"""
        with pytest.raises(ConfigurationError):
            load_rules(self._write_yaml(tmp_path, yaml))
