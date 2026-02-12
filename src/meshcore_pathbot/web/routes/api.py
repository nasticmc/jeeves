"""API routes — SSE streams and JSON endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from sse_starlette import EventSourceResponse

from ...events.types import AppEvent
from ..dependencies import get_bot, get_bus, get_db, get_message_store
from ..sse import sse_stream

router = APIRouter()


@router.get("/messages/stream")
async def messages_stream(request: Request, bus=Depends(get_bus)):
    """SSE stream for live message updates."""
    return EventSourceResponse(
        sse_stream(request, bus, AppEvent.MSG_IN, AppEvent.MSG_OUT)
    )


@router.get("/stats/stream")
async def stats_stream(request: Request, bus=Depends(get_bus)):
    """SSE stream for stats updates."""
    return EventSourceResponse(
        sse_stream(
            request,
            bus,
            AppEvent.STATS_UPDATE,
            AppEvent.BOT_CONNECTED,
            AppEvent.BOT_DISCONNECTED,
        )
    )


@router.get("/repeaters/stream")
async def repeaters_stream(request: Request, bus=Depends(get_bus)):
    """SSE stream for repeater DB updates."""
    return EventSourceResponse(
        sse_stream(request, bus, AppEvent.REPEATER_UPDATE, AppEvent.REPEATER_DELETE)
    )


@router.get("/uptime")
async def uptime(bot=Depends(get_bot)):
    """Return formatted uptime string."""
    seconds = int(bot.stats.uptime_seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@router.get("/messages/history")
async def messages_history(
    count: int = 200,
    store=Depends(get_message_store),
):
    """Return message history as JSON."""
    return store.get_recent(count)


@router.get("/stats/json")
async def stats_json(bot=Depends(get_bot), db=Depends(get_db)):
    """Return current stats as JSON."""
    return {
        "bot": bot.stats.to_dict(),
        "db": db.stats(),
        "connected": bot.is_connected,
        "timestamp": time.time(),
    }


@router.get("/packets/totals")
async def packets_totals(store=Depends(get_message_store)):
    """Return 24-hour packet totals by type."""
    return store.get_24h_totals()


@router.get("/packets/hourly")
async def packets_hourly(store=Depends(get_message_store)):
    """Return hourly packet counts for the last 24 hours."""
    from datetime import datetime
    data = store.get_hourly_counts(24)
    return {
        "labels": [datetime.fromtimestamp(d["hour_ts"]).strftime("%H:%M") for d in data],
        "msg_in": [d["msg_in"] for d in data],
        "msg_out": [d["msg_out"] for d in data],
        "advert": [d["advert"] for d in data],
    }
