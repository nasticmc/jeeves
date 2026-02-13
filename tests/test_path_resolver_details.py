from __future__ import annotations

from pathlib import Path

import asyncio

from meshcore_pathbot.core.path_resolver import PathResolver
from meshcore_pathbot.config.schema import AppConfig
from meshcore_pathbot.core.repeater_db import RepeaterDB


def test_resolve_detailed_includes_prefix_options_for_ambiguous_hop(tmp_path: Path) -> None:
    db = RepeaterDB(tmp_path / "repeaters.json")
    asyncio.run(db.load())

    asyncio.run(db.update_from_contact(
        {
            "public_key": "aa1111",
            "adv_type": 2,
            "adv_name": "Alpha",
            "adv_lat": 40.0,
            "adv_lon": -74.0,
            "last_seen": 100,
        }
    ))
    asyncio.run(db.update_from_contact(
        {
            "public_key": "aa2222",
            "adv_type": 2,
            "adv_name": "Alpine",
            "adv_lat": 40.2,
            "adv_lon": -73.8,
            "last_seen": 200,
        }
    ))
    asyncio.run(db.update_from_contact(
        {
            "public_key": "bb3333",
            "adv_type": 2,
            "adv_name": "Bravo",
            "adv_lat": 41.0,
            "adv_lon": -73.0,
            "last_seen": 300,
        }
    ))

    resolver = PathResolver(db)
    hops = resolver.resolve_detailed("aabb")

    assert len(hops) == 2
    first = hops[0]
    assert first["prefix"] == "aa"
    assert first["ambiguous"] is True
    assert first["candidates"] == 2
    assert first["selected_by"] == "distance"
    assert [option["name"] for option in first["options"]] == ["Alpha", "Alpine"]


def test_resolve_detailed_uses_manual_selection_and_marks_selected_option(tmp_path: Path) -> None:
    db = RepeaterDB(tmp_path / "repeaters.json")
    asyncio.run(db.load())

    asyncio.run(db.update_from_contact(
        {
            "public_key": "aa1111",
            "adv_type": 2,
            "adv_name": "Alpha",
            "adv_lat": 10.0,
            "adv_lon": 10.0,
            "last_seen": 100,
        }
    ))
    asyncio.run(db.update_from_contact(
        {
            "public_key": "aa2222",
            "adv_type": 2,
            "adv_name": "Alpine",
            "adv_lat": 20.0,
            "adv_lon": 20.0,
            "last_seen": 200,
        }
    ))
    asyncio.run(db.update_from_contact(
        {
            "public_key": "bb3333",
            "adv_type": 2,
            "adv_name": "Bravo",
            "adv_lat": 21.0,
            "adv_lon": 21.0,
            "last_seen": 300,
        }
    ))

    resolver = PathResolver(db)
    hops = resolver.resolve_detailed("aabb", preferred_repeaters={"aa": "aa1111"})

    assert hops[0]["name"] == "Alpha"
    assert hops[0]["ambiguous"] is False
    assert hops[0]["selected_by"] == "manual"
    selected = [option for option in hops[0]["options"] if option["selected"]]
    assert len(selected) == 1
    assert selected[0]["public_key"] == "aa1111"
    assert hops[1]["distance_from_prev"] is not None


def test_resolve_detailed_home_repeater_last_hop_uses_bot_location(tmp_path: Path) -> None:
    db = RepeaterDB(tmp_path / "repeaters.json")
    asyncio.run(db.load())

    config = AppConfig()
    config.bot.home_repeater_name = "Home"
    config.bot.home_repeater_prefix = "bb"
    config.bot.lat = 12.34
    config.bot.lon = 56.78

    resolver = PathResolver(db, config=config)
    hops = resolver.resolve_detailed("aabb")

    assert hops[1]["name"] == "Home"
    assert hops[1]["lat"] == 12.34
    assert hops[1]["lon"] == 56.78


def test_normalize_path_accepts_plain_colon_and_comma_delimiters() -> None:
    assert PathResolver.normalize_path("c132fa9d7a") == "c132fa9d7a"
    assert PathResolver.normalize_path("c1:32:fa:9d:7a") == "c132fa9d7a"
    assert PathResolver.normalize_path("c1,32,fa,9d,7a") == "c132fa9d7a"
