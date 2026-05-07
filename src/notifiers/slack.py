"""
Sentinel - Slack Notifier

Sends rich Block Kit notifications to a Slack channel via incoming webhook.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiohttp

from src.core.exceptions import NotifierError
from src.core.logger import get_logger
from src.notifiers.base import BaseNotifier

logger = get_logger()

_SEVERITY_EMOJI = {
    "info": ":information_source:",
    "warning": ":warning:",
    "critical": ":rotating_light:",
}


class SlackNotifier(BaseNotifier):
    """Send alerts to Slack via incoming webhook with Block Kit formatting."""

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url:
            raise ValueError("Slack webhook URL is required")
        self._webhook_url = webhook_url

    @property
    def channel_name(self) -> str:
        return "slack"

    async def send(
        self,
        title: str,
        message: str,
        severity: str = "info",
        container_name: str = "",
        **kwargs: object,
    ) -> None:
        """Send a Slack Block Kit notification."""
        emoji = _SEVERITY_EMOJI.get(severity, ":information_source:")
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {title}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*Severity:* {severity.upper()} | "
                            f"*Container:* {container_name or 'N/A'} | "
                            f"*Time:* {timestamp}"
                        ),
                    },
                ],
            },
            {"type": "divider"},
        ]

        payload = {
            "text": f"{emoji} {title}: {message[:200]}",  # Fallback text
            "blocks": blocks,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise NotifierError(
                            f"Slack webhook returned {resp.status}: {body}"
                        )

            logger.debug(
                f"Slack notification sent: {title}",
                component="notifiers.slack",
            )
        except NotifierError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to send Slack notification: {e}",
                component="notifiers.slack",
            )
            raise NotifierError(f"Slack notification failed: {e}") from e
