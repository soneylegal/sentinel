"""
Sentinel - Custom Exceptions
Fail-fast exceptions for configuration errors, Docker connectivity,
and circuit breaker activation.
"""

from __future__ import annotations


class SentinelError(Exception):
    """Base exception for all Sentinel errors."""


class ConfigurationError(SentinelError):
    """Raised when rules.yaml or .env contains invalid configuration.

    This is a FATAL error — the daemon must not start with a broken config.
    """


class DockerConnectionError(SentinelError):
    """Raised when the Docker daemon is unreachable via the configured socket."""


class CircuitBreakerOpen(SentinelError):
    """Raised when the circuit breaker has tripped for a specific container.

    This means the container has been restarted too many times within the
    configured window and autonomous action is suspended.
    """

    def __init__(self, container_name: str, restart_count: int, window_minutes: int) -> None:
        self.container_name = container_name
        self.restart_count = restart_count
        self.window_minutes = window_minutes
        super().__init__(
            f"Circuit breaker OPEN for '{container_name}': "
            f"{restart_count} restarts in the last {window_minutes} minutes. "
            f"Autonomous action suspended — human intervention required."
        )


class ActionExecutionError(SentinelError):
    """Raised when an action (restart, stop, scale) fails to execute."""


class NotifierError(SentinelError):
    """Raised when a notification channel fails to deliver a message."""
