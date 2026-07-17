"""
Sentinel - Start Action

Starts a stopped container. Unlike RestartAction (which restarts a running
container), StartAction is designed for containers in "exited" status.

Uses `docker start` which preserves the container's name, ID, networks,
and volumes — critical for reverse proxy routing (e.g., SWAG/Traefik).
"""

from __future__ import annotations

from src.actions.base import BaseAction
from src.core.exceptions import ActionExecutionError
from src.core.logger import get_logger

logger = get_logger()


class StartAction(BaseAction):
    """Start a stopped Docker container."""

    @property
    def action_type(self) -> str:
        return "start"

    async def execute(
        self,
        container_id: str,
        container_name: str,
        timeout: int = 30,
        **kwargs: object,
    ) -> None:
        """Start a stopped container.

        This is the correct action for exited containers. Using `restart`
        on a stopped container would also work, but `start` is semantically
        clearer and avoids an unnecessary stop signal.
        """
        try:
            container = await self._get_container(container_id)
            logger.warning(
                f"Starting exited container '{container_name}' (id={container_id})",
                component="actions.start",
            )
            await container.start()
            logger.info(
                f"Container '{container_name}' started successfully",
                component="actions.start",
            )
        except Exception as e:
            raise ActionExecutionError(
                f"Failed to start container '{container_name}': {e}"
            ) from e
