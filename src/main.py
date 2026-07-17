"""
Sentinel - Main Orchestrator

The entry point for the Sentinel daemon. Bootstraps all components
and runs three concurrent asyncio tasks:

  1. Metrics Collector (polling loop)
  2. Rules Engine (evaluation on each poll cycle)
  3. Observability API (FastAPI + Uvicorn)

Handles graceful shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from src.actions.base import BaseAction
from src.actions.restart import RestartAction, StopAction
from src.actions.start import StartAction
from src.actions.scale import ScaleComposeAction
from src.api.server import APIServer
from src.collectors.docker_async import DockerAsyncCollector
from src.core.config import SentinelSettings, load_rules, load_settings
from src.core.exceptions import ConfigurationError, DockerConnectionError
from src.core.logger import get_logger, setup_logger
from src.engine.rules import RulesEngine
from src.engine.state_manager import StateManager
from src.notifiers.base import BaseNotifier, ConsoleNotifier
from src.notifiers.discord import DiscordNotifier
from src.notifiers.slack import SlackNotifier

logger = get_logger()


class SentinelDaemon:
    """Main daemon orchestrator.

    Manages the lifecycle of all components and runs the
    monitoring loop until interrupted.
    """

    def __init__(self) -> None:
        self._settings: SentinelSettings | None = None
        self._collector: DockerAsyncCollector | None = None
        self._state_manager: StateManager | None = None
        self._engine: RulesEngine | None = None
        self._api_server: APIServer | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Bootstrap all components and start the daemon."""
        self._print_banner()

        # ── 1. Load Configuration (Fail Fast) ──
        try:
            self._settings = load_settings()
            setup_logger(
                level=self._settings.log_level,
                fmt=self._settings.log_format,
            )
            logger.info("Configuration loaded successfully", component="main")

            rules_file = load_rules(self._settings.rules_path)
            logger.info(
                f"Rules loaded: {len(rules_file.rules)} rules "
                f"({sum(1 for r in rules_file.rules if r.enabled)} enabled)",
                component="main",
            )
        except ConfigurationError as e:
            logger.critical(f"FATAL: {e}", component="main")
            sys.exit(1)

        # ── 2. Initialize Docker Collector ──
        self._collector = DockerAsyncCollector(docker_url=self._settings.docker_url)
        try:
            await self._collector.connect()
        except DockerConnectionError as e:
            logger.critical(f"FATAL: {e}", component="main")
            sys.exit(1)

        # ── 3. Initialize State Manager (SQLite) ──
        self._state_manager = StateManager(
            db_path=self._settings.db_path,
            threshold=self._settings.circuit_breaker_threshold,
            window_minutes=self._settings.circuit_breaker_window_minutes,
        )
        await self._state_manager.initialize()

        # ── 4. Build Action Strategies ──
        assert self._collector._client is not None
        docker_client = self._collector._client
        actions: dict[str, BaseAction] = {
            "restart": RestartAction(docker_client),
            "stop": StopAction(docker_client),
            "start": StartAction(docker_client),
            "scale": ScaleComposeAction(docker_client),
        }

        # ── 5. Build Notifier Strategies ──
        notifiers: dict[str, BaseNotifier] = {
            "console": ConsoleNotifier(),
        }

        if self._settings.discord_webhook_url:
            notifiers["discord"] = DiscordNotifier(self._settings.discord_webhook_url)
            logger.info("Discord notifier enabled", component="main")

        if self._settings.slack_webhook_url:
            notifiers["slack"] = SlackNotifier(self._settings.slack_webhook_url)
            logger.info("Slack notifier enabled", component="main")

        # ── 6. Initialize Rules Engine ──
        self._engine = RulesEngine(
            rules=rules_file.rules,
            state_manager=self._state_manager,
            actions=actions,
            notifiers=notifiers,
            collector=self._collector,
        )

        # ── 7. Start API Server ──
        self._api_server = APIServer(
            host=self._settings.api_host,
            port=self._settings.api_port,
        )
        self._api_server.inject_dependencies(
            collector=self._collector,
            state_manager=self._state_manager,
        )
        await self._api_server.start()

        # ── 8. Register Signal Handlers ──
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # ── 9. Run Monitoring Loop ──
        logger.info(
            f"Sentinel is operational — polling every {self._settings.poll_interval}s",
            component="main",
        )
        await self._monitoring_loop()

    async def _monitoring_loop(self) -> None:
        """Main polling loop: collect metrics → evaluate rules → sleep."""
        assert self._settings is not None
        assert self._collector is not None
        assert self._engine is not None

        while not self._shutdown_event.is_set():
            try:
                # Collect metrics from all running containers
                metrics = await self._collector.collect_all()

                if metrics:
                    # Evaluate rules against collected metrics
                    await self._engine.evaluate(metrics)
                else:
                    logger.debug(
                        "No running containers found in this cycle",
                        component="main",
                    )

                # Collect and evaluate exited containers
                exited = await self._collector.collect_exited()
                if exited:
                    await self._engine.evaluate_exited(exited)

            except DockerConnectionError as e:
                logger.error(
                    f"Docker connection lost: {e}. Attempting reconnect...",
                    component="main",
                )
                try:
                    await self._collector.connect()
                except DockerConnectionError:
                    logger.error(
                        "Reconnection failed. Will retry next cycle.",
                        component="main",
                    )

            except Exception as e:
                logger.error(
                    f"Unexpected error in monitoring loop: {e}",
                    component="main",
                    exc_info=True,
                )

            # Wait for the next cycle or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._settings.poll_interval,
                )
            except TimeoutError:
                pass  # Normal: timeout means it's time for the next cycle

    def _handle_shutdown(self) -> None:
        """Signal handler for graceful shutdown."""
        logger.warning("Shutdown signal received — stopping gracefully...", component="main")
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Clean up all resources."""
        logger.info("Shutting down components...", component="main")

        if self._api_server:
            await self._api_server.stop()

        if self._collector:
            await self._collector.disconnect()

        if self._state_manager:
            await self._state_manager.close()

        logger.info("Sentinel stopped. Goodbye.", component="main")

    @staticmethod
    def _print_banner() -> None:
        """Print the startup banner."""
        banner = r"""
  ____            _   _            _
 / ___|  ___ _ __ | |_(_)_ __   ___| |
 \___ \ / _ \ '_ \| __| | '_ \ / _ \ |
  ___) |  __/ | | | |_| | | | |  __/ |
 |____/ \___|_| |_|\__|_|_| |_|\___|_|

  Docker Autonomous Orchestrator & Monitor
  ─────────────────────────────────────────
"""
        print(banner)


async def main() -> None:
    """Application entry point."""
    daemon = SentinelDaemon()
    try:
        await daemon.start()
    finally:
        await daemon.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
