"""Top-level application orchestrator: run bot + web server concurrently."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .config.schema import AppConfig
from .core.bot import PathBot
from .core.message_store import MessageStore
from .core.repeater_db import RepeaterDB
from .events.bus import EventBus

log = logging.getLogger("pathbot.app")


async def run(config: AppConfig) -> None:
    """Initialize all components and run the bot (+ optional web server)."""
    bus = EventBus()
    db = RepeaterDB(config.bot.repeaters_file)
    await db.load()

    # Message store lives alongside repeaters file
    messages_path = config.bot.repeaters_file.parent / "messages.json"
    message_store = MessageStore(messages_path)
    await message_store.load()

    bot = PathBot(config, db, bus, message_store)

    if config.web.enabled:
        from .web.app import create_app

        web_app = create_app(config, bot, db, bus, message_store)
        uvi_config = uvicorn.Config(
            web_app,
            host=config.web.host,
            port=config.web.port,
            log_level=config.logging.level.lower(),
        )
        server = uvicorn.Server(uvi_config)

        log.info(f"Starting web dashboard on http://{config.web.host}:{config.web.port}")

        try:
            await asyncio.gather(
                bot.start(),
                server.serve(),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutting down...")
        finally:
            await bot.stop()
    else:
        log.info("Web dashboard disabled, running bot only")
        try:
            await bot.start()
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutting down...")
        finally:
            await bot.stop()
