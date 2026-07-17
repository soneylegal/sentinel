"""
Sentinel - State Manager & Circuit Breaker (SQLite + aiosqlite)

Persists intervention history to prevent Crash Loop BackOff scenarios.
Before any autonomous action, the engine queries this module:
  "Have I already restarted this container N times in the last M minutes?"

If the threshold is exceeded, the circuit breaker trips and the action
is suppressed — escalating to human notification instead.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.exceptions import CircuitBreakerOpen
from src.core.logger import get_logger

logger = get_logger()

# ─────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intervention_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id   TEXT NOT NULL,
    container_name TEXT NOT NULL,
    rule_name      TEXT NOT NULL,
    action_type    TEXT NOT NULL,
    success        BOOLEAN NOT NULL DEFAULT 1,
    error_message  TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_history_container_time
    ON intervention_history (container_name, created_at);

CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    container_name TEXT PRIMARY KEY,
    trip_count     INTEGER NOT NULL DEFAULT 0,
    last_tripped   TEXT,
    is_open        BOOLEAN NOT NULL DEFAULT 0
);
"""


class StateManager:
    """Async SQLite-backed state manager with circuit breaker logic.

    Attributes:
        threshold: Max interventions before the breaker trips.
        window_minutes: Time window (in minutes) for counting interventions.
    """

    def __init__(
        self,
        db_path: str = "db/sentinel.db",
        threshold: int = 3,
        window_minutes: int = 5,
    ) -> None:
        self._db_path = db_path
        self._threshold = threshold
        self._window_minutes = window_minutes
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create database directory, connect, and ensure schema exists."""
        db_dir = Path(self._db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()

        logger.info(
            "State manager initialized",
            db_path=self._db_path,
            threshold=self._threshold,
            window_minutes=self._window_minutes,
            component="engine.state_manager",
        )

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def check_circuit_breaker(self, container_name: str) -> None:
        """Check if the circuit breaker is open for a container.

        Two-phase check:
        1. If the breaker has already been latched open (is_open=1),
           it stays open until manually reset via the API.
        2. Otherwise, count recent interventions in the sliding window
           to detect new crash loops.

        Raises:
            CircuitBreakerOpen: If the container has exceeded the restart
                threshold within the configured time window, or if the
                breaker was previously latched open.
        """
        assert self._db is not None, "StateManager not initialized"

        async with self._lock:
            # Phase 1: Check if the breaker is already latched open.
            # Once tripped, it stays open until a human calls
            # reset_circuit_breaker() via the API.
            cursor = await self._db.execute(
                "SELECT is_open FROM circuit_breaker_state WHERE container_name = ?",
                (container_name,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                raise CircuitBreakerOpen(
                    container_name=container_name,
                    restart_count=self._threshold,
                    window_minutes=self._window_minutes,
                )

            # Phase 2: Count recent interventions in the sliding window
            # to detect the FIRST crash loop occurrence.
            cursor = await self._db.execute(
                """
                SELECT COUNT(*) as cnt
                FROM intervention_history
                WHERE container_name = ?
                  AND created_at > datetime('now', ?)
                  AND action_type IN ('restart', 'stop', 'start')
                """,
                (container_name, f"-{self._window_minutes} minutes"),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0

            if count >= self._threshold:
                # Latch the breaker open — it will NOT auto-close
                # when the sliding window moves past.
                await self._db.execute(
                    """
                    INSERT INTO circuit_breaker_state
                        (container_name, trip_count, last_tripped, is_open)
                    VALUES (?, ?, datetime('now'), 1)
                    ON CONFLICT(container_name) DO UPDATE SET
                        trip_count = trip_count + 1,
                        last_tripped = datetime('now'),
                        is_open = 1
                    """,
                    (container_name, count),
                )
                await self._db.commit()

                raise CircuitBreakerOpen(
                    container_name=container_name,
                    restart_count=count,
                    window_minutes=self._window_minutes,
                )

    async def record_intervention(
        self,
        container_id: str,
        container_name: str,
        rule_name: str,
        action_type: str,
        success: bool = True,
        error_message: str | None = None,
    ) -> int:
        """Record an autonomous intervention in the history table.

        Returns:
            The row ID of the inserted record.
        """
        assert self._db is not None, "StateManager not initialized"

        async with self._lock:
            cursor = await self._db.execute(
                """
                INSERT INTO intervention_history
                    (container_id, container_name, rule_name, action_type, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (container_id, container_name, rule_name, action_type, success, error_message),
            )
            await self._db.commit()
            row_id = cursor.lastrowid

        logger.info(
            f"Intervention recorded: {action_type} on {container_name} "
            f"(rule={rule_name}, success={success})",
            component="engine.state_manager",
        )
        return row_id or 0

    async def get_recent_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve the most recent intervention records.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of intervention records as dictionaries.
        """
        assert self._db is not None, "StateManager not initialized"

        cursor = await self._db.execute(
            """
            SELECT id, container_id, container_name, rule_name,
                   action_type, success, error_message, created_at
            FROM intervention_history
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "container_id": row[1],
                "container_name": row[2],
                "rule_name": row[3],
                "action_type": row[4],
                "success": bool(row[5]),
                "error_message": row[6],
                "created_at": row[7],
            }
            for row in rows
        ]

    async def reset_circuit_breaker(self, container_name: str) -> None:
        """Manually reset the circuit breaker for a container."""
        assert self._db is not None, "StateManager not initialized"

        async with self._lock:
            await self._db.execute(
                """
                UPDATE circuit_breaker_state
                SET is_open = 0, trip_count = 0
                WHERE container_name = ?
                """,
                (container_name,),
            )
            await self._db.commit()

        logger.info(
            f"Circuit breaker reset for '{container_name}'",
            component="engine.state_manager",
        )

    async def get_circuit_breaker_status(self) -> list[dict[str, Any]]:
        """Get the current state of all circuit breakers."""
        assert self._db is not None, "StateManager not initialized"

        cursor = await self._db.execute(
            "SELECT container_name, trip_count, last_tripped, is_open FROM circuit_breaker_state"
        )
        rows = await cursor.fetchall()

        return [
            {
                "container_name": row[0],
                "trip_count": row[1],
                "last_tripped": row[2],
                "is_open": bool(row[3]),
            }
            for row in rows
        ]
