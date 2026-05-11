from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from src.bot_manager import BotManager
    from src.config import Config
    from src.notifier import Notifier

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _WEB_DIR / "templates"
STATIC_DIR = _WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@dataclass(frozen=True)
class WebDeps:
    """Bundle of dependencies routes pull from `request.app.state.deps`."""

    config: Config
    bot_manager: BotManager
    notifier: Notifier
    boot_monotonic: float


def create_app(
    *,
    config: Config,
    bot_manager: BotManager,
    notifier: Notifier,
) -> FastAPI:
    """Build the FastAPI app and wire dependencies."""
    app = FastAPI(title="ofm-tg-bot control panel", docs_url=None, redoc_url=None)

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    app.state.deps = WebDeps(
        config=config,
        bot_manager=bot_manager,
        notifier=notifier,
        boot_monotonic=time.monotonic(),
    )

    # Imported here to avoid circular imports at module load.
    from src.web.routes import accounts as accounts_routes
    from src.web.routes import chats as chats_routes
    from src.web.routes import logs as logs_routes
    from src.web.routes import system as system_routes

    app.include_router(accounts_routes.router)
    app.include_router(chats_routes.router)
    app.include_router(logs_routes.router)
    app.include_router(system_routes.router)

    @app.get("/")
    async def root(_: Request) -> RedirectResponse:
        return RedirectResponse(url="/accounts", status_code=302)

    return app


def get_deps(request: Request) -> WebDeps:
    """Dependency injection helper for route functions."""
    deps = request.app.state.deps
    assert isinstance(deps, WebDeps)
    return deps
