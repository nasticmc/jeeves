"""Write configuration back to TOML file."""

from __future__ import annotations

from pathlib import Path

import tomli_w

from .schema import AppConfig


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Serialize the current config to a TOML file."""
    target = path or config.config_path
    if target is None:
        raise ValueError("No config file path specified")

    data = config.model_dump(exclude={"config_path"}, mode="json")

    # Convert Path objects to strings for TOML serialization
    if "bot" in data and "repeaters_file" in data["bot"]:
        data["bot"]["repeaters_file"] = str(data["bot"]["repeaters_file"])
    if "logging" in data and data["logging"].get("file"):
        data["logging"]["file"] = str(data["logging"]["file"])

    with open(target, "wb") as f:
        tomli_w.dump(data, f)
