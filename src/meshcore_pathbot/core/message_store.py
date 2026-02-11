"""SQLite-backed message store for message history and path tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("pathbot.messages")

MAX_MESSAGES = 1000


class MessageStore:
    """SQLite-persisted message history.

    Each entry:
        id, direction ("in"/"out"), peer, text, timestamp, channel, path, rxlog
    """

    def __init__(self, filepath: Path):
        self.legacy_filepath = filepath if filepath.suffix == ".json" else filepath.with_name("messages.json")
        self.filepath = filepath.with_suffix(".db") if filepath.suffix == ".json" else filepath
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def load(self) -> None:
        """Load message store from SQLite, migrating legacy JSON if needed."""
        async with self._lock:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self._init_db_sync()
            self._migrate_legacy_json_sync()
            self._trim_sync()
            log.info(f"Loaded message store from {self.filepath}")

    def _init_db_sync(self) -> None:
        if self._conn is None:
            self._conn = sqlite3.connect(self.filepath)
            self._conn.row_factory = sqlite3.Row

        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                peer TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp REAL NOT NULL,
                channel INTEGER,
                path TEXT NOT NULL DEFAULT '',
                rxlog TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_peer_direction ON messages(peer, direction);
            CREATE INDEX IF NOT EXISTS idx_messages_peer_path ON messages(peer, path);
            """
        )
        self._ensure_column_sync("rxlog", "TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    def _ensure_column_sync(self, name: str, spec: str) -> None:
        existing_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if name not in existing_cols:
            self._conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {spec}")

    def _migrate_legacy_json_sync(self) -> None:
        if not self.legacy_filepath.exists() or self.legacy_filepath.suffix != ".json":
            return

        row_count = self._conn.execute("SELECT COUNT(1) FROM messages").fetchone()[0]
        if row_count > 0:
            return

        try:
            with open(self.legacy_filepath, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Could not migrate legacy messages JSON: {e}")
            return

        if not isinstance(data, list):
            log.warning("Legacy messages JSON was not a list; skipping migration")
            return

        inserted = 0
        for raw in data:
            if not isinstance(raw, dict):
                continue
            ts = float(raw.get("timestamp", time.time()))
            direction = str(raw.get("direction", "in"))
            peer = str(raw.get("peer", ""))
            text = str(raw.get("text", ""))
            channel = raw.get("channel")
            path = str(raw.get("path", ""))
            rxlog = str(raw.get("rxlog", ""))
            msg_id = str(raw.get("id", f"{time.time_ns()}_{direction}"))
            self._insert_sync(
                {
                    "id": msg_id,
                    "direction": direction,
                    "peer": peer,
                    "text": text,
                    "timestamp": ts,
                    "channel": channel,
                    "path": path,
                    "rxlog": rxlog,
                }
            )
            inserted += 1

        self._conn.commit()
        self._trim_sync()
        log.info(
            f"Migrated {inserted} messages from legacy JSON {self.legacy_filepath} -> {self.filepath}"
        )

    def _insert_sync(self, entry: dict) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO messages
            (id, direction, peer, text, timestamp, channel, path, rxlog)
            VALUES (:id, :direction, :peer, :text, :timestamp, :channel, :path, :rxlog)
            """,
            entry,
        )

    def _trim_sync(self) -> None:
        total = self._conn.execute("SELECT COUNT(1) FROM messages").fetchone()[0]
        excess = total - MAX_MESSAGES
        if excess <= 0:
            return

        self._conn.execute(
            """
            DELETE FROM messages
            WHERE id IN (
                SELECT id
                FROM messages
                ORDER BY timestamp ASC
                LIMIT ?
            )
            """,
            (excess,),
        )
        self._conn.commit()

    async def save(self) -> None:
        """No-op for API compatibility; updates are persisted immediately."""
        return

    async def add(
        self,
        direction: str,
        peer: str,
        text: str,
        timestamp: float | None = None,
        channel: int | None = None,
        path: str = "",
        rxlog: str = "",
    ) -> dict:
        """Add a message to the store. Returns the created entry."""
        ts = float(timestamp or time.time())
        entry = {
            "id": f"{time.time_ns()}_{direction}",
            "direction": direction,
            "peer": peer,
            "text": text,
            "timestamp": ts,
            "channel": channel,
            "path": path,
            "rxlog": rxlog,
        }

        async with self._lock:
            self._insert_sync(entry)
            self._trim_sync()
            self._conn.commit()

        return entry

    def get_all(self) -> list[dict]:
        """Return all messages sorted by timestamp (newest first)."""
        rows = self._conn.execute(
            """
            SELECT id, direction, peer, text, timestamp, channel, path, rxlog
            FROM messages
            ORDER BY timestamp DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent(self, count: int = 50) -> list[dict]:
        """Return the most recent N messages (newest first)."""
        rows = self._conn.execute(
            """
            SELECT id, direction, peer, text, timestamp, channel, path, rxlog
            FROM messages
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (count,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_unique_peers(self) -> list[str]:
        """Return a sorted list of unique peer names that have sent messages."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT peer
            FROM messages
            WHERE direction = 'in' AND peer != ''
            ORDER BY peer ASC
            """
        ).fetchall()
        return [str(row["peer"]) for row in rows]

    def get_paths_for_peer(self, peer: str) -> list[str]:
        """Return unique raw path hex strings seen from a given peer.

        Returns a list of unique path strings (e.g. ["fb1f7a", "a1b2"]).
        Empty string paths are tracked as "direct".
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT COALESCE(path, '') AS path
            FROM messages
            WHERE direction = 'in' AND LOWER(peer) = LOWER(?)
            ORDER BY path ASC
            """,
            (peer,),
        ).fetchall()
        return [str(row["path"]) for row in rows]

    async def clear(self) -> None:
        """Clear all messages."""
        async with self._lock:
            self._conn.execute("DELETE FROM messages")
            self._conn.commit()

    @property
    def count(self) -> int:
        if self._conn is None:
            return 0
        return int(self._conn.execute("SELECT COUNT(1) FROM messages").fetchone()[0])
