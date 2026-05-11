from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from src import storage
from src.notifier import Notifier
from src.telegram_client import BotClient

_BACKOFF_SCHEDULE = (1, 2, 4, 8, 16, 32, 60)
_DISCONNECT_ALERT_THRESHOLD_SECONDS = 300


class Watchdog:
    """Heartbeats, reconnects on drop, and alerts on sustained outage.

    Reconnect uses exponential backoff capped at 60s. If the userbot stays
    disconnected for more than 5 minutes cumulatively (i.e. across a single
    outage), the operator is alerted via the notifier bot.
    """

    def __init__(
        self,
        client: BotClient,
        db_path: Path,
        notifier: Notifier,
        interval_seconds: int,
    ) -> None:
        self._client = client
        self._db_path = db_path
        self._notifier = notifier
        self._interval = max(1, int(interval_seconds))
        self._log = logger.bind(module=__name__)
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Run the watchdog loop until `stop()` is called."""
        self._log.info("Watchdog starting", interval_seconds=self._interval)
        try:
            while not self._stop_requested.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(), timeout=self._interval
                    )
                except TimeoutError:
                    pass
        finally:
            self._log.info("Watchdog stopped")

    async def stop(self) -> None:
        """Signal the loop to exit. Returns once the task has fully exited."""
        self._stop_requested.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def attach_task(self, task: asyncio.Task[None]) -> None:
        """Record the background task so `stop()` can await its completion."""
        self._task = task

    async def _tick(self) -> None:
        connected = self._client.is_connected()
        storage.log_event(
            self._db_path,
            "heartbeat",
            {"connected": connected},
        )
        if connected:
            return

        self._log.warning("Userbot disconnected — attempting reconnect.")
        await self._reconnect_loop()

    async def _reconnect_loop(self) -> None:
        attempt = 0
        outage_seconds = 0.0
        alerted = False
        while not self._stop_requested.is_set():
            delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            outage_seconds += delay
            attempt += 1

            try:
                await self._client.client.connect()
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "Reconnect attempt failed",
                    attempt=attempt,
                    error=str(exc),
                )

            if self._client.is_connected():
                self._log.info(
                    "Reconnected",
                    attempts=attempt,
                    outage_seconds=outage_seconds,
                )
                storage.log_event(
                    self._db_path,
                    "reconnect",
                    {"attempts": attempt, "outage_seconds": outage_seconds},
                )
                return

            if (
                not alerted
                and outage_seconds >= _DISCONNECT_ALERT_THRESHOLD_SECONDS
            ):
                await self._notifier.alert(
                    "Userbot disconnected >5min",
                    severity="error",
                    key="watchdog_disconnect",
                )
                alerted = True
