"""
Sentinel - Discord Notifier

Sends rich embed notifications to a Discord channel via webhook.
Uses aiohttp for non-blocking HTTP requests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiohttp

from src.core.exceptions import NotifierError
from src.core.logger import get_logger
from src.notifiers.base import BaseNotifier

logger = get_logger()

# Severity → Discord embed color (decimal)
_SEVERITY_COLORS = {
    "info": 0x3498DB,  # Blue
    "warning": 0xF39C12,  # Amber
    "critical": 0xE74C3C,  # Red
}

_SEVERITY_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
}


class DiscordNotifier(BaseNotifier):
    """Send alerts to Discord via webhook with rich embeds."""

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url:
            raise ValueError("Discord webhook URL is required")
        self._webhook_url = webhook_url

    @property
    def channel_name(self) -> str:
        return "discord"

    async def send(
        self,
        title: str,
        message: str,
        severity: str = "info",
        container_name: str = "",
        **kwargs: object,
    ) -> None:
        """Send a Discord embed notification."""
        emoji = _SEVERITY_EMOJI.get(severity, "ℹ️")
        color = _SEVERITY_COLORS.get(severity, 0x3498DB)

        embed = {
            "title": f"{emoji} {title}",
            "description": message,
            "color": color,
            "timestamp": datetime.now(UTC).isoformat(),
            "footer": {
                "text": f"Sentinel • {container_name}" if container_name else "Sentinel",
            },
            "fields": [
                {
                    "name": "Severity",
                    "value": severity.upper(),
                    "inline": True,
                },
                {
                    "name": "Container",
                    "value": container_name or "N/A",
                    "inline": True,
                },
            ],
        }

        payload = {
            "username": "Sentinel",
            "embeds": [embed],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        raise NotifierError(f"Discord webhook returned {resp.status}: {body}")

            logger.debug(
                f"Discord notification sent: {title}",
                component="notifiers.discord",
            )
        except NotifierError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to send Discord notification: {e}",
                component="notifiers.discord",
            )
            raise NotifierError(f"Discord notification failed: {e}") from e
