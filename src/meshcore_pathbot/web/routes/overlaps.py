"""Overlaps route — shows repeaters with colliding prefix IDs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..dependencies import get_db

router = APIRouter()


@router.get("/overlaps")
async def overlaps_page(
    request: Request,
    db=Depends(get_db),
):
    templates = request.app.state.templates
    collisions = db.get_collisions()
    total_affected = sum(len(g["repeaters"]) for g in collisions)
    return templates.TemplateResponse(
        "overlaps.html",
        {
            "request": request,
            "collisions": collisions,
            "collision_count": len(collisions),
            "total_affected": total_affected,
        },
    )
