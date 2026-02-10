"""Persistent JSON-backed repeater database."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("pathbot.repeater_db")

# MeshCore ADV_TYPE for repeaters
ADV_TYPE_REPEATER = 2


class RepeaterDB:
    """Thread/async-safe persistent store of known repeater nodes.

    Each entry is keyed by the full public_key hex string and contains:
        public_key, prefix, name, lat, lon, type, last_seen
    """

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.nodes: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load repeater data from the JSON file."""
        async with self._lock:
            self._load_sync()

    def _load_sync(self) -> None:
        """Synchronous load (call within lock or at init)."""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                self.nodes = dict(data)
                log.info(f"Loaded {len(self.nodes)} repeaters from {self.filepath}")
            except (json.JSONDecodeError, IOError) as e:
                log.warning(f"Could not load repeaters file: {e}")
                self.nodes = {}
        else:
            log.info("No existing repeaters file, starting fresh")

    async def save(self) -> None:
        """Persist repeater data to the JSON file."""
        async with self._lock:
            self._save_sync()

    def _save_sync(self) -> None:
        """Synchronous save (call within lock)."""
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.nodes, f, indent=2)
        except IOError as e:
            log.error(f"Could not save repeaters file: {e}")

    async def update_from_contact(self, contact: dict) -> dict | None:
        """Update DB from a contact dict. Returns the entry if it was a repeater, else None."""
        pub_key = contact.get("public_key", "")
        adv_type = contact.get("type", contact.get("adv_type", 0))
        name = contact.get("adv_name", "unknown")
        lat = contact.get("adv_lat", 0.0)
        lon = contact.get("adv_lon", 0.0)

        if not pub_key or len(pub_key) < 2:
            return None

        if adv_type != ADV_TYPE_REPEATER:
            return None

        prefix = pub_key[:2].lower()
        entry = {
            "public_key": pub_key,
            "prefix": prefix,
            "name": name,
            "lat": lat,
            "lon": lon,
            "type": adv_type,
            "last_seen": int(time.time()),
        }

        async with self._lock:
            is_new = pub_key not in self.nodes
            self.nodes[pub_key] = entry
            self._save_sync()

        if is_new:
            log.info(f"New repeater: {name} (prefix={prefix}, lat={lat}, lon={lon})")
        else:
            log.debug(f"Updated repeater: {name} (prefix={prefix})")

        return entry

    def get_by_prefix(self, prefix: str) -> list[dict]:
        """Get all repeaters matching a 1-byte hex prefix."""
        prefix = prefix.lower()
        return [n for n in self.nodes.values() if n["prefix"] == prefix]

    def get_all(self) -> list[dict]:
        """Return all repeater entries sorted by name."""
        return sorted(self.nodes.values(), key=lambda n: n.get("name", ""))

    async def delete(self, public_key: str) -> bool:
        """Remove a repeater by public key. Returns True if it existed."""
        async with self._lock:
            if public_key in self.nodes:
                del self.nodes[public_key]
                self._save_sync()
                return True
        return False

    @property
    def count(self) -> int:
        return len(self.nodes)

    def stats(self) -> dict:
        """Return statistics about the repeater DB."""
        prefixes = set(n["prefix"] for n in self.nodes.values())
        collisions = sum(1 for p in prefixes if len(self.get_by_prefix(p)) > 1)
        with_location = sum(
            1 for n in self.nodes.values() if n.get("lat", 0) != 0 or n.get("lon", 0) != 0
        )
        return {
            "total": self.count,
            "unique_prefixes": len(prefixes),
            "collisions": collisions,
            "with_location": with_location,
        }

    def stats_str(self) -> str:
        """Return a formatted stats string."""
        s = self.stats()
        return (
            f"{s['total']} repeaters, "
            f"{s['unique_prefixes']} unique prefixes, "
            f"{s['collisions']} collisions"
        )
