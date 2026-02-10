"""Pydantic configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ConnectionConfig(BaseModel):
    """MeshCore device connection settings."""

    type: Literal["serial", "tcp", "ble"] = "serial"
    serial_port: str | None = None
    serial_baud: int = 115200
    tcp_host: str | None = None
    tcp_port: int = 5000
    ble_address: str | None = None
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 10


class BotConfig(BaseModel):
    """Bot behavior settings."""

    channel: int = Field(default=2, ge=0, le=7)
    repeaters_file: Path = Path("repeaters.db")
    ignore_list: list[str] = Field(default_factory=lambda: ["jeeves"])


class WebConfig(BaseModel):
    """Web dashboard settings."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(default=8075, ge=1, le=65535)


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: str = "INFO"
    file: Path | None = None


class AppConfig(BaseModel):
    """Top-level application configuration."""

    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    config_path: Path | None = None
