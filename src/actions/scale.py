"""
Sentinel - Scale Action (docker-compose integration)

Scales a service up/down using the docker-compose CLI.
This action shells out to `docker compose` since aiodocker doesn't
support Compose-level orchestration natively.
"""

from __future__ import annotations

import asyncio

from src.actions.base import BaseAction
from src.core.exceptions import ActionExecutionError
from src.core.logger import get_logger

logger = get_logger()


class ScaleComposeAction(BaseAction):
    """Scale a docker-compose service to a specified number of replicas."""

    @property
    def action_type(self) -> str:
        return "scale"

    async def execute(
        self,
        container_id: str,
        container_name: str,
        timeout: int = 30,
        **kwargs: object,
    ) -> None:
        """Scale the service using `docker compose`.

        Expects kwargs:
            replicas (int): Target number of replicas.
        """
        replicas = kwargs.get("replicas", 1)
        service_name = self._infer_service_name(container_name)

        logger.warning(
            f"Scaling service '{service_name}' to {replicas} replicas",
            component="actions.scale",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "up",
                "-d",
                "--scale",
                f"{service_name}={replicas}",
                "--no-recreate",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            if proc.returncode != 0:
                error_output = stderr.decode().strip()
                raise ActionExecutionError(
                    f"docker compose scale failed (rc={proc.returncode}): {error_output}"
                )

            logger.info(
                f"Service '{service_name}' scaled to {replicas} replicas",
                component="actions.scale",
            )

        except TimeoutError:
            raise ActionExecutionError(
                f"Scaling service '{service_name}' timed out after {timeout}s"
            )
        except ActionExecutionError:
            raise
        except Exception as e:
            raise ActionExecutionError(f"Failed to scale service '{service_name}': {e}") from e

    @staticmethod
    def _infer_service_name(container_name: str) -> str:
        """Infer the compose service name from a container name.

        Docker Compose naming: <project>-<service>-<replica_num>
        or: <project>_<service>_<replica_num> (v1)
        """
        # Try v2 format first (dash-separated)
        parts = container_name.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            # Remove the project prefix too
            service_parts = parts[0].split("-", 1)
            return service_parts[-1] if len(service_parts) > 1 else parts[0]

        # Try v1 format (underscore-separated)
        parts = container_name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            service_parts = parts[0].split("_", 1)
            return service_parts[-1] if len(service_parts) > 1 else parts[0]

        # Fallback: use the full name
        return container_name
