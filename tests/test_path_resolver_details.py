from __future__ import annotations

from pathlib import Path

import pytest

from meshcore_pathbot.core.path_resolver import PathResolver
from meshcore_pathbot.core.repeater_db import RepeaterDB


@pytest.mark.asyncio
async def test_resolve_detailed_includes_prefix_options_for_ambiguous_hop(tmp_path: Path) -> None:
    db = RepeaterDB(tmp_path / "repeaters.json")
    await db.load()

    await db.update_from_contact(
        {
            "public_key": "aa1111",
            "adv_type": 2,
            "adv_name": "Alpha",
            "adv_lat": 40.0,
            "adv_lon": -74.0,
            "last_seen": 100,
        }
    )
    await db.update_from_contact(
        {
            "public_key": "aa2222",
            "adv_type": 2,
            "adv_name": "Alpine",
            "adv_lat": 40.2,
            "adv_lon": -73.8,
            "last_seen": 200,
        }
    )
    await db.update_from_contact(
        {
            "public_key": "bb3333",
            "adv_type": 2,
            "adv_name": "Bravo",
            "adv_lat": 41.0,
            "adv_lon": -73.0,
            "last_seen": 300,
        }
    )

    resolver = PathResolver(db)
    hops = resolver.resolve_detailed("aabb")

    assert len(hops) == 2
    first = hops[0]
    assert first["prefix"] == "aa"
    assert first["ambiguous"] is True
    assert first["candidates"] == 2
    assert [option["name"] for option in first["options"]] == ["Alpha", "Alpine"]
