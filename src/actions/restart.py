"""
Sentinel - Restart Action

Gracefully restarts a container with a configurable timeout.
"""

from __future__ import annotations

from src.actions.base import BaseAction
from src.core.exceptions import ActionExecutionError
from src.core.logger import get_logger

logger = get_logger()


class RestartAction(BaseAction):
    """Restart a Docker container with graceful shutdown."""

    @property
    def action_type(self) -> str:
        return "restart"

    async def execute(
        self,
        container_id: str,
        container_name: str,
        timeout: int = 30,
        **kwargs: object,
    ) -> None:
        """Restart the target container.

        Sends SIGTERM, waits `timeout` seconds, then SIGKILL if needed.
        """
        try:
            container = await self._get_container(container_id)
            logger.warning(
                f"Restarting container '{container_name}' (id={container_id}, "
                f"timeout={timeout}s)",
                component="actions.restart",
            )
            await container.restart(timeout=timeout)
            logger.info(
                f"Container '{container_name}' restarted successfully",
                component="actions.restart",
            )
        except Exception as e:
            raise ActionExecutionError(
                f"Failed to restart container '{container_name}': {e}"
            ) from e


class StopAction(BaseAction):
    """Stop a Docker container gracefully."""

    @property
    def action_type(self) -> str:
        return "stop"

    async def execute(
        self,
        container_id: str,
        container_name: str,
        timeout: int = 30,
        **kwargs: object,
    ) -> None:
        """Stop the target container."""
        try:
            container = await self._get_container(container_id)
            logger.warning(
                f"Stopping container '{container_name}' (id={container_id}, "
                f"timeout={timeout}s)",
                component="actions.restart",
            )
            await container.stop(t=timeout)
            logger.info(
                f"Container '{container_name}' stopped successfully",
                component="actions.restart",
            )
        except Exception as e:
            raise ActionExecutionError(
                f"Failed to stop container '{container_name}': {e}"
            ) from e
