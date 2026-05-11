from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.notifier import Notifier


async def test_disabled_when_token_missing() -> None:
    notifier = Notifier(token=None, chat_id=12345)
    with patch("aiohttp.ClientSession") as session_cls:
        await notifier.alert("ignored", severity="info")
    session_cls.assert_not_called()


async def test_disabled_when_chat_id_missing() -> None:
    notifier = Notifier(token="abc", chat_id=None)
    with patch("aiohttp.ClientSession") as session_cls:
        await notifier.alert("ignored", severity="warn")
    session_cls.assert_not_called()


async def test_disabled_when_both_missing() -> None:
    notifier = Notifier(token=None, chat_id=None)
    with patch("aiohttp.ClientSession") as session_cls:
        await notifier.alert("ignored", severity="error", key="k")
    session_cls.assert_not_called()


async def test_enabled_posts_to_api() -> None:
    notifier = Notifier(token="abc123", chat_id=42)

    response = AsyncMock()
    response.status = 200
    response.text = AsyncMock(return_value="ok")

    post_ctx = AsyncMock()
    post_ctx.__aenter__.return_value = response
    post_ctx.__aexit__.return_value = None

    session = AsyncMock()
    session.post = lambda *_a, **_k: post_ctx

    session_ctx = AsyncMock()
    session_ctx.__aenter__.return_value = session
    session_ctx.__aexit__.return_value = None

    with patch("aiohttp.ClientSession", return_value=session_ctx) as session_cls:
        await notifier.alert("hello", severity="info")

    session_cls.assert_called_once()


async def test_rate_limit_suppresses_repeats() -> None:
    notifier = Notifier(token="abc123", chat_id=42)

    response = AsyncMock()
    response.status = 200
    response.text = AsyncMock(return_value="ok")
    post_ctx = AsyncMock()
    post_ctx.__aenter__.return_value = response
    post_ctx.__aexit__.return_value = None
    session = AsyncMock()
    session.post = lambda *_a, **_k: post_ctx
    session_ctx = AsyncMock()
    session_ctx.__aenter__.return_value = session
    session_ctx.__aexit__.return_value = None

    with patch("aiohttp.ClientSession", return_value=session_ctx) as session_cls:
        await notifier.alert("first", severity="error", key="dupe")
        await notifier.alert("second", severity="error", key="dupe")

    assert session_cls.call_count == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
