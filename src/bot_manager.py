from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from src import accounts, storage
from src.accounts import AccountsError
from src.backlog import BacklogProcessor
from src.classifier import Classifier
from src.event_handler import EventHandler
from src.llm.client import LLMClient
from src.notifier import Notifier
from src.response_generator import ResponseGenerator
from src.telegram_client import BotClient, auth_provider_from_queue
from src.watchdog import Watchdog

State = Literal[
    "idle",
    "starting",
    "awaiting_code",
    "awaiting_password",
    "bootstrapping",
    "running",
    "stopping",
    "error",
]


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class BotManager:
    """Owns the lifecycle of a single active `BotClient` instance.

    The manager is the surface the web UI talks to. It does not import
    web symbols itself; the routes drive it with regular method calls and
    poll `status()` for updates.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        encryption_key: str,
        notifier: Notifier,
        llm: LLMClient,
        heartbeat_interval_seconds: int = 60,
        confidence_threshold: float = 0.6,
        bootstrap_history_messages: int = 100,
        bootstrap_history_days: int = 30,
        bootstrap_max_concurrent: int = 3,
        backlog_max_concurrent: int = 5,
        resurface_dormant_days: int = 14,
        response_temperature: float = 0.85,
        response_max_tokens: int = 200,
        response_max_retries: int = 2,
        response_persona_path: Path | None = None,
        operator_user_ids: frozenset[int] = frozenset(),
        default_bot_enabled_new_chats: int = 1,
    ) -> None:
        self._db_path = db_path
        self._encryption_key = encryption_key
        self._notifier = notifier
        self._heartbeat = heartbeat_interval_seconds
        self._llm = llm
        self._confidence_threshold = confidence_threshold
        self._bootstrap_history_messages = bootstrap_history_messages
        self._bootstrap_history_days = bootstrap_history_days
        self._bootstrap_max_concurrent = bootstrap_max_concurrent
        self._backlog_max_concurrent = backlog_max_concurrent
        self._resurface_dormant_days = resurface_dormant_days
        self._response_temperature = response_temperature
        self._response_max_tokens = response_max_tokens
        self._response_max_retries = response_max_retries
        self._response_persona_path = (
            response_persona_path
            or (db_path.parent.parent / "personas" / "default" / "persona.md")
        )
        self._operator_user_ids = operator_user_ids
        self._default_bot_enabled_new_chats = default_bot_enabled_new_chats
        self._log = logger.bind(module=__name__)

        self._client: BotClient | None = None
        self._event_handler: EventHandler | None = None
        self._watchdog: Watchdog | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._backlog: BacklogProcessor | None = None
        self._classifier: Classifier | None = None
        self._response_generator: ResponseGenerator | None = None

        self._state: State = "idle"
        self._active_account_id: int | None = None
        self._last_error: str | None = None
        self._connected_since: float | None = None

        self._code_queue: asyncio.Queue[str] | None = None
        self._password_queue: asyncio.Queue[str] | None = None
        self._lock = asyncio.Lock()

    # ---------- status surface ----------

    def status(self) -> dict[str, Any]:
        connected_since_iso: str | None = None
        uptime_seconds: float | None = None
        if self._connected_since is not None:
            uptime_seconds = max(0.0, time.monotonic() - self._connected_since)
            connected_since_iso = datetime.fromtimestamp(
                time.time() - uptime_seconds, tz=UTC
            ).isoformat()
        progress: dict[str, Any] | None = None
        if self._backlog is not None:
            progress = self._backlog.progress()
        return {
            "state": self._state,
            "active_account_id": self._active_account_id,
            "last_error": self._last_error,
            "connected_since": connected_since_iso,
            "uptime_seconds": uptime_seconds,
            "is_connected": self._client.is_connected() if self._client else False,
            "backlog": progress,
            "llm": self._llm.health(),
            **self._response_stats(),
        }

    def _response_stats(self) -> dict[str, Any]:
        """Response-generator counters for the active account (last hour)."""
        if self._active_account_id is None:
            return {
                "last_response_run_at": None,
                "responses_sent_last_hour": 0,
                "responses_gated_last_hour": 0,
            }
        account_id = self._active_account_id
        since = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        try:
            return {
                "last_response_run_at": storage.get_last_response_run_at(
                    self._db_path, account_id
                ),
                "responses_sent_last_hour": storage.count_response_runs_by_outcome(
                    self._db_path,
                    account_id=account_id,
                    outcome="sent",
                    since_iso=since,
                ),
                "responses_gated_last_hour": storage.count_response_runs_by_outcome(
                    self._db_path,
                    account_id=account_id,
                    outcome="gated",
                    since_iso=since,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Response stats query failed", error=str(exc))
            return {
                "last_response_run_at": None,
                "responses_sent_last_hour": 0,
                "responses_gated_last_hour": 0,
            }

    @property
    def active_account_id(self) -> int | None:
        return self._active_account_id

    # ---------- auth queues (driven by /accounts/<id>/auth) ----------

    def submit_code(self, code: str) -> bool:
        if self._code_queue is None:
            return False
        try:
            self._code_queue.put_nowait(code)
            return True
        except asyncio.QueueFull:
            return False

    def submit_password(self, password: str) -> bool:
        if self._password_queue is None:
            return False
        try:
            self._password_queue.put_nowait(password)
            return True
        except asyncio.QueueFull:
            return False

    # ---------- lifecycle ----------

    async def activate(self, account_id: int) -> None:
        """Start the client for `account_id`, stopping any currently active one."""
        async with self._lock:
            if self._client is not None:
                self._log.info("Deactivating previously-active account before switch")
                await self._deactivate_locked()
            await self._activate_locked(account_id)

    async def deactivate(self) -> None:
        """Stop the active client and return to idle."""
        async with self._lock:
            await self._deactivate_locked()

    async def _activate_locked(self, account_id: int) -> None:
        self._state = "starting"
        self._last_error = None
        self._connected_since = None
        self._active_account_id = account_id

        account = accounts.get_account(
            self._db_path,
            self._encryption_key,
            account_id,
            with_credentials=True,
        )
        if account is None:
            self._state = "error"
            self._last_error = f"No account with id {account_id}"
            raise AccountsError(self._last_error)
        if account.api_id is None or account.api_hash is None or account.phone is None:
            self._state = "error"
            self._last_error = "Account credentials are missing"
            raise AccountsError(self._last_error)

        session_string = accounts.read_session_blob(
            self._db_path, self._encryption_key, account_id
        )

        def _on_session_update(new_string: str) -> None:
            accounts.update_session_blob(
                self._db_path, self._encryption_key, account_id, new_string
            )

        client = BotClient(
            api_id=account.api_id,
            api_hash=account.api_hash,
            phone=account.phone,
            session_string=session_string,
            on_session_update=_on_session_update,
            label=account.label,
        )
        self._client = client

        self._code_queue = asyncio.Queue(maxsize=1)
        self._password_queue = asyncio.Queue(maxsize=1)

        async def _code_provider() -> str:
            self._state = "awaiting_code"
            self._log.info("Awaiting phone code from UI", account_id=account_id)
            assert self._code_queue is not None
            return await auth_provider_from_queue(self._code_queue)

        async def _password_provider() -> str:
            self._state = "awaiting_password"
            self._log.info("Awaiting 2FA password from UI", account_id=account_id)
            assert self._password_queue is not None
            return await auth_provider_from_queue(self._password_queue)

        try:
            await client.start(
                code_provider=_code_provider,
                password_provider=_password_provider,
            )
        except Exception as exc:  # noqa: BLE001
            self._state = "error"
            self._last_error = f"Activation failed: {exc}"
            self._log.error(
                "Activation failed", account_id=account_id, error=str(exc)
            )
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._code_queue = None
            self._password_queue = None
            await self._notifier.alert(
                f"Activation failed for account {account_id}: {exc}",
                severity="error",
                key="bot_activate_failure",
            )
            raise

        # Wire post-auth state.
        try:
            me_summary = await client.get_me_summary()
            accounts.update_account_metadata(
                self._db_path,
                account_id,
                tg_user_id=me_summary.get("id"),
                tg_username=me_summary.get("username"),
                last_connected_at=_utcnow_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Post-auth metadata update failed", error=str(exc)
            )

        accounts.set_active_account(self._db_path, account_id)

        # Construct classifier + event handler. Event handler defers live
        # messages onto an internal queue until bootstrap completes.
        self._classifier = Classifier(
            db_path=self._db_path,
            llm=self._llm,
            notifier=self._notifier,
            confidence_threshold=self._confidence_threshold,
            history_window=30,
        )
        self._event_handler = EventHandler(
            client=client,
            db_path=self._db_path,
            notifier=self._notifier,
            account_id=account_id,
            classifier=self._classifier,
            resurface_threshold_days=self._resurface_dormant_days,
            operator_user_ids=self._operator_user_ids,
        )
        self._response_generator = ResponseGenerator(
            db_path=self._db_path,
            llm=self._llm,
            telegram_client=client,
            persona_path=self._response_persona_path,
            get_lock=self._event_handler.get_lock,
            max_retries=self._response_max_retries,
            temperature=self._response_temperature,
            max_tokens=self._response_max_tokens,
            default_bot_enabled_new_chats=self._default_bot_enabled_new_chats,
        )
        self._event_handler.set_response_generator(self._response_generator)
        await self._event_handler.setup()

        self._watchdog = Watchdog(
            client=client,
            db_path=self._db_path,
            notifier=self._notifier,
            interval_seconds=self._heartbeat,
            account_id=account_id,
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog.run(), name=f"watchdog-{account_id}"
        )
        self._watchdog.attach_task(self._watchdog_task)

        # Best-effort LLM health check; failure is logged but non-blocking.
        try:
            await self._llm.ping()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("LLM health check raised", error=str(exc))

        self._backlog = BacklogProcessor(
            db_path=self._db_path,
            client=client,
            classifier=self._classifier,
            account_id=account_id,
            history_messages=self._bootstrap_history_messages,
            history_days=self._bootstrap_history_days,
            bootstrap_concurrency=self._bootstrap_max_concurrent,
            catchup_concurrency=self._backlog_max_concurrent,
            resurface_threshold_days=self._resurface_dormant_days,
        )

        self._run_task = asyncio.create_task(
            client.run_until_disconnected(),
            name=f"run-until-disconnected-{account_id}",
        )

        self._code_queue = None
        self._password_queue = None
        self._state = "bootstrapping"
        self._connected_since = time.monotonic()
        self._log.info("Account active — starting bootstrap", account_id=account_id)

        # Run bootstrap + catchup. Live event handler is already registered;
        # it queues messages until `_state` flips to "running".
        if self._llm.health().get("model_loaded"):
            try:
                bootstrap_report = await self._backlog.run_initial_bootstrap()
                catchup_report = await self._backlog.run_unread_catchup()
                self._log.info(
                    "Backlog finished",
                    bootstrap=bootstrap_report,
                    catchup=catchup_report,
                )
            except Exception as exc:  # noqa: BLE001
                self._log.error("Backlog phase failed", error=str(exc))
                self._last_error = f"backlog failed: {exc}"
        else:
            self._log.warning(
                "LLM not ready — skipping bootstrap/catchup; "
                "live classification will degrade until Ollama is reachable",
                llm=self._llm.health(),
            )

        self._state = "running"
        self._log.info("Account active and running", account_id=account_id)
        await self._notifier.alert(
            f"Account active: {account.label}", severity="info"
        )
        await self._event_handler.flush_pending()

    async def _deactivate_locked(self) -> None:
        if self._state == "idle" and self._client is None:
            return
        self._state = "stopping"
        try:
            if self._watchdog is not None:
                await self._watchdog.stop()
            if self._run_task is not None and not self._run_task.done():
                self._run_task.cancel()
                try:
                    await self._run_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._client is not None:
                await self._client.stop()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Deactivation raised", error=str(exc))

        accounts.clear_active_account(self._db_path)

        prior_account_id = self._active_account_id
        self._client = None
        self._event_handler = None
        self._watchdog = None
        self._watchdog_task = None
        self._run_task = None
        self._backlog = None
        self._classifier = None
        self._response_generator = None
        self._active_account_id = None
        self._connected_since = None
        self._state = "idle"
        self._log.info("Account deactivated", prior_account_id=prior_account_id)
        await self._notifier.alert(
            "Bot deactivated", severity="info"
        )
