"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config.schema import AppConfig
from ..core.bot import PathBot
from ..core.repeater_db import RepeaterDB
from ..events.bus import EventBus

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(
    config: AppConfig,
    bot: PathBot,
    db: RepeaterDB,
    bus: EventBus,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="MeshCore PathBot",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store shared state for dependency injection
    app.state.config = config
    app.state.bot = bot
    app.state.db = db
    app.state.bus = bus
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Import and include route routers
    from .routes import api, dashboard, messages, paths, repeaters, settings, stats

    app.include_router(dashboard.router)
    app.include_router(messages.router)
    app.include_router(repeaters.router)
    app.include_router(paths.router)
    app.include_router(settings.router)
    app.include_router(stats.router)
    app.include_router(api.router, prefix="/api")

    return app
