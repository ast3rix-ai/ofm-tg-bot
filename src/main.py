from __future__ import annotations

import asyncio
import signal
import sys
from types import FrameType

from loguru import logger

from src import storage
from src.config import ConfigError, load_config
from src.event_handler import EventHandler
from src.logging_config import setup_logging
from src.notifier import Notifier
from src.telegram_client import BotClient
from src.watchdog import Watchdog


async def main() -> None:
    """Boot the userbot: connect, register handlers, run until disconnected."""
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)

    setup_logging(config)
    log = logger.bind(module="src.main")

    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.session_tmp_dir.mkdir(parents=True, exist_ok=True)

    storage.init_db(config.db_path)
    log.info("DB initialized", db_path=str(config.db_path))

    notifier = Notifier(config.notifier_bot_token, config.notifier_chat_id)
    client = BotClient(config)
    handler = EventHandler(client, config.db_path, notifier)
    watchdog = Watchdog(
        client, config.db_path, notifier, config.heartbeat_interval_seconds
    )

    await notifier.alert("Bot starting", severity="info")

    try:
        await client.start()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to start Telegram client", error=str(exc))
        await notifier.alert(
            f"Bot start failed: {exc}", severity="error", key="bot_start_failure"
        )
        raise

    await handler.setup()

    watchdog_task = asyncio.create_task(watchdog.run(), name="watchdog")
    watchdog.attach_task(watchdog_task)

    stop_event = asyncio.Event()

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        log.info("Shutdown signal received", signal=signum)
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
    else:
        signal.signal(signal.SIGINT, _request_stop)
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (AttributeError, ValueError):
            pass

    await notifier.alert("Bot started", severity="info")
    log.info("Bot started")

    disconnect_task = asyncio.create_task(
        client.client.run_until_disconnected(),
        name="run_until_disconnected",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop_wait")

    try:
        done, _pending = await asyncio.wait(
            {disconnect_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            log.info("Stop requested — shutting down.")
        else:
            log.info("Telegram client disconnected — shutting down.")
    finally:
        stop_task.cancel()
        await watchdog.stop()
        if not disconnect_task.done():
            disconnect_task.cancel()
            try:
                await disconnect_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await client.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("Client stop raised", error=str(exc))
        await notifier.alert("Bot stopped", severity="info")
        log.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
