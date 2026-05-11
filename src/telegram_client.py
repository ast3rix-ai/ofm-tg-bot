from __future__ import annotations

import shutil

from loguru import logger
from telethon import TelegramClient

from src.config import Config
from src.crypto import decrypt_file, encrypt_file


class BotClient:
    """Telethon wrapper that encrypts the session file at rest.

    Lifecycle:
        1. `start()` decrypts `session.enc` (if it exists) to a temp file and
           connects. On first run, Telethon prompts on stdin for the phone
           code (and 2FA password if set).
        2. `stop()` disconnects, re-encrypts the temp session, and removes
           the temp directory.

    Thread/task-safety: the underlying Telethon client is single-loop; do
    not share across processes.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._log = logger.bind(module=__name__)
        self._client: TelegramClient | None = None
        self._session_loaded = False

    @property
    def client(self) -> TelegramClient:
        """Underlying Telethon client. Available after `start()`."""
        if self._client is None:
            raise RuntimeError("BotClient.start() has not been called.")
        return self._client

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        return bool(self._client.is_connected())

    def _prepare_session(self) -> None:
        cfg = self._config
        cfg.session_tmp_dir.mkdir(parents=True, exist_ok=True)
        if cfg.encrypted_session_path.exists():
            self._log.info(
                "Decrypting session at rest",
                src=str(cfg.encrypted_session_path),
            )
            decrypt_file(
                cfg.encrypted_session_path,
                cfg.decrypted_session_path,
                cfg.session_encryption_key,
            )
            self._session_loaded = True
        else:
            self._log.info(
                "No encrypted session found — first-run auth will be required."
            )

    def _persist_session(self) -> None:
        cfg = self._config
        if not cfg.decrypted_session_path.exists():
            self._log.warning("No decrypted session to persist.")
            return
        encrypt_file(
            cfg.decrypted_session_path,
            cfg.encrypted_session_path,
            cfg.session_encryption_key,
        )
        self._log.info(
            "Session re-encrypted at rest",
            dst=str(cfg.encrypted_session_path),
        )

    def _cleanup_tmp(self) -> None:
        cfg = self._config
        try:
            if cfg.session_tmp_dir.exists():
                shutil.rmtree(cfg.session_tmp_dir, ignore_errors=True)
        except OSError as exc:
            self._log.warning("Failed to remove session tmp dir", error=str(exc))

    async def start(self) -> None:
        """Connect to Telegram, decrypting the session and prompting on first run."""
        self._prepare_session()
        cfg = self._config
        self._client = TelegramClient(
            str(cfg.decrypted_session_path.with_suffix("")),
            cfg.tg_api_id,
            cfg.tg_api_hash,
        )
        self._log.info("Connecting to Telegram", phone=cfg.tg_phone)
        await self._client.start(phone=lambda: cfg.tg_phone)
        me = await self._client.get_me()
        self._log.info(
            "Connected",
            user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
        )

    async def stop(self) -> None:
        """Disconnect, re-encrypt the session, and remove the temp directory."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("Disconnect raised", error=str(exc))
        try:
            self._persist_session()
        finally:
            self._cleanup_tmp()
            self._client = None
