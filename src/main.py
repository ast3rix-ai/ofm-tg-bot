from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn
from loguru import logger

from src import accounts as accounts_mod
from src import storage
from src.bot_manager import BotManager
from src.config import Config, ConfigError, load_config
from src.llm.client import LLMClient
from src.logging_config import setup_logging
from src.notifier import Notifier
from src.web.app import create_app


def _build_migration_context(config: Config) -> dict[str, object]:
    """Assemble the context dict consumed by `init_db` migrations."""
    ctx: dict[str, object] = {
        "encryption_key": config.session_encryption_key,
    }
    if config.tg_api_id and config.tg_api_hash and config.tg_phone:
        ctx["default_label"] = "Default"
        ctx["default_api_id"] = config.tg_api_id
        ctx["default_api_hash"] = config.tg_api_hash
        ctx["default_phone"] = config.tg_phone
    if config.legacy_encrypted_session_path.exists():
        ctx["legacy_session_path"] = str(config.legacy_encrypted_session_path)
    return ctx


async def main() -> None:
    """Boot the bot host: run migrations, start UI, optionally auto-activate."""
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)

    setup_logging(config)
    log = logger.bind(module="src.main")

    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    migration_context = _build_migration_context(config)
    storage.init_db(config.db_path, migration_context)
    log.info("DB initialized", db_path=str(config.db_path))

    notifier = Notifier(config.notifier_bot_token, config.notifier_chat_id)
    llm = LLMClient(
        host=config.ollama_host,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )
    # Best-effort health check at boot — logs warn if Ollama is unreachable
    # or the model isn't pulled. Does not block startup.
    if config.llm_model:
        try:
            ok = await llm.ping()
            if ok:
                log.info("LLM reachable", model=config.llm_model)
            else:
                log.warning(
                    "LLM health check failed at boot",
                    health=llm.health(),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM ping raised", error=str(exc))
    else:
        log.warning(
            "LLM_MODEL is not configured — classification will be skipped"
            " until set (see docs/MODEL_SELECTION.md)"
        )

    bot_manager = BotManager(
        db_path=config.db_path,
        encryption_key=config.session_encryption_key,
        notifier=notifier,
        llm=llm,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        confidence_threshold=config.classifier_confidence_threshold,
        bootstrap_history_messages=config.bootstrap_history_messages,
        bootstrap_history_days=config.bootstrap_history_days,
        bootstrap_max_concurrent=config.bootstrap_max_concurrent,
        backlog_max_concurrent=config.backlog_max_concurrent,
        resurface_dormant_days=config.resurface_dormant_days,
        response_temperature=config.response_temperature,
        response_max_tokens=config.response_max_tokens,
        response_max_retries=config.response_max_retries,
        response_persona_path=config.response_persona_path,
        operator_user_ids=config.operator_user_ids,
        default_bot_enabled_new_chats=config.default_bot_enabled_new_chats,
    )

    app = create_app(config=config, bot_manager=bot_manager, notifier=notifier)

    uv_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=config.ui_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        log.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)
    else:
        signal.signal(signal.SIGINT, _request_stop)
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (AttributeError, ValueError):
            pass

    log.info(
        "UI ready", host="127.0.0.1", port=config.ui_port,
        url=f"http://127.0.0.1:{config.ui_port}",
    )
    await notifier.alert(
        f"Bot host started — UI at http://127.0.0.1:{config.ui_port}",
        severity="info",
    )

    if config.ui_auto_activate:
        active = accounts_mod.get_active_account(
            config.db_path, config.session_encryption_key
        )
        if active is not None and active.has_session:
            log.info(
                "Auto-activating account",
                account_id=active.id, label=active.label,
            )
            asyncio.create_task(
                _auto_activate(bot_manager, active.id),
                name="auto-activate",
            )
        elif active is not None:
            log.info(
                "Active account has no session yet — open the UI to authenticate",
                account_id=active.id,
            )
        else:
            log.info("No active account — open the UI to add or activate one")

    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

    try:
        await asyncio.wait(
            {stop_task, server_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        stop_task.cancel()
        log.info("Shutting down")
        try:
            await bot_manager.deactivate()
        except Exception as exc:  # noqa: BLE001
            log.warning("Bot deactivate raised", error=str(exc))
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10)
        except (TimeoutError, Exception):  # noqa: BLE001
            server_task.cancel()
        await notifier.alert("Bot host stopped", severity="info")
        log.info("Bot host stopped")


async def _auto_activate(bot_manager: BotManager, account_id: int) -> None:
    try:
        await bot_manager.activate(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.bind(module="src.main").warning(
            "Auto-activate failed", account_id=account_id, error=str(exc)
        )


if __name__ == "__main__":
    asyncio.run(main())
