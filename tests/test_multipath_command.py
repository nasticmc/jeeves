from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshcore_pathbot.config.schema import AppConfig, ChannelConfig
from meshcore_pathbot.core.bot import PathBot
from meshcore_pathbot.core.message_store import MessageStore
from meshcore_pathbot.events.bus import EventBus


class DummyDB:
    def get_by_prefix(self, prefix: str):
        return []


class DummyCommands:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_chan_msg(self, channel_id: int, text: str):
        self.sent.append((channel_id, text))
        return SimpleNamespace(type=None, payload={})


def _make_bot(store: MessageStore, channels: list[ChannelConfig]) -> tuple[PathBot, DummyCommands]:
    config = AppConfig()
    config.bot.channels = channels
    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=store)
    commands = DummyCommands()
    bot._mc = SimpleNamespace(commands=commands)
    return bot, commands


@pytest.mark.asyncio
async def test_get_paths_for_recent_message_returns_all_arrival_paths(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    await store.add("in", "Alice", "Alice: ping", timestamp=100.0, channel=2, path="a1b2c3")
    await store.add("in", "Alice", "Alice: ping", timestamp=101.0, channel=2, path="a1d4e5")
    await store.add("in", "Alice", "Alice: paths", timestamp=110.0, channel=2, path="a1b2c3")
    await store.add("in", "Alice", "Alice: multipath", timestamp=120.0, channel=2, path="a1b2c3")

    result = store.get_paths_for_recent_message("Alice", channel=2, exclude_command="multipath")
    assert result is not None
    body, paths = result
    # Most recent non-multipath message was "paths" — only one arrival path.
    assert body == "paths"
    assert paths == ["a1b2c3"]


@pytest.mark.asyncio
async def test_get_paths_for_recent_message_groups_duplicate_arrivals(tmp_path: Path) -> None:
    """When the same message text is delivered repeatedly (e.g. flooded copies),
    multipath should surface every distinct arrival path."""
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    await store.add("in", "Alice", "Alice: ping", timestamp=100.0, channel=2, path="a1b2c3")
    await store.add("in", "Alice", "Alice: ping", timestamp=101.0, channel=2, path="a1d4e5")
    await store.add("in", "Alice", "Alice: multipath", timestamp=120.0, channel=2, path="a1b2c3")

    result = store.get_paths_for_recent_message("Alice", channel=2, exclude_command="multipath")
    assert result is not None
    body, paths = result
    assert body == "ping"
    assert paths == ["a1b2c3", "a1d4e5"]


@pytest.mark.asyncio
async def test_get_paths_for_recent_message_returns_none_when_only_command(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    await store.add("in", "Alice", "Alice: multipath", timestamp=120.0, channel=2, path="a1b2c3")

    assert store.get_paths_for_recent_message("Alice", channel=2, exclude_command="multipath") is None


@pytest.mark.asyncio
async def test_get_paths_for_recent_message_scopes_by_channel(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    await store.add("in", "Alice", "Alice: hello-ch3", timestamp=100.0, channel=3, path="ff")
    await store.add("in", "Alice", "Alice: hello-ch2", timestamp=101.0, channel=2, path="aa")
    await store.add("in", "Alice", "Alice: multipath", timestamp=120.0, channel=2, path="aa")

    result = store.get_paths_for_recent_message("Alice", channel=2, exclude_command="multipath")
    assert result is not None
    body, _ = result
    assert body == "hello-ch2"


@pytest.mark.asyncio
async def test_multipath_command_disabled_by_default_in_new_channel_config(tmp_path: Path) -> None:
    """Newly-created ChannelConfig must not auto-enable multipath."""
    ch = ChannelConfig(id=2)
    assert "multipath" not in ch.enabled_commands


@pytest.mark.asyncio
async def test_multipath_command_replies_with_paths_for_prior_message(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    bot, commands = _make_bot(store, [
        ChannelConfig(
            id=2,
            enabled_commands=["ping", "multipath"],
            rate_limit_enabled=False,
        ),
    ])

    # Two arrivals of the same "ping" via different paths.
    await store.add("in", "Alice", "Alice: ping", timestamp=100.0, channel=2, path="a1b2c3")
    await store.add("in", "Alice", "Alice: ping", timestamp=101.0, channel=2, path="d4e5f6")

    event = SimpleNamespace(payload={"text": "Alice: multipath", "channel_idx": 2})
    await bot._on_channel_msg(event)

    assert len(commands.sent) == 1
    channel_id, reply = commands.sent[0]
    assert channel_id == 2
    assert reply.startswith('@[Alice] "ping" 2 paths:')
    assert "a1:b2:c3 (3)" in reply
    assert "d4:e5:f6 (3)" in reply


@pytest.mark.asyncio
async def test_multipath_command_disabled_does_not_reply(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    # multipath NOT in enabled_commands.
    bot, commands = _make_bot(store, [
        ChannelConfig(
            id=2,
            enabled_commands=["ping"],
            rate_limit_enabled=False,
        ),
    ])

    await store.add("in", "Alice", "Alice: ping", timestamp=100.0, channel=2, path="a1b2c3")

    event = SimpleNamespace(payload={"text": "Alice: multipath", "channel_idx": 2})
    await bot._on_channel_msg(event)

    assert commands.sent == []


@pytest.mark.asyncio
async def test_multipath_command_with_no_prior_message(tmp_path: Path) -> None:
    store = MessageStore(tmp_path / "messages.db")
    await store.load()

    bot, commands = _make_bot(store, [
        ChannelConfig(
            id=2,
            enabled_commands=["multipath"],
            rate_limit_enabled=False,
        ),
    ])

    event = SimpleNamespace(payload={"text": "Alice: multipath", "channel_idx": 2})
    await bot._on_channel_msg(event)

    assert commands.sent == [(2, "@[Alice] no recent message to trace")]
