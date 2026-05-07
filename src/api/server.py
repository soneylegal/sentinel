"""
Sentinel - API Server (FastAPI + Uvicorn)

Runs the observability API as a background asyncio task alongside
the main monitoring loop. Uses Uvicorn's programmatic API to avoid
spawning a separate process.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import app_state, router
from src.core.logger import get_logger

if TYPE_CHECKING:
    from src.collectors.docker_async import DockerAsyncCollector
    from src.engine.state_manager import StateManager

logger = get_logger()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Sentinel - Observability API",
        description=(
            "Internal API for monitoring the Sentinel daemon's health, "
            "viewing intervention history, and managing circuit breakers."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow local tooling (Grafana, dashboards, etc.)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    return app


class APIServer:
    """Manages the lifecycle of the embedded Uvicorn server.

    The server runs as an asyncio task within the same event loop
    as the monitoring engine, sharing state without IPC overhead.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9120) -> None:
        self._host = host
        self._port = port
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def inject_dependencies(
        self,
        collector: DockerAsyncCollector,
        state_manager: StateManager,
    ) -> None:
        """Inject shared daemon components into the API routes."""
        app_state.collector = collector
        app_state.state_manager = state_manager

    async def start(self) -> None:
        """Start the Uvicorn server as a background asyncio task."""
        app = create_app()

        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )

        self._server = uvicorn.Server(config)

        # Run in background task so it doesn't block the event loop
        self._task = asyncio.create_task(self._server.serve())

        logger.info(
            f"Observability API started on http://{self._host}:{self._port}",
            component="api.server",
        )

    async def stop(self) -> None:
        """Gracefully shut down the Uvicorn server."""
        if self._server:
            self._server.should_exit = True

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()

        logger.info("Observability API stopped", component="api.server")
