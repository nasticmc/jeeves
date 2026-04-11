"""Tests for lightning alert loop and daily forecast broadcast."""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshcore_pathbot.config.schema import AppConfig, ChannelConfig
from meshcore_pathbot.core.bot import PathBot
from meshcore_pathbot.core.weather import (
    LIGHTNING_CODES,
    check_lightning,
    forecast_reply,
    forecast_broadcast,
)
from meshcore_pathbot.events.bus import EventBus


# ── Shared test helpers ────────────────────────────────────────────────────────

class DummyDB:
    pass


class DummyStore:
    def __init__(self):
        self.records: list = []

    async def add(self, *args, **kwargs):
        self.records.append(args)

    async def add_event(self, *args, **kwargs):
        pass

    def get_paths_for_peer(self, peer: str):
        return []


class DummyCommands:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_chan_msg(self, channel_id: int, text: str):
        self.sent.append((channel_id, text))
        return SimpleNamespace(type=None, payload={})


def _make_bot(*, lat=0.0, lon=0.0, lightning_enabled=False, lightning_channels=None,
              lightning_interval=1, forecast_enabled=False, forecast_channels=None,
              forecast_hour=6) -> tuple[PathBot, DummyCommands, DummyStore]:
    config = AppConfig()
    config.bot.lat = lat
    config.bot.lon = lon
    config.bot.channels = [ChannelConfig(id=1, enabled_commands=["trace"], rate_limit_enabled=False)]
    config.bot.lightning_alert_enabled = lightning_enabled
    config.bot.lightning_alert_channels = lightning_channels or []
    config.bot.lightning_alert_interval_minutes = lightning_interval
    config.bot.daily_forecast_enabled = forecast_enabled
    config.bot.daily_forecast_channels = forecast_channels or []
    config.bot.daily_forecast_hour = forecast_hour

    store = DummyStore()
    bot = PathBot(config=config, db=DummyDB(), bus=EventBus(), message_store=store)
    commands = DummyCommands()
    bot._mc = SimpleNamespace(commands=commands)
    return bot, commands, store


# ── check_lightning (weather.py) ───────────────────────────────────────────────

def test_check_lightning_returns_true_for_thunderstorm_codes():
    for code in LIGHTNING_CODES:
        data = {"current": {"weather_code": code, "temperature_2m": 25.0}}
        with patch(
            "meshcore_pathbot.core.weather.get_current_weather",
            new=AsyncMock(return_value=data),
        ):
            result = asyncio.run(check_lightning(-38.0, 145.0))
        assert result is True, f"Expected True for WMO code {code}"


def test_check_lightning_returns_false_for_non_storm():
    data = {"current": {"weather_code": 2, "temperature_2m": 20.0}}  # Partly cloudy
    with patch(
        "meshcore_pathbot.core.weather.get_current_weather",
        new=AsyncMock(return_value=data),
    ):
        result = asyncio.run(check_lightning(-38.0, 145.0))
    assert result is False


def test_check_lightning_returns_false_on_api_error():
    with patch(
        "meshcore_pathbot.core.weather.get_current_weather",
        new=AsyncMock(side_effect=Exception("network error")),
    ):
        result = asyncio.run(check_lightning(-38.0, 145.0))
    assert result is False


def test_check_lightning_returns_false_when_code_missing():
    data = {"current": {}}
    with patch(
        "meshcore_pathbot.core.weather.get_current_weather",
        new=AsyncMock(return_value=data),
    ):
        result = asyncio.run(check_lightning(-38.0, 145.0))
    assert result is False


def test_check_lightning_returns_true_when_nearby_sample_has_storm():
    async def fake_current_weather(lat, lon):
        # Simulate no storm at center, storm at first nearby sample point.
        if lat > -38.0 and lon == 145.0:
            return {"current": {"weather_code": 95}}
        return {"current": {"weather_code": 2}}

    with patch(
        "meshcore_pathbot.core.weather.get_current_weather",
        new=AsyncMock(side_effect=fake_current_weather),
    ):
        result = asyncio.run(check_lightning(-38.0, 145.0))
    assert result is True


