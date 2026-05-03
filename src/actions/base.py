"""
Sentinel - Base Action Interface (Strategy Pattern)

All autonomous actions inherit from BaseAction, allowing the engine
to execute any action without knowing its implementation details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import aiodocker


class BaseAction(ABC):
    """Abstract base class for all autonomous actions.

    Each concrete action (Restart, Stop, Scale, Exec) implements
    the `execute` method. The engine calls it polymorphically.
    """

    def __init__(self, docker_client: aiodocker.Docker) -> None:
        self._docker = docker_client

    @property
    @abstractmethod
    def action_type(self) -> str:
        """Return the canonical name of this action (e.g., 'restart')."""
        ...

    @abstractmethod
    async def execute(
        self,
        container_id: str,
        container_name: str,
        timeout: int = 30,
        **kwargs: object,
    ) -> None:
        """Execute the autonomous action on the target container.

        Args:
            container_id: Short (12-char) Docker container ID.
            container_name: Human-readable container name.
            timeout: Seconds to wait for graceful completion.
            **kwargs: Additional action-specific parameters.

        Raises:
            ActionExecutionError: If the action fails.
        """
        ...

    async def _get_container(self, container_id: str) -> aiodocker.docker.DockerContainer:
        """Resolve a container by its ID via the Docker API."""
        return await self._docker.containers.get(container_id)
