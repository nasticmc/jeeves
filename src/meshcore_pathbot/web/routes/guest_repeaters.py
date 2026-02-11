"""Guest repeaters route — read-only database viewer (no delete)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..dependencies import get_db

router = APIRouter()


@router.get("/repeaters")
async def repeaters_page(
    request: Request,
    db=Depends(get_db),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "repeaters.html",
        {
            "request": request,
            "repeaters": db.get_all(),
            "stats": db.stats(),
        },
    )


@router.get("/")
async def guest_root(
    request: Request,
    db=Depends(get_db),
):
    """Redirect guest root to the repeaters page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "repeaters.html",
        {
            "request": request,
            "repeaters": db.get_all(),
            "stats": db.stats(),
        },
    )
