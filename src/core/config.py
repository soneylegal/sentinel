"""
Sentinel - Configuration Module (Pydantic Settings + YAML Validation)

Uses Pydantic v2 for strict validation of both environment variables
and the rules.yaml file. If anything is misconfigured, the daemon
refuses to start (Fail Fast principle).
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.exceptions import ConfigurationError


# ─────────────────────────────────────────────────────────
# Environment Settings (.env)
# ─────────────────────────────────────────────────────────
class SentinelSettings(BaseSettings):
    """Root settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Docker
    docker_url: str = "unix:///var/run/docker.sock"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=9120, ge=1024, le=65535)

    # Paths
    rules_path: str = "rules.yaml"
    db_path: str = "db/sentinel.db"

    # Polling
    poll_interval: int = Field(default=15, ge=5, le=300)

    # Circuit Breaker
    circuit_breaker_threshold: int = Field(default=3, ge=1, le=100)
    circuit_breaker_window_minutes: int = Field(default=5, ge=1, le=1440)

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # Notifiers (optional)
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got '{v}'")
        return v.upper()


# ─────────────────────────────────────────────────────────
# Rules YAML Schema
# ─────────────────────────────────────────────────────────
class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(str, Enum):
    RESTART = "restart"
    STOP = "stop"
    SCALE = "scale"
    EXEC = "exec"


class ConditionOperator(str, Enum):
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    EQ = "=="


class MatchConfig(BaseModel):
    """Container matching configuration with regex patterns."""
    container_name_pattern: str = ".*"
    exclude_patterns: list[str] = Field(default_factory=list)

    @field_validator("container_name_pattern")
    @classmethod
    def validate_regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{v}': {e}") from e
        return v

    @field_validator("exclude_patterns")
    @classmethod
    def validate_exclude_regexes(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"Invalid exclude regex '{pattern}': {e}") from e
        return v


class ConditionConfig(BaseModel):
    """Rule condition: metric + operator + threshold + sustained duration."""
    metric: str
    operator: ConditionOperator
    threshold: float | str  # str for health_status checks
    sustained_seconds: int = Field(default=0, ge=0)

    @field_validator("metric")
    @classmethod
    def validate_metric(cls, v: str) -> str:
        allowed = {"cpu_percent", "memory_percent", "memory_usage_mb", "health_status"}
        if v not in allowed:
            raise ValueError(f"metric must be one of {allowed}, got '{v}'")
        return v


class ActionConfig(BaseModel):
    """Action to execute when a rule condition is met."""
    type: ActionType
    timeout: int = Field(default=30, ge=5, le=300)
    command: str | None = None  # For exec actions
    replicas: int | None = None  # For scale actions

    @model_validator(mode="after")
    def validate_action_params(self) -> "ActionConfig":
        if self.type == ActionType.EXEC and not self.command:
            raise ValueError("Action type 'exec' requires a 'command' field")
        if self.type == ActionType.SCALE and self.replicas is None:
            raise ValueError("Action type 'scale' requires a 'replicas' field")
        return self


class NotifyConfig(BaseModel):
    """Notification channels and severity for a rule."""
    channels: list[str] = Field(default_factory=lambda: ["console"])
    severity: Severity = Severity.WARNING


class RuleConfig(BaseModel):
    """A single monitoring rule with match, condition, action, and notify."""
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    enabled: bool = True
    match: MatchConfig
    condition: ConditionConfig
    action: ActionConfig
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class GlobalConfig(BaseModel):
    """Global settings from rules.yaml."""
    poll_interval: int = Field(default=15, ge=5, le=300)
    default_severity: Severity = Severity.WARNING


class RulesFile(BaseModel):
    """Top-level schema for rules.yaml."""
    global_config: GlobalConfig = Field(default_factory=GlobalConfig, alias="global")
    rules: list[RuleConfig] = Field(min_length=1)

    model_config = {"populate_by_name": True}


# ─────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────
def load_rules(path: str | Path) -> RulesFile:
    """Parse and validate rules.yaml. Raises ConfigurationError on failure."""
    path = Path(path)

    if not path.exists():
        raise ConfigurationError(f"Rules file not found: {path.resolve()}")

    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigurationError(f"Rules file must be a YAML mapping, got {type(raw).__name__}")

    try:
        return RulesFile.model_validate(raw)
    except Exception as e:
        raise ConfigurationError(f"Rules validation failed: {e}") from e


def load_settings() -> SentinelSettings:
    """Load and validate environment settings."""
    try:
        return SentinelSettings()  # type: ignore[call-arg]
    except Exception as e:
        raise ConfigurationError(f"Environment configuration error: {e}") from e
