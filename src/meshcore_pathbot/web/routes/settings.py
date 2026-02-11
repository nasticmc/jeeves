"""Settings route — configuration editor."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from ...config.schema import AppConfig
from ...config.writer import save_config
from ...events.bus import EventBus
from ...events.types import AppEvent
from ..dependencies import get_bus, get_config

router = APIRouter()


@router.get("/settings")
async def settings_page(
    request: Request,
    config: AppConfig = Depends(get_config),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "config": config},
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    config: AppConfig = Depends(get_config),
    bus: EventBus = Depends(get_bus),
    connection_type: str = Form("serial"),
    serial_port: str = Form(""),
    serial_baud: int = Form(115200),
    tcp_host: str = Form(""),
    tcp_port: int = Form(5000),
    ble_address: str = Form(""),
    channel: int = Form(2),
    ignore_list: str = Form(""),
    home_repeater_name: str = Form(""),
    home_repeater_prefix: str = Form(""),
    bot_lat: float = Form(0.0),
    bot_lon: float = Form(0.0),
    web_host: str = Form("0.0.0.0"),
    web_port: int = Form(8075),
    log_level: str = Form("INFO"),
):
    templates = request.app.state.templates

    # Update config in-place
    config.connection.type = connection_type  # type: ignore[assignment]
    config.connection.serial_port = serial_port or None
    config.connection.serial_baud = serial_baud
    config.connection.tcp_host = tcp_host or None
    config.connection.tcp_port = tcp_port
    config.connection.ble_address = ble_address or None
    config.bot.channel = channel
    config.bot.ignore_list = [
        s.strip() for s in ignore_list.split(",") if s.strip()
    ]
    config.bot.home_repeater_name = home_repeater_name.strip()
    config.bot.home_repeater_prefix = home_repeater_prefix.strip().lower()
    config.bot.lat = bot_lat
    config.bot.lon = bot_lon
    config.web.host = web_host
    config.web.port = web_port
    config.logging.level = log_level

    # Save to file if a config path is set
    if config.config_path:
        try:
            save_config(config, config.config_path)
        except Exception as e:
            return templates.TemplateResponse(
                "partials/toast.html",
                {"request": request, "message": f"Error saving: {e}", "type": "error"},
            )

    await bus.publish(AppEvent.CONFIG_UPDATE, {"saved": True})

    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": "Settings saved", "type": "success"},
    )