def test_check_lightning_returns_false_when_all_samples_clear():
    with patch(
        "meshcore_pathbot.core.weather.get_current_weather",
        new=AsyncMock(return_value={"current": {"weather_code": 2}}),
    ):
        result = asyncio.run(check_lightning(-38.0, 145.0))
    assert result is False


# ── forecast_broadcast (weather.py) ───────────────────────────────────────────

def test_forecast_broadcast_no_sender_prefix():
    data = {
        "daily": {
            "time": ["2026-03-01"],
            "weather_code": [2],
            "temperature_2m_max": [25.0],
            "temperature_2m_min": [15.0],
        }
    }
    with patch(
        "meshcore_pathbot.core.weather.get_forecast",
        new=AsyncMock(return_value=data),
    ):
        result = asyncio.run(forecast_broadcast(-38.0, 145.0, "Hampton Park"))
    assert result.startswith("Hampton Park")
    assert "@[" not in result
    assert "3-day forecast" in result


def test_forecast_broadcast_unavailable_on_empty_data():
    data = {"daily": {"time": []}}
    with patch(
        "meshcore_pathbot.core.weather.get_forecast",
        new=AsyncMock(return_value=data),
    ):
        result = asyncio.run(forecast_broadcast(-38.0, 145.0, "Hampton Park"))
    assert "unavailable" in result.lower()


def test_forecast_reply_tolerates_partial_daily_data():
    data = {
        "daily": {
            "time": ["2026-03-01", "2026-03-02"],
            "weather_code": [2, None],
            "temperature_2m_max": [25.0, None],
            "temperature_2m_min": [15.0],
        }
    }
    with patch(
        "meshcore_pathbot.core.weather.get_forecast",
        new=AsyncMock(return_value=data),
    ):
        result = asyncio.run(forecast_reply("Alice", -38.0, 145.0, "Hampton Park"))
    assert result.startswith("@[Alice] Hampton Park 3-day:")
    assert "Partly cloudy" in result
    assert "?" in result


# ── send_channel_message ───────────────────────────────────────────────────────

def test_send_channel_message_returns_true_on_success():
    bot, commands, _ = _make_bot()
    result = asyncio.run(bot.send_channel_message(1, "Hello"))
    assert result is True
    assert len(commands.sent) == 1
    assert commands.sent[0] == (1, "Hello")


def test_send_channel_message_returns_false_when_disconnected():
    bot, _, _ = _make_bot()
    bot._mc = None
    result = asyncio.run(bot.send_channel_message(1, "Hello"))
    assert result is False


def test_send_channel_message_returns_false_on_error():
    from meshcore import EventType

    bot, commands, _ = _make_bot()

    async def bad_send(ch, text):
        return SimpleNamespace(type=EventType.ERROR, payload="fail")

    bot._mc.commands.send_chan_msg = bad_send
    result = asyncio.run(bot.send_channel_message(1, "Hello"))
    assert result is False


# ── _lightning_alert_loop ──────────────────────────────────────────────────────

def test_lightning_alert_loop_exits_when_location_unset():
    """Loop should return immediately if lat/lon are 0,0."""
    bot, commands, _ = _make_bot(lightning_enabled=True, lat=0.0, lon=0.0)
    # Should complete without sleeping (no location configured)
    asyncio.run(asyncio.wait_for(bot._lightning_alert_loop(), timeout=1.0))
    assert len(commands.sent) == 0


