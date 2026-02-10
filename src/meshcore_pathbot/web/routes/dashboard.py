"""Dashboard route — main landing page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..dependencies import get_bot, get_db

router = APIRouter()


@router.get("/")
async def dashboard(
    request: Request,
    bot=Depends(get_bot),
    db=Depends(get_db),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": bot.stats.to_dict(),
            "db_stats": db.stats(),
            "connected": bot.is_connected,
            "channel": bot.config.bot.channel,
        },
    )
