"""
Sentinel - Async Docker Metrics Collector (aiodocker)

Connects to the Docker daemon via the Unix socket, collects container
stats (CPU%, RAM%), and normalizes them into a platform-agnostic
dataclass regardless of whether the host is Linux, macOS, or WSL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiodocker

from src.core.exceptions import DockerConnectionError
from src.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True, slots=True)
class ContainerMetrics:
    """Normalized, platform-agnostic container metrics snapshot."""

    container_id: str
    container_name: str
    image: str
    status: str  # running, exited, paused, ...
    health_status: str  # healthy, unhealthy, none
    cpu_percent: float  # 0.0 - 100.0+
    memory_percent: float  # 0.0 - 100.0
    memory_usage_mb: float  # Current RSS in MiB
    memory_limit_mb: float  # Container memory limit in MiB
    pids: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class DockerAsyncCollector:
    """Asynchronous Docker metrics collector using aiodocker.

    Handles connection lifecycle, stats collection, and cross-platform
    normalization of CPU/memory metrics.
    """

    def __init__(self, docker_url: str = "unix:///var/run/docker.sock") -> None:
        self._docker_url = docker_url
        self._client: aiodocker.Docker | None = None

    async def connect(self) -> None:
        """Establish connection to the Docker daemon."""
        try:
            self._client = aiodocker.Docker(url=self._docker_url)
            # Verify connectivity
            await self._client.version()
            logger.info(
                "Connected to Docker daemon",
                url=self._docker_url,
                component="collectors.docker_async",
            )
        except Exception as e:
            raise DockerConnectionError(
                f"Failed to connect to Docker at {self._docker_url}: {e}"
            ) from e

    async def disconnect(self) -> None:
        """Gracefully close the Docker connection."""
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("Disconnected from Docker daemon", component="collectors.docker_async")

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._client is not None

    async def collect_all(self) -> list[ContainerMetrics]:
        """Collect metrics for all running containers.

        Returns:
            List of normalized ContainerMetrics dataclass instances.
        """
        if not self._client:
            raise DockerConnectionError("Not connected to Docker daemon. Call connect() first.")

        containers = await self._client.containers.list()
        if not containers:
            logger.debug("No running containers found", component="collectors.docker_async")
            return []

        tasks = [self._collect_single(container) for container in containers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        metrics: list[ContainerMetrics] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.warning(
                    f"Failed to collect metrics for a container: {result}",
                    component="collectors.docker_async",
                )
            elif result is not None:
                metrics.append(result)

        logger.debug(
            f"Collected metrics for {len(metrics)} containers",
            component="collectors.docker_async",
        )
        return metrics

    async def _collect_single(
        self,
        container: aiodocker.docker.DockerContainer,
    ) -> ContainerMetrics | None:
        """Collect and normalize metrics for a single container."""
        try:
            info = await container.show()
            stats = await self._get_stats_snapshot(container)

            if stats is None:
                return None

            name = info.get("Name", "").lstrip("/")
            image = info.get("Config", {}).get("Image", "unknown")
            status = info.get("State", {}).get("Status", "unknown")
            health = self._extract_health(info)

            cpu_percent = self._calculate_cpu_percent(stats)
            mem_usage, mem_limit = self._calculate_memory(stats)
            mem_percent = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0

            pids = stats.get("pids_stats", {}).get("current", 0) or 0

            return ContainerMetrics(
                container_id=container.id[:12],
                container_name=name,
                image=image,
                status=status,
                health_status=health,
                cpu_percent=round(cpu_percent, 2),
                memory_percent=round(mem_percent, 2),
                memory_usage_mb=round(mem_usage / (1024 * 1024), 2),
                memory_limit_mb=round(mem_limit / (1024 * 1024), 2),
                pids=pids,
            )
        except Exception as e:
            logger.error(
                f"Error collecting metrics for container {container.id[:12]}: {e}",
                component="collectors.docker_async",
            )
            return None

    async def _get_stats_snapshot(
        self,
        container: aiodocker.docker.DockerContainer,
    ) -> dict[str, Any] | None:
        """Get a single stats snapshot (non-streaming)."""
        try:
            stats_data = await container.stats(stream=False)

            # aiodocker returns a list when stream=False
            if isinstance(stats_data, list) and len(stats_data) > 0:
                result: dict[str, Any] = stats_data[0]
                return result
            elif isinstance(stats_data, dict):
                # Just in case it returns a dict directly in some versions
                result: dict[str, Any] = stats_data
                return result
        except Exception as e:
            logger.error(
                f"Error getting stats for container: {e}",
                component="collectors.docker_async",
            )
            return None
        return None

    @staticmethod
    def _calculate_cpu_percent(stats: dict[str, Any]) -> float:
        """Calculate CPU usage percentage, normalized across platforms.

        Uses the delta method: (container_delta / system_delta) * num_cpus * 100
        """
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})

        cpu_usage = cpu_stats.get("cpu_usage", {})
        precpu_usage = precpu_stats.get("cpu_usage", {})

        cpu_delta = cpu_usage.get("total_usage", 0) - precpu_usage.get("total_usage", 0)

        # Linux: system_cpu_usage is available
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get(
            "system_cpu_usage", 0
        )

        if system_delta > 0 and cpu_delta > 0:
            num_cpus = cpu_stats.get("online_cpus", 0)
            if num_cpus == 0:
                num_cpus = len(cpu_usage.get("percpu_usage", [])) or 1
            return float((cpu_delta / system_delta) * num_cpus * 100.0)

        return 0.0

    @staticmethod
    def _calculate_memory(stats: dict[str, Any]) -> tuple[float, float]:
        """Extract memory usage and limit in bytes.

        Handles both cgroup v1 and v2 memory accounting.
        """
        mem_stats = stats.get("memory_stats", {})
        limit = mem_stats.get("limit", 0)

        # cgroup v2: usage minus inactive_file from stats sub-dict
        usage = mem_stats.get("usage", 0)
        stats_detail = mem_stats.get("stats", {})

        # Subtract cache/inactive to get actual RSS
        cache = stats_detail.get("inactive_file", 0) or stats_detail.get("cache", 0)
        actual_usage = max(0, usage - cache)

        return float(actual_usage), float(limit)

    @staticmethod
    def _extract_health(info: dict[str, Any]) -> str:
        """Extract container health status. Returns 'none' if no healthcheck."""
        state = info.get("State", {})
        health = state.get("Health", {})
        return str(health.get("Status", "none"))
