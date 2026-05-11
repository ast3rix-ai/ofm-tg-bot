from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from src.web.app import WebDeps, get_deps, templates

router = APIRouter(tags=["logs"])

_TAIL_LINES = 100
_POLL_INTERVAL = 0.5


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, _: WebDeps = Depends(get_deps)) -> HTMLResponse:
    return templates.TemplateResponse(request, "logs.html", {})


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=n))
    except OSError:
        return []


async def _log_stream(path: Path) -> AsyncIterator[dict[str, str]]:
    for line in _tail_lines(path, _TAIL_LINES):
        yield {"event": "log", "data": line.rstrip("\n")}

    position: int = path.stat().st_size if path.exists() else 0
    buffer = ""
    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size < position:
            # File was rotated.
            position = 0
            buffer = ""
        if size == position:
            yield {"event": "ping", "data": ""}
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(position)
                chunk = fh.read(size - position)
                position = fh.tell()
        except OSError:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line:
                yield {"event": "log", "data": line}


@router.get("/logs/stream")
async def logs_stream(deps: WebDeps = Depends(get_deps)) -> EventSourceResponse:
    log_path = deps.config.logs_dir / "bot.log"
    return EventSourceResponse(_log_stream(log_path))