def test_lightning_alert_loop_sends_alert_when_storm_detected():
    """Loop should send alert when storm is first detected."""
    bot, commands, store = _make_bot(
        lat=-38.0, lon=145.0, lightning_enabled=True, lightning_channels=[1]
    )

    call_count = 0

    async def fake_check(lat, lon):
        nonlocal call_count
        call_count += 1
        return True  # storm active

    async def run_one_iteration():
        # Patch asyncio.sleep to advance once then cancel
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with (
            patch("meshcore_pathbot.core.bot.weather_svc.check_lightning", new=fake_check),
            patch("asyncio.sleep", new=fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await bot._lightning_alert_loop()

    asyncio.run(run_one_iteration())
    # Should have sent a lightning alert to channel 1
    assert any("Weather alert" in text for _, text in commands.sent)


def test_lightning_all_clear_sent_when_storm_passes():
    """After a storm, when conditions clear the loop should send an all-clear."""
    bot, commands, store = _make_bot(
        lat=-38.0, lon=145.0, lightning_enabled=True, lightning_channels=[1]
    )

    call_count = 0
    # First call: storm, second call: clear
    responses = [True, False]

    async def fake_check(lat, lon):
        nonlocal call_count
        result = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return result

    async def run_two_iterations():
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            # Allow enough loop turns for both storm-detected and all-clear paths.
            if sleep_calls >= 5:
                raise asyncio.CancelledError()

        with (
            patch("meshcore_pathbot.core.bot.weather_svc.check_lightning", new=fake_check),
            patch("asyncio.sleep", new=fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await bot._lightning_alert_loop()

    asyncio.run(run_two_iterations())
    sent_texts = [text for _, text in commands.sent]
    assert any("Weather alert" in t for t in sent_texts)
    assert any("all-clear" in t.lower() for t in sent_texts)


def test_lightning_alert_uses_all_active_channels_when_none_configured():
    """When lightning_alert_channels is empty, all active channels should be alerted."""
    bot, commands, _ = _make_bot(
        lat=-38.0, lon=145.0, lightning_enabled=True, lightning_channels=[]
    )
    # Active channel is 1 from _make_bot
    call_count = 0

    async def fake_check(lat, lon):
        nonlocal call_count
        call_count += 1
        return True

    async def run_one_iteration():
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        with (
            patch("meshcore_pathbot.core.bot.weather_svc.check_lightning", new=fake_check),
            patch("asyncio.sleep", new=fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await bot._lightning_alert_loop()

    asyncio.run(run_one_iteration())
    # Channel 1 is the only active channel
    assert any(ch == 1 for ch, _ in commands.sent)


# ── _daily_forecast_loop ───────────────────────────────────────────────────────

def test_daily_forecast_loop_sends_broadcast():
    """Daily forecast loop should send a forecast broadcast on the configured channel."""
    bot, commands, store = _make_bot(
        forecast_enabled=True, forecast_channels=[1], forecast_hour=6
    )

    fake_msg = "Hampton Park 3-day forecast: Sun 15-22°C Partly cloudy"

    async def run_one_cycle():
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        now_fixed = datetime.datetime(2026, 3, 1, 5, 55, 0,
                                      tzinfo=datetime.timezone.utc)

        with (
            patch("meshcore_pathbot.core.bot.weather_svc.forecast_broadcast",
                  new=AsyncMock(return_value=fake_msg)),
            patch("asyncio.sleep", new=fake_sleep),
            patch("datetime.datetime") as mock_dt,
        ):
            # Make datetime.datetime.now() return a time just before 6am
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            with pytest.raises(asyncio.CancelledError):
                await bot._daily_forecast_loop()

    asyncio.run(run_one_cycle())
    assert any(fake_msg in text for _, text in commands.sent)


def test_daily_forecast_skips_when_disconnected():
    """If the bot is not connected, the daily forecast should not try to send."""
    bot, commands, _ = _make_bot(
        forecast_enabled=True, forecast_channels=[1], forecast_hour=6
    )
    bot._mc = None  # Simulate disconnected state

    async def run_one_cycle():
        sleep_calls = 0

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", new=fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await bot._daily_forecast_loop()

    asyncio.run(run_one_cycle())
    assert len(commands.sent) == 0


# ── Config schema defaults ─────────────────────────────────────────────────────

def test_lightning_and_forecast_disabled_by_default():
    config = AppConfig()
    assert config.bot.lightning_alert_enabled is False
    assert config.bot.daily_forecast_enabled is False
    assert config.bot.lightning_alert_channels == []
    assert config.bot.daily_forecast_channels == []
    assert config.bot.lightning_alert_interval_minutes == 15
    assert config.bot.daily_forecast_hour == 6
