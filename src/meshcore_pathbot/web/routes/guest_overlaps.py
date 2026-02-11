"""Guest overlaps route — read-only prefix collision viewer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .overlaps import _annotate_collision_distances
from ..dependencies import get_db

router = APIRouter()


@router.get("/overlaps")
async def overlaps_page(
    request: Request,
    db=Depends(get_db),
):
    templates = request.app.state.templates
    collisions = _annotate_collision_distances(db.get_collisions())
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
