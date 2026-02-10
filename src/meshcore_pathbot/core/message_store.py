"""Persistent message store — keeps message history across page navigations and restarts."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("pathbot.messages")

MAX_MESSAGES = 1000


class MessageStore:
    """In-memory + JSON-persisted message history.

    Each entry:
        id, direction ("in"/"out"), peer, text, timestamp, channel
    """

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.messages: list[dict] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load message history from JSON file."""
        async with self._lock:
            self._load_sync()

    def _load_sync(self) -> None:
        if self.filepath.exists():
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.messages = data[-MAX_MESSAGES:]
                else:
                    self.messages = []
                log.info(f"Loaded {len(self.messages)} messages from {self.filepath}")
            except (json.JSONDecodeError, IOError) as e:
                log.warning(f"Could not load messages file: {e}")
                self.messages = []
        else:
            log.info("No existing messages file, starting fresh")

    async def save(self) -> None:
        """Persist messages to JSON file."""
        async with self._lock:
            self._save_sync()

    def _save_sync(self) -> None:
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.messages, f, indent=2)
        except IOError as e:
            log.error(f"Could not save messages file: {e}")

    async def add(
        self,
        direction: str,
        peer: str,
        text: str,
        timestamp: float | None = None,
        channel: int | None = None,
    ) -> dict:
        """Add a message to the store. Returns the created entry."""
        ts = timestamp or time.time()
        entry = {
            "id": f"{ts}_{direction}",
            "direction": direction,
            "peer": peer,
            "text": text,
            "timestamp": ts,
            "channel": channel,
        }

        async with self._lock:
            self.messages.append(entry)
            # Cap at MAX_MESSAGES, drop oldest
            if len(self.messages) > MAX_MESSAGES:
                self.messages = self.messages[-MAX_MESSAGES:]
            self._save_sync()

        return entry

    def get_all(self) -> list[dict]:
        """Return all messages sorted by timestamp (newest first)."""
        return sorted(self.messages, key=lambda m: m.get("timestamp", 0), reverse=True)

    def get_recent(self, count: int = 50) -> list[dict]:
        """Return the most recent N messages (newest first)."""
        return self.get_all()[:count]

    async def clear(self) -> None:
        """Clear all messages."""
        async with self._lock:
            self.messages = []
            self._save_sync()

    @property
    def count(self) -> int:
        return len(self.messages)
