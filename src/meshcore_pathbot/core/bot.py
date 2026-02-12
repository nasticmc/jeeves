"""PathBot: MeshCore connection, event handling, and command dispatch."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from meshcore import EventType, MeshCore

from typing import Any

from ..config.schema import AppConfig
from ..events.bus import EventBus
from ..events.types import AppEvent
from .message_store import MessageStore
from .path_resolver import PathResolver
from .repeater_db import RepeaterDB

log = logging.getLogger("pathbot.bot")


def parse_rx_log_data(payload: Any) -> dict[str, Any]:
    """Parse RX_LOG event payload to extract path details.

    The payload hex format is:
      byte 0: header
      byte 1: path_len
      next path_len bytes: path node prefixes
    """
    result: dict[str, Any] = {}

    hex_str = None
    if isinstance(payload, dict):
        hex_str = payload.get("payload") or payload.get("raw_hex")
    elif isinstance(payload, (str, bytes)):
        hex_str = payload

    if not hex_str:
        return result

    if isinstance(hex_str, bytes):
        hex_str = hex_str.hex()

    hex_str = str(hex_str).lower().replace(" ", "")

    if len(hex_str) < 4:
        return result

    try:
        path_len = int(hex_str[2:4], 16)
    except ValueError:
        return result

    result["path_len"] = path_len

    path_start = 4
    path_end = path_start + (path_len * 2)

    if len(hex_str) < path_end:
        return result

    result["path"] = hex_str[path_start:path_end]
    return result


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
        self.resolver = PathResolver(db, config)
        self.stats = BotStats()
        self._mc: MeshCore | None = None
        self._latest_rx_path: dict[str, Any] = {}

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
        self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log_data)

        # Subscribe to each active channel
        active_channels = self.config.bot.get_active_channels()
        for ch in active_channels:
            self._mc.subscribe(
                EventType.CHANNEL_MSG_RECV,
                self._on_channel_msg,
                attribute_filters={"channel_idx": ch.id},
            )

        await self._mc.start_auto_message_fetching()

        channel_ids = [ch.id for ch in active_channels]
        log.info(
            f"PathBot running on channel(s) {channel_ids} | "
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

        await self.message_store.add_event("advert")

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

    @staticmethod
    def _split_message(text: str, max_len: int = 120) -> list[str]:
        """Split a message into chunks of at most max_len characters, breaking on word boundaries."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find the last space within the limit
            split_at = text.rfind(" ", 0, max_len + 1)
            if split_at <= 0:
                # No space found — hard split
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip(" ")
        return chunks

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

    async def _on_rx_log_data(self, event) -> None:
        """Handle RX_LOG_DATA — extract and cache path info for the next channel message."""
        parsed = parse_rx_log_data(event.payload)
        if parsed:
            self._latest_rx_path = parsed
            log.debug(f"RX log path: {parsed}")

    async def _on_channel_msg(self, event) -> None:
        """Handle incoming channel message — check for trace, ping, or paths."""
        data = event.payload
        sender, msg_body = self._parse_sender(data)
        text = data.get("text", "")

        # Determine which channel this message arrived on
        channel_id = data.get("channel_idx", self.config.bot.channel)

        # Path data comes from the most recent RX_LOG_DATA event (radio log),
        # not from the channel message payload itself.
        rx = self._latest_rx_path
        self._latest_rx_path = {}
        raw_path = rx.get("path", "")
        path_len = rx.get("path_len", data.get("path_len", 0))

        log.debug(f"Channel {channel_id} msg from {sender}: {text} (path={raw_path}, path_len={path_len})")

        self.stats.messages_in += 1
        self.stats.last_message_at = time.time()
        ts = time.time()

        await self.message_store.add(
            "in", sender, text, ts, channel_id, path=raw_path,
        )
        await self.bus.publish(
            AppEvent.MSG_IN,
            {"sender": sender, "text": text, "timestamp": ts, "channel": channel_id},
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

        # Determine which command matched and check per-channel permission
        if is_prefix:
            cmd_name = "prefix"
        elif is_paths:
            cmd_name = "paths"
        elif is_trace:
            cmd_name = "trace"
        else:
            cmd_name = "ping"

        if not self.config.bot.is_command_enabled(channel_id, cmd_name):
            log.debug(f"Command '{cmd_name}' not enabled on channel {channel_id}, ignoring")
            return

        self.stats.commands_processed += 1

        # Handle prefix command — look up repeater names from hex prefixes
        if is_prefix:
            log.info(f"Prefix lookup from {sender} on ch{channel_id}")
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
            log.info(f"Paths from {sender} on ch{channel_id}")
            reply = self._build_paths_reply(sender)
        # Handle trace command
        elif is_trace:
            log.info(f"Trace from {sender} on ch{channel_id}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                resolved = self.resolver.resolve(raw_path)
                reply = f"@[{sender}] {resolved}"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops, no path detail)"
            else:
                reply = f"@[{sender}] rxed (no path data)"
        # Handle ping command
        else:
            log.info(f"Ping from {sender} on ch{channel_id}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                raw_fmt = self.resolver.raw(raw_path)
                reply = f"@[{sender}] {raw_fmt}"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops)"
            else:
                reply = f"@[{sender}] rxed"

        chunks = self._split_message(reply)
        log.info(f"Replying on ch{channel_id} ({len(chunks)} part(s)): {reply}")

        for chunk in chunks:
            result = await self._mc.commands.send_chan_msg(channel_id, chunk)
            if result.type == EventType.ERROR:
                log.error(f"Failed to send reply chunk: {result.payload}")
                self.stats.errors += 1
                await self.bus.publish(AppEvent.ERROR, {"message": f"Send failed: {result.payload}"})
                return
            self.stats.messages_out += 1

        out_ts = time.time()
        await self.message_store.add(
            "out", sender, reply, out_ts, channel_id,
        )
        await self.bus.publish(
            AppEvent.MSG_OUT,
            {"recipient": sender, "text": reply, "timestamp": out_ts, "channel": channel_id},
        )
        await self.bus.publish(AppEvent.STATS_UPDATE, self.stats.to_dict())
