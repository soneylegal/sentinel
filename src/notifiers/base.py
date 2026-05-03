"""
Sentinel - Base Notifier Interface (Strategy Pattern)

All notification channels implement this abstract interface.
The engine calls `notifier.send(alert)` without knowing whether
it's going to Discord, Slack, Telegram, or stdout.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """Abstract base class for all notification channels."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return the canonical name of this channel (e.g., 'discord')."""
        ...

    @abstractmethod
    async def send(
        self,
        title: str,
        message: str,
        severity: str = "info",
        container_name: str = "",
        **kwargs: object,
    ) -> None:
        """Send a notification.

        Args:
            title: Alert title/subject.
            message: Alert body with details.
            severity: 'info', 'warning', or 'critical'.
            container_name: Name of the affected container.
            **kwargs: Channel-specific parameters.

        Raises:
            NotifierError: If the notification fails to deliver.
        """
        ...


class ConsoleNotifier(BaseNotifier):
    """Writes notifications to stdout via Loguru.

    Always available — used as the fallback notifier.
    """

    @property
    def channel_name(self) -> str:
        return "console"

    async def send(
        self,
        title: str,
        message: str,
        severity: str = "info",
        container_name: str = "",
        **kwargs: object,
    ) -> None:
        """Log the notification to structured output."""
        from src.core.logger import get_logger

        logger = get_logger()

        severity_map = {
            "info": logger.info,
            "warning": logger.warning,
            "critical": logger.critical,
        }

        log_fn = severity_map.get(severity, logger.info)

        log_fn(
            f"[ALERT] {title}\n{message}",
            component="notifiers.console",
            severity=severity,
            container=container_name,
        )
