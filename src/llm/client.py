from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import ollama
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class LLMError(RuntimeError):
    """Raised when an LLM call cannot be completed after retries."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str


# Errors worth retrying on (connectivity, transient timeouts).
_TRANSIENT_EXC = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    ConnectionError,
    TimeoutError,
)


class LLMClient:
    """Async wrapper over Ollama's chat/generate endpoints with retry.

    Errors from the model itself (parse failures, malformed responses) are
    surfaced to the caller as `LLMError` without retry. Connection-level
    failures retry up to `max_retries` times with exponential backoff.
    """

    def __init__(
        self,
        *,
        host: str,
        model: str | None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._host = host
        self._model = model
        self._timeout = float(timeout_seconds)
        self._max_retries = max(1, int(max_retries) + 1)  # tenacity total attempts
        self._log = logger.bind(module=__name__)
        self._client = ollama.AsyncClient(host=host, timeout=self._timeout)
        self._last_ping: dict[str, Any] = {
            "reachable": False,
            "model_loaded": False,
            "last_check_at": None,
            "error": None,
        }

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def host(self) -> str:
        return self._host

    def health(self) -> dict[str, Any]:
        """Last `ping()` result. Safe to read without awaiting."""
        return dict(self._last_ping)

    async def ping(self) -> bool:
        """Verify Ollama is reachable and the configured model is available.

        Returns True on success. Updates `health()` regardless. Does not raise.
        """
        from datetime import UTC, datetime

        now_iso = datetime.now(UTC).isoformat()
        if not self._model:
            self._last_ping = {
                "reachable": False,
                "model_loaded": False,
                "last_check_at": now_iso,
                "error": "LLM_MODEL not configured",
            }
            return False
        try:
            tags = await self._client.list()
            models = tags.get("models", []) if isinstance(tags, dict) else (
                getattr(tags, "models", []) or []
            )
            available: set[str] = set()
            for m in models:
                if isinstance(m, dict):
                    name = m.get("name") or m.get("model")
                else:
                    name = getattr(m, "model", None) or getattr(m, "name", None)
                if name:
                    available.add(str(name))
            model_loaded = (
                self._model in available
                or any(name.startswith(f"{self._model}:") for name in available)
                or any(name.split(":", 1)[0] == self._model for name in available)
            )
            self._last_ping = {
                "reachable": True,
                "model_loaded": model_loaded,
                "last_check_at": now_iso,
                "error": None if model_loaded else (
                    f"model {self._model!r} not pulled — run "
                    f"`ollama pull {self._model}`"
                ),
                "available_models": sorted(available),
            }
            return bool(model_loaded)
        except Exception as exc:  # noqa: BLE001
            self._last_ping = {
                "reachable": False,
                "model_loaded": False,
                "last_check_at": now_iso,
                "error": str(exc),
            }
            return False

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        stop: list[str] | None = None,
        response_format: Literal["text", "json"] = "text",
    ) -> LLMResponse:
        """Call the local LLM. Retries connection-level failures.

        Raises:
            LLMError: If the model isn't configured, the server can't be
                reached after retries, or the call returns an error.
        """
        target_model = model or self._model
        if not target_model:
            raise LLMError(
                "LLM_MODEL is not configured. Set it in .env after reviewing "
                "docs/MODEL_SELECTION.md and pulling the model via Ollama."
            )

        options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if stop:
            options["stop"] = stop

        format_arg: Literal["", "json"] = (
            "json" if response_format == "json" else ""
        )

        start = time.monotonic()
        last_error: Exception | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(_TRANSIENT_EXC),
            reraise=False,
        ):
            with attempt:
                try:
                    result = await self._client.generate(
                        model=target_model,
                        prompt=prompt,
                        options=options,
                        format=format_arg,
                        stream=False,
                    )
                except _TRANSIENT_EXC as exc:
                    last_error = exc
                    self._log.warning(
                        "LLM transient error — retrying",
                        attempt=attempt.retry_state.attempt_number,
                        error=str(exc),
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    self._log.error("LLM call failed", error=str(exc))
                    raise LLMError(f"LLM call failed: {exc}") from exc
                else:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    payload: dict[str, Any]
                    if isinstance(result, dict):
                        payload = result
                    else:
                        payload = getattr(result, "model_dump", lambda: {})()
                        if not payload and hasattr(result, "__dict__"):
                            payload = dict(result.__dict__)
                    text = str(payload.get("response", "") or "")
                    return LLMResponse(
                        text=text,
                        tokens_in=int(payload.get("prompt_eval_count", 0) or 0),
                        tokens_out=int(payload.get("eval_count", 0) or 0),
                        latency_ms=latency_ms,
                        model=target_model,
                    )

        # If retries exhausted with only transient errors, surface as LLMError.
        raise LLMError(
            f"LLM unreachable after {self._max_retries} attempt(s): {last_error}"
        )
