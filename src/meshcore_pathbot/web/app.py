"""FastAPI application factory."""

from __future__ import annotations

import time

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config.schema import AppConfig
from ..core.bot import PathBot
from ..core.message_store import MessageStore
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
    message_store: MessageStore,
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
    app.state.message_store = message_store
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Custom Jinja2 filter: unix timestamp -> readable time
    def format_timestamp(ts: float | int | None) -> str:
        if not ts:
            return "--"
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.strftime("%H:%M:%S")
        except (ValueError, OSError):
            return "--"

    def format_relative(ts: float | int | None) -> str:
        """Unix timestamp -> '2m ago', '3h ago', '1d ago', etc."""
        if not ts:
            return "--"
        try:
            diff = time.time() - float(ts)
            if diff < 0:
                return "just now"
            if diff < 60:
                return f"{int(diff)}s ago"
            if diff < 3600:
                return f"{int(diff // 60)}m ago"
            if diff < 86400:
                return f"{int(diff // 3600)}h ago"
            return f"{int(diff // 86400)}d ago"
        except (ValueError, TypeError):
            return "--"

    def format_datetime(ts: float | int | None) -> str:
        """Unix timestamp -> full date/time string for tooltips."""
        if not ts:
            return ""
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except (ValueError, OSError):
            return ""

    templates.env.filters["fmt_time"] = format_timestamp
    templates.env.filters["fmt_relative"] = format_relative
    templates.env.filters["fmt_datetime"] = format_datetime
    app.state.templates = templates

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
