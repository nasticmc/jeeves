"""Top-level application orchestrator: run bot + web server concurrently."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn

from .config.schema import AppConfig
from .core.bot import PathBot
from .core.message_store import MessageStore
from .core.repeater_db import RepeaterDB
from .events.bus import EventBus

log = logging.getLogger("pathbot.app")


async def _daily_db_cleanup(db: RepeaterDB) -> None:
    """Run stale repeater cleanup once per day."""
    while True:
        try:
            deleted = await db.cleanup_stale(max_age_days=7)
            if deleted:
                log.info(f"Daily repeater cleanup removed {deleted} stale entries")
            else:
                log.debug("Daily repeater cleanup found no stale entries")
        except Exception as e:
            log.warning(f"Daily repeater cleanup failed: {e}")

        await asyncio.sleep(24 * 60 * 60)


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
    cleanup_task = asyncio.create_task(_daily_db_cleanup(db), name="daily-db-cleanup")

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
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            await bot.stop()
    else:
        log.info("Web dashboard disabled, running bot only")
        try:
            await bot.start()
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutting down...")
        finally:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            await bot.stop()
