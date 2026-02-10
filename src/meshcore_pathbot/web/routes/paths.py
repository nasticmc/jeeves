"""Paths route — path resolution tool."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ..dependencies import get_bot

router = APIRouter()


@router.get("/paths")
async def paths_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "paths.html",
        {"request": request, "hops": None, "raw_path": ""},
    )


@router.post("/paths/resolve")
async def resolve_path(
    request: Request,
    raw_path: str = Form(""),
    bot=Depends(get_bot),
):
    templates = request.app.state.templates
    raw_path = raw_path.strip().lower()

    hops = bot.resolver.resolve_detailed(raw_path) if raw_path else []
    resolved_str = bot.resolver.resolve(raw_path) if raw_path else ""
    raw_str = bot.resolver.raw(raw_path) if raw_path else ""

    return templates.TemplateResponse(
        "partials/path_display.html",
        {
            "request": request,
            "hops": hops,
            "resolved_str": resolved_str,
            "raw_str": raw_str,
        },
    )
