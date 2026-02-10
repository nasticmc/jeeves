from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshcore_pathbot.core.message_store import MessageStore


@pytest.mark.asyncio
async def test_message_store_add_get_recent_and_paths_for_peer(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    await store.add("in", "Alice", "hello", timestamp=100.0, path="aa11")
    await store.add("in", "alice", "again", timestamp=200.0, path="")
    await store.add("out", "alice", "reply", timestamp=300.0, path="bb22")

    recent = store.get_recent(2)
    assert len(recent) == 2
    assert recent[0]["text"] == "reply"
    assert recent[1]["text"] == "again"

    # only incoming messages for this peer, case-insensitive peer matching
    assert store.get_paths_for_peer("ALICE") == ["", "aa11"]


@pytest.mark.asyncio
async def test_message_store_migrates_legacy_json(tmp_path: Path) -> None:
    legacy_path = tmp_path / "messages.json"
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "id": "1",
                    "direction": "in",
                    "peer": "Bob",
                    "text": "old",
                    "timestamp": 123.0,
                    "channel": 1,
                    "path": "ff00",
                }
            ]
        )
    )

    store = MessageStore(legacy_path)
    await store.load()

    assert store.filepath.suffix == ".db"
    assert store.count == 1
    assert store.get_recent(1)[0]["text"] == "old"
    assert store.get_paths_for_peer("bob") == ["ff00"]
