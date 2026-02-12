from __future__ import annotations

from types import SimpleNamespace

import asyncio

from meshcore_pathbot.config.schema import AppConfig, ChannelConfig
from meshcore_pathbot.core.bot import PathBot
from meshcore_pathbot.events.bus import EventBus


class DummyDB:
    pass


class DummyStore:
    async def add(self, *args, **kwargs):
        return None

    async def add_event(self, *args, **kwargs):
        return None

    def get_paths_for_peer(self, peer: str):
        return []


class DummyCommands:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_chan_msg(self, channel_id: int, text: str):
        self.sent.append((channel_id, text))
        return SimpleNamespace(type=None, payload={})


def test_per_user_rate_limit_blocks_second_command_within_timeout() -> None:
    config = AppConfig()
    config.bot.channels = [
        ChannelConfig(
            id=2,
            enabled_commands=["ping"],
            rate_limit_enabled=True,
            rate_limit_seconds=120,
        )
    ]

    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=DummyStore())
    commands = DummyCommands()
    bot._mc = SimpleNamespace(commands=commands)

    event = SimpleNamespace(payload={"text": "Alice: ping", "channel_idx": 2})

    asyncio.run(bot._on_channel_msg(event))
    asyncio.run(bot._on_channel_msg(event))

    assert len(commands.sent) == 1


def test_channel_rate_limit_can_be_disabled() -> None:
    config = AppConfig()
    config.bot.channels = [
        ChannelConfig(
            id=2,
            enabled_commands=["ping"],
            rate_limit_enabled=False,
            rate_limit_seconds=120,
        )
    ]

    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=DummyStore())
    commands = DummyCommands()
    bot._mc = SimpleNamespace(commands=commands)

    event = SimpleNamespace(payload={"text": "Alice: ping", "channel_idx": 2})

    asyncio.run(bot._on_channel_msg(event))
    asyncio.run(bot._on_channel_msg(event))

    assert len(commands.sent) == 2
