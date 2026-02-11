"""PathBot: MeshCore connection, event handling, and command dispatch."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from meshcore import EventType, MeshCore

from ..config.schema import AppConfig
from ..events.bus import EventBus
from ..events.types import AppEvent
from . import _reader_patch
from .message_store import MessageStore
from .path_resolver import PathResolver
from .repeater_db import RepeaterDB

# Fix meshcore channel message parsing to extract path bytes.
_reader_patch.apply()

log = logging.getLogger("pathbot.bot")


@dataclass
class BotStats:
    """Mutable statistics container."""

    start_time: float = field(default_factory=time.time)
    messages_in: int = 0
    messages_out: int = 0
    commands_processed: int = 0
    errors: int = 0
    last_message_at: float | None = None

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "uptime_seconds": int(self.uptime_seconds),
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "commands_processed": self.commands_processed,
            "errors": self.errors,
            "last_message_at": self.last_message_at,
        }


class PathBot:
    """Core bot: connects to MeshCore, handles trace/ping commands."""

    def __init__(self, config: AppConfig, db: RepeaterDB, bus: EventBus, message_store: MessageStore):
        self.config = config
        self.db = db
        self.bus = bus
        self.message_store = message_store
        self.resolver = PathResolver(db)
        self.stats = BotStats()
        self._mc: MeshCore | None = None

    @property
    def is_connected(self) -> bool:
        return self._mc is not None

    async def start(self) -> None:
        """Connect to MeshCore, sync contacts, subscribe to events, start auto-fetching."""
        log.info("Starting PathBot...")

        self._mc = await self._connect()
        await self.bus.publish(AppEvent.BOT_CONNECTED)

        await self._sync_contacts()

        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)
        self._mc.subscribe(
            EventType.CHANNEL_MSG_RECV,
            self._on_channel_msg,
            attribute_filters={"channel_idx": self.config.bot.channel},
        )

        await self._mc.start_auto_message_fetching()

        log.info(
            f"PathBot running on channel {self.config.bot.channel} | "
            f"Repeater DB: {self.db.stats_str()}"
        )

    async def stop(self) -> None:
        """Gracefully disconnect from MeshCore."""
        if self._mc:
            try:
                await self._mc.stop_auto_message_fetching()
                await self._mc.disconnect()
            except Exception as e:
                log.warning(f"Error during disconnect: {e}")
            self._mc = None
            await self.bus.publish(AppEvent.BOT_DISCONNECTED)
            log.info("Disconnected from MeshCore")

    async def _connect(self) -> MeshCore:
        """Create MeshCore connection based on config."""
        conn = self.config.connection
        debug = self.config.logging.level == "DEBUG"

        log.info(f"Connecting via {conn.type}...")

        if conn.type == "serial":
            if not conn.serial_port:
                raise ValueError("Serial port not configured")
            return await MeshCore.create_serial(conn.serial_port, conn.serial_baud, debug=debug)
        elif conn.type == "tcp":
            if not conn.tcp_host:
                raise ValueError("TCP host not configured")
            return await MeshCore.create_tcp(conn.tcp_host, conn.tcp_port, debug=debug)
        elif conn.type == "ble":
            if conn.ble_address:
                return await MeshCore.create_ble(conn.ble_address, debug=debug)
            else:
                return await MeshCore.create_ble(debug=debug)
        else:
            raise ValueError(f"Unknown connection type: {conn.type}")

    async def _sync_contacts(self) -> None:
        """Pull contacts list and seed repeater DB."""
        log.info("Syncing contacts...")
        result = await self._mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            log.error(f"Failed to get contacts: {result.payload}")
            return

        contacts = result.payload
        count = 0
        for key, contact in contacts.items():
            if "public_key" not in contact:
                contact["public_key"] = key
            await self.db.update_from_contact(contact)
            count += 1

        log.info(f"Synced {count} contacts, {self.db.count} repeaters in DB")

    @staticmethod
    def _extract_advert_pub_key(payload: object) -> str | None:
        """Extract a public key string from MeshCore advert payload variants."""
        if isinstance(payload, str):
            return payload

        if isinstance(payload, dict):
            for key_name in ("public_key", "pub_key", "sender", "node_id"):
                value = payload.get(key_name)
                if isinstance(value, str) and value:
                    return value

        return None

    async def _on_advert(self, event) -> None:
        """Handle incoming advertisement — update repeater DB."""
        pub_key = self._extract_advert_pub_key(event.payload)
        log.debug(f"Advert received from: {event.payload}")

        if not pub_key:
            log.debug(f"Ignoring advert with unsupported payload type: {type(event.payload).__name__}")
            return

        result = await self._mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            log.warning(f"Could not refresh contacts after advert: {result.payload}")
            return

        contacts = result.payload
        if not isinstance(contacts, dict):
            log.warning(f"Unexpected contacts payload type: {type(contacts).__name__}")
            return

        contact = contacts.get(pub_key)

        # Some backends key contacts by a non-public-key ID; fall back to scan.
        if not contact:
            for key, candidate in contacts.items():
                if not isinstance(candidate, dict):
                    continue
                candidate_key = candidate.get("public_key", key)
                if candidate_key == pub_key:
                    contact = candidate
                    break

        if not isinstance(contact, dict):
            log.debug(f"Advert source {pub_key} not found in contacts payload")
            return

        if "public_key" not in contact:
            contact["public_key"] = pub_key

        entry = await self.db.update_from_contact(contact)
        if entry:
            await self.bus.publish(AppEvent.REPEATER_UPDATE, entry)

    @staticmethod
    def _parse_sender(data: dict) -> tuple[str, str]:
        """Extract sender name and message body from channel message payload.

        MeshCore channel messages embed the sender in the text field as "Name: message".
        Falls back to sender_name/pubkey_prefix fields if available.

        Returns (sender_name, message_body).
        """
        text = data.get("text", "")

        # Try explicit fields first
        sender = data.get("sender_name", data.get("pubkey_prefix", ""))
        if sender:
            return sender, text

        # Parse "Name: message" format from text field
        if ": " in text:
            sender, body = text.split(": ", 1)
            return sender.strip(), body.strip()

        # No colon — entire text is the message, sender unknown
        return "???", text

    def _build_paths_reply(self, sender: str) -> str:
        """Build reply for the 'paths' command — unique paths seen from this sender."""
        raw_paths = self.message_store.get_paths_for_peer(sender)

        if not raw_paths:
            return f"@[{sender}] no paths recorded"

        formatted = []
        for rp in raw_paths:
            if not rp or len(rp) < 2 or len(rp) % 2 != 0:
                formatted.append("direct")
            else:
                hop_count = len(rp) // 2
                split = ":".join(rp[i:i + 2] for i in range(0, len(rp), 2))
                formatted.append(f"{split} ({hop_count})")

        # Deduplicate (empty paths all become "direct")
        seen: list[str] = []
        for f in formatted:
            if f not in seen:
                seen.append(f)

        return f"@[{sender}] {len(seen)} paths: {', '.join(seen)}"

    async def _on_channel_msg(self, event) -> None:
        """Handle incoming channel message — check for trace, ping, or paths."""
        data = event.payload
        sender, msg_body = self._parse_sender(data)
        text = data.get("text", "")
        raw_path = data.get("path", "")
        path_len = data.get("path_len", 0)
        raw_rxlog = data.get("rxlog", "")

        log.debug(f"Channel msg from {sender}: {text} (path={raw_path}, path_len={path_len})")

        self.stats.messages_in += 1
        self.stats.last_message_at = time.time()
        ts = time.time()

        await self.message_store.add(
            "in", sender, text, ts, self.config.bot.channel, path=raw_path, rxlog=str(raw_rxlog),
        )
        await self.bus.publish(
            AppEvent.MSG_IN,
            {"sender": sender, "text": text, "timestamp": ts},
        )

        # Check ignore list
        sender_lower = sender.lower()
        ignore_list = [n.lower() for n in self.config.bot.ignore_list]
        if any(ignored in sender_lower for ignored in ignore_list):
            log.debug(f"Ignoring message from {sender} (in ignore list)")
            return

        body_lower = msg_body.lower()
        is_trace = "trace" in body_lower
        is_ping = "ping" in body_lower
        is_paths = "paths" in body_lower
        is_prefix = body_lower.startswith("prefix")

        if not is_trace and not is_ping and not is_paths and not is_prefix:
            return

        self.stats.commands_processed += 1

        # Handle prefix command — look up repeater names from hex prefixes
        if is_prefix:
            log.info(f"Prefix lookup from {sender}")
            # Extract hex argument after "prefix" keyword
            hex_arg = msg_body[len("prefix"):].strip()
            if hex_arg:
                lookup = self.resolver.lookup_prefixes(hex_arg)
                if lookup:
                    reply = f"@[{sender}] {lookup}"
                else:
                    reply = f"@[{sender}] invalid prefix string"
            else:
                reply = f"@[{sender}] usage: prefix <hex> (e.g. prefix fb:1f:7a)"
        # Handle paths command
        elif is_paths:
            log.info(f"Paths from {sender}")
            reply = self._build_paths_reply(sender)
        # Handle trace command
        elif is_trace:
            log.info(f"Trace from {sender}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                resolved = self.resolver.resolve(raw_path)
                reply = f"@[{sender}] {resolved}"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops, no path detail)"
            else:
                reply = f"@[{sender}] rxed (no path data)"
        # Handle ping command
        else:
            log.info(f"Ping from {sender}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                raw_fmt = self.resolver.raw(raw_path)
                reply = f"@[{sender}] {raw_fmt}"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops)"
            else:
                reply = f"@[{sender}] rxed"

        log.info(f"Replying: {reply}")

        result = await self._mc.commands.send_chan_msg(self.config.bot.channel, reply)
        if result.type == EventType.ERROR:
            log.error(f"Failed to send reply: {result.payload}")
            self.stats.errors += 1
            await self.bus.publish(AppEvent.ERROR, {"message": f"Send failed: {result.payload}"})
            return

        self.stats.messages_out += 1
        out_ts = time.time()
        await self.message_store.add(
            "out", sender, reply, out_ts, self.config.bot.channel,
        )
        await self.bus.publish(
            AppEvent.MSG_OUT,
            {"recipient": sender, "text": reply, "timestamp": out_ts},
        )
        await self.bus.publish(AppEvent.STATS_UPDATE, self.stats.to_dict())
