from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshcore_pathbot.config.loader import load_config
from meshcore_pathbot.config.schema import AppConfig, ConnectionConfig
from meshcore_pathbot.core.bot import PathBot
from meshcore_pathbot.web.routes.settings import ALL_COMMANDS
from meshcore_pathbot.events.bus import EventBus


class DummyDB:
    pass


class DummyStore:
    pass


def test_cli_tcp_selects_tcp_connection_and_port() -> None:
    args = SimpleNamespace(
        serial=None,
        tcp="192.168.1.100",
        ble=None,
        port=5001,
        baud=None,
        channel=None,
        repeaters_file=None,
        ignore=[],
        web_host=None,
        web_port=None,
        no_web=False,
        debug=False,
    )

    config = load_config(None, args)

    assert config.connection.type == "tcp"
    assert config.connection.tcp_host == "192.168.1.100"
    assert config.connection.tcp_port == 5001


def test_settings_command_list_includes_multipath() -> None:
    assert "multipath" in ALL_COMMANDS


@pytest.mark.asyncio
async def test_bot_connects_to_tcp_companion_with_reconnect_options(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_create_tcp(host: str, port: int, **kwargs):
        calls.append({"host": host, "port": port, **kwargs})
        return SimpleNamespace()

    monkeypatch.setattr("meshcore_pathbot.core.bot.MeshCore.create_tcp", fake_create_tcp)

    config = AppConfig(
        connection=ConnectionConfig(
            type="tcp",
            tcp_host="meshcore.local",
            tcp_port=5001,
            auto_reconnect=True,
            max_reconnect_attempts=7,
        )
    )
    config.logging.level = "DEBUG"
    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=DummyStore())

    await bot._connect()

    assert calls == [
        {
            "host": "meshcore.local",
            "port": 5001,
            "debug": True,
            "auto_reconnect": True,
            "max_reconnect_attempts": 7,
        }
    ]


@pytest.mark.asyncio
async def test_bot_requires_tcp_host_for_tcp_connection() -> None:
    config = AppConfig(connection=ConnectionConfig(type="tcp", tcp_host=None))
    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=DummyStore())

    with pytest.raises(ValueError, match="TCP host not configured"):
        await bot._connect()
