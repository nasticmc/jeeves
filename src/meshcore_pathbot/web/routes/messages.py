"""Messages route — live message feed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..dependencies import get_bot

router = APIRouter()


@router.get("/messages")
async def messages_page(
    request: Request,
    bot=Depends(get_bot),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "messages.html",
        {
            "request": request,
            "stats": bot.stats.to_dict(),
        },
    )
