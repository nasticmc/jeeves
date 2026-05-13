"""PathBot: MeshCore connection, event handling, and command dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

from meshcore import EventType, MeshCore

from typing import Any

from ..config.schema import AppConfig
from ..events.bus import EventBus
from ..events.types import AppEvent
from .message_store import MessageStore
from .path_resolver import PathResolver
from .repeater_db import RepeaterDB
from . import weather as weather_svc

log = logging.getLogger("pathbot.bot")

_PROMO_URL = "https://j.eastmesh.au"
_MAX_MSG_LEN = 130
_PING_URL_INTERVAL = 7  # append URL on every Nth ping reply

# How long an RX_LOG_DATA entry stays valid waiting for its CHANNEL_MSG_RECV pair.
# After this, the entry is dropped — the channel message is treated as having no
# path data rather than being attributed to a stale, unrelated packet.
_RX_PATH_TTL_SECONDS = 10.0
# Meshcore payload type constant for channel (flood/group) text messages.
_PAYLOAD_TYPE_CHANNEL_MSG = 0x05

_CMD_PATTERNS: dict[str, re.Pattern[str]] = {
    "ping":     re.compile(r"[Pp]ing$"),
    "trace":    re.compile(r"[Tt]race$"),
    "paths":    re.compile(r"[Pp]aths$"),
    "multipath": re.compile(r"[Mm]ultipath$"),
    "prefix":   re.compile(r"[Pp]refix\b"),
    "weather":  re.compile(r"[Ww]eather(\s+\d{4})?$"),
    "forecast": re.compile(r"[Ff]orecast(\s+\d{4})?$"),
    "help":     re.compile(r"[Hh]elp$"),
}


def parse_rx_log_data(payload: Any) -> dict[str, Any]:
    """Parse RX_LOG event payload to extract path details.

    The payload hex format is:
      byte 0: header
      byte 1: path byte
        - top 2 bits: path hash size per hop (1..4 bytes)
        - lower 6 bits: hop count
      next path_len * path_hash_size bytes: path hashes

    PathBot resolves repeaters using 1-byte prefixes. For multi-byte path
    hashes, we preserve the full path and also derive a compact first-byte
    form for resolver compatibility.
    """
    result: dict[str, Any] = {}

    if isinstance(payload, dict):
        path_len = payload.get("path_len")
        path_hash_size = payload.get("path_hash_size")
        raw_path = payload.get("path")
        chan_hash = payload.get("chan_hash")
        payload_type = payload.get("payload_type")
        if isinstance(path_len, int) and path_len >= 0:
            result["path_len"] = path_len
        if isinstance(path_hash_size, int) and path_hash_size > 0:
            result["path_hash_size"] = path_hash_size
        if isinstance(raw_path, str):
            result["full_path"] = raw_path.lower().replace(" ", "")
        if isinstance(chan_hash, str) and chan_hash:
            result["chan_hash"] = chan_hash.lower()
        if isinstance(payload_type, int):
            result["payload_type"] = payload_type

        if "path_len" in result and "path_hash_size" in result and "full_path" in result:
            hop_len = result["path_hash_size"] * 2
            required_len = result["path_len"] * hop_len
            if len(result["full_path"]) >= required_len:
                full_path = result["full_path"][:required_len]
                compact_path = "".join(
                    full_path[i : i + 2]
                    for i in range(0, required_len, hop_len)
                )
                if len(compact_path) == result["path_len"] * 2:
                    result["path"] = compact_path
                    result["full_path"] = full_path
                    return result

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
        path_byte = int(hex_str[2:4], 16)
    except ValueError:
        return result

    path_hash_size = ((path_byte & 0xC0) >> 6) + 1
    path_len = path_byte & 0x3F

    result["path_len"] = path_len
    result["path_hash_size"] = path_hash_size

    path_start = 4
    path_end = path_start + (path_len * path_hash_size * 2)

    if len(hex_str) < path_end:
        return result

    full_path = hex_str[path_start:path_end]
    hop_hex_len = path_hash_size * 2
    result["full_path"] = full_path
    result["path"] = "".join(
        full_path[i : i + 2]
        for i in range(0, len(full_path), hop_hex_len)
    )
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
        # Legacy single-slot fallback used when no channel-hash correlation is
        # available (e.g. tests). Production multi-channel correlation uses
        # _rx_path_by_chan_hash below.
        self._latest_rx_path: dict[str, Any] = {}
        # Per-channel FIFO of recently logged paths, keyed by chan_hash (the
        # 1-byte channel hash meshcore stamps on each flood-msg log entry).
        # This avoids cross-channel contamination of path data when packets
        # for multiple channels arrive interleaved.
        self._rx_path_by_chan_hash: dict[str, deque[tuple[float, dict[str, Any]]]] = {}
        # channel_idx -> chan_hash mapping, populated from the radio at startup.
        self._chan_hash_by_idx: dict[int, str] = {}
        self._last_command_at_by_user: dict[tuple[int, str], float] = {}
        self._daily_forecast_task: asyncio.Task | None = None
        self._contact_purge_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._ping_reply_count: int = 0
        self._pending_url: bool = False

    @property
    def is_connected(self) -> bool:
        return self._mc is not None

    async def start(self) -> None:
        """Connect to MeshCore, sync contacts, subscribe to events, start auto-fetching."""
        log.info("Starting PathBot...")

        self._mc = await self._connect()
        await self.bus.publish(AppEvent.BOT_CONNECTED)

        await self._sync_contacts()

        # Subscribe to each active channel
        active_channels = self.config.bot.get_active_channels()
        await self._sync_channel_hashes(active_channels)

        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)
        self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log_data)
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

        if self.config.bot.daily_forecast_enabled:
            self._daily_forecast_task = asyncio.create_task(
                self._daily_forecast_loop(), name="daily-forecast"
            )
            log.info("Daily forecast task started")

        self._contact_purge_task = asyncio.create_task(
            self._contact_purge_loop(), name="contact-purge"
        )
        log.info("Hourly contact purge task started")

    async def stop(self) -> None:
        """Gracefully disconnect from MeshCore."""
        for task in (self._daily_forecast_task, self._contact_purge_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._daily_forecast_task = None
        self._contact_purge_task = None

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

        log.info(f"Synced {count} contacts, {self.db.count} nodes in DB")

    async def _sync_channel_hashes(self, channels) -> None:
        """Fetch channel_hash for each active channel so RX_LOG_DATA entries
        can be correlated to the right channel.

        chan_hash is the 1-byte hash meshcore stamps on each flood-msg log
        record (see MeshcorePacketParser.parsePacketPayload). Without this
        mapping, RX log entries for one channel can be wrongly attributed to
        a message arriving on a different channel.
        """
        self._chan_hash_by_idx = {}
        for ch in channels:
            try:
                result = await self._mc.commands.get_channel(ch.id)
            except Exception as exc:
                log.warning(f"Could not fetch channel {ch.id} for hash mapping: {exc}")
                continue
            if result is None or result.type == EventType.ERROR:
                log.warning(f"Channel {ch.id} hash lookup failed: {getattr(result, 'payload', None)}")
                continue
            info = result.payload
            chan_hash = info.get("channel_hash") if isinstance(info, dict) else None
            if isinstance(chan_hash, str) and chan_hash:
                self._chan_hash_by_idx[ch.id] = chan_hash.lower()
        log.info(f"Channel hash map: {self._chan_hash_by_idx}")

    async def _purge_device_contacts(self) -> None:
        """Remove all contacts from the MeshCore device to force fresh re-discovery."""
        result = await self._mc.commands.get_contacts()
        if result is None or result.type == EventType.ERROR:
            log.warning("Contact purge: failed to fetch contacts from device")
            return

        contacts = result.payload
        if not isinstance(contacts, dict) or not contacts:
            log.debug("Contact purge: no contacts on device to remove")
            return

        removed = 0
        for key, contact in list(contacts.items()):
            pub_key = contact.get("public_key", key) if isinstance(contact, dict) else key
            if not isinstance(pub_key, str) or len(pub_key) < 64:
                continue
            try:
                res = await self._mc.commands.remove_contact(pub_key)
                if res and res.type != EventType.ERROR:
                    removed += 1
                else:
                    log.debug(f"Could not remove contact {pub_key[:8]}...: {getattr(res, 'payload', '')}")
            except Exception as e:
                log.warning(f"Error removing contact {pub_key[:8]}...: {e}")

        log.info(f"Contact purge: removed {removed}/{len(contacts)} contacts from device")
        await self._sync_contacts()

    async def _contact_purge_loop(self) -> None:
        """Delete all device contacts every hour so fresh advertisement data repopulates them."""
        while True:
            await asyncio.sleep(3600)
            if self._mc:
                log.info("Running hourly contact purge...")
                await self._purge_device_contacts()

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

    def _build_multipath_reply(self, sender: str, channel_id: int) -> str:
        """Build reply for the 'multipath' command.

        Reports every distinct path the bot has seen for the sender's most
        recent prior message on this channel (i.e. the message they sent
        just before issuing `multipath`). In a flooded mesh the same message
        can arrive via several routes; this surfaces them all rather than
        just the first one the firmware delivered.
        """
        result = self.message_store.get_paths_for_recent_message(
            sender, channel_id, exclude_command="multipath",
        )
        if not result:
            return f"@[{sender}] no recent message to trace"

        text, raw_paths = result
        if not raw_paths:
            return f"@[{sender}] no paths recorded for last msg"

        formatted: list[str] = []
        for rp in raw_paths:
            if not rp or len(rp) < 2 or len(rp) % 2 != 0:
                formatted.append("direct")
            else:
                hop_count = len(rp) // 2
                split = ":".join(rp[i:i + 2] for i in range(0, len(rp), 2))
                formatted.append(f"{split} ({hop_count})")

        # Preserve order, dedupe identical formatted paths.
        seen: list[str] = []
        for f in formatted:
            if f not in seen:
                seen.append(f)

        snippet = text if len(text) <= 20 else text[:17] + "..."
        return f"@[{sender}] \"{snippet}\" {len(seen)} paths: {', '.join(seen)}"

    async def send_channel_message(self, channel_id: int, text: str) -> bool:
        """Send a text message on the given channel, serialized via a global lock.

        A 2-second gap is enforced after the last chunk so concurrent callers
        can never fire messages back-to-back.  Returns True on success.
        """
        if not self._mc:
            log.warning("Cannot send message: not connected to MeshCore")
            return False
        async with self._send_lock:
            chunks = self._split_message(text)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(2)
                result = await self._mc.commands.send_chan_msg(channel_id, chunk)
                if result.type == EventType.ERROR:
                    log.error(f"Failed to send message on ch{channel_id}: {result.payload}")
                    self.stats.errors += 1
                    return False
                self.stats.messages_out += 1
            await asyncio.sleep(2)  # gap before next message can acquire the lock
        return True

    async def _daily_forecast_loop(self) -> None:
        """Send a daily 3-day forecast broadcast at the configured hour (local time)."""
        cfg = self.config.bot

        tz: datetime.tzinfo | None = None
        if cfg.timezone:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(cfg.timezone)
                log.info("Daily forecast loop: will broadcast at %02d:00 %s", cfg.daily_forecast_hour, cfg.timezone)
            except Exception:
                log.warning("Invalid timezone %r — falling back to system local time", cfg.timezone)

        if tz is None:
            log.info("Daily forecast loop: will broadcast at %02d:00 local time", cfg.daily_forecast_hour)

        while True:
            now = datetime.datetime.now(tz) if tz is not None else datetime.datetime.now().astimezone()
            target = now.replace(
                hour=cfg.daily_forecast_hour, minute=0, second=0, microsecond=0
            )
            if now >= target:
                target += datetime.timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            log.debug(
                "Daily forecast: sleeping %.0fs until %s (local time)",
                sleep_seconds,
                target.strftime("%Y-%m-%d %H:%M %Z"),
            )
            await asyncio.sleep(sleep_seconds)

            if not self._mc:
                log.debug("Daily forecast: not connected, skipping today")
                await asyncio.sleep(60)
                continue

            try:
                msg = await weather_svc.forecast_broadcast(
                    cfg.weather_home_lat,
                    cfg.weather_home_lon,
                    cfg.weather_home_name,
                )
            except Exception as exc:
                log.warning("Daily forecast fetch failed: %s", exc)
                await asyncio.sleep(60)
                continue

            channels = cfg.daily_forecast_channels or [ch.id for ch in cfg.get_active_channels()]
            log.info("Daily forecast: sending to channels %s", channels)
            for ch_id in channels:
                await self.send_channel_message(ch_id, msg)
                await self.message_store.add("out", "system", msg, time.time(), ch_id)
            await self.bus.publish(AppEvent.STATS_UPDATE, self.stats.to_dict())

            # Brief pause so we don't re-trigger within the same minute
            await asyncio.sleep(90)

    async def _on_rx_log_data(self, event) -> None:
        """Handle RX_LOG_DATA — extract and cache path info, keyed by channel hash.

        meshcore stamps each flood-msg log entry with the channel's chan_hash.
        We enqueue per chan_hash so that a CHANNEL_MSG_RECV on channel A is not
        matched against a path that was logged for channel B. Non-channel
        events (acks, adverts, traces, contact msgs) are ignored here so they
        cannot displace pending channel-message paths.
        """
        parsed = parse_rx_log_data(event.payload)
        if not parsed:
            return

        # Always update the legacy single-slot fallback (used by tests and as a
        # last resort when chan_hash mapping is unavailable).
        self._latest_rx_path = parsed
        log.debug(f"RX log path: {parsed}")

        chan_hash = parsed.get("chan_hash")
        payload_type = parsed.get("payload_type")
        if not isinstance(chan_hash, str) or not chan_hash:
            return
        if payload_type is not None and payload_type != _PAYLOAD_TYPE_CHANNEL_MSG:
            return

        now = time.monotonic()
        queue = self._rx_path_by_chan_hash.setdefault(chan_hash, deque())
        queue.append((now, parsed))
        cutoff = now - _RX_PATH_TTL_SECONDS
        while queue and queue[0][0] < cutoff:
            queue.popleft()

    def _consume_rx_path_for_channel(self, channel_idx: int) -> dict[str, Any]:
        """Pop the oldest RX log entry that belongs to the given channel.

        Falls back to the legacy single-slot cache when no chan_hash mapping is
        configured (e.g. unit tests that set _latest_rx_path directly), or when
        the per-channel queue is empty.
        """
        chan_hash = self._chan_hash_by_idx.get(channel_idx)
        if chan_hash:
            queue = self._rx_path_by_chan_hash.get(chan_hash)
            cutoff = time.monotonic() - _RX_PATH_TTL_SECONDS
            while queue and queue[0][0] < cutoff:
                queue.popleft()
            if queue:
                _, parsed = queue.popleft()
                # Successful correlation — the legacy slot is no longer the
                # source of truth for this message, so clear it to prevent
                # accidental reuse on a later un-correlated message.
                self._latest_rx_path = {}
                return parsed
            # Mapping known but no matching log entry — return nothing rather
            # than fall through to the legacy slot, which may belong to a
            # different channel.
            self._latest_rx_path = {}
            return {}

        # No chan_hash mapping (legacy / test path): use single-slot cache.
        rx = self._latest_rx_path
        self._latest_rx_path = {}
        return rx


    @staticmethod
    def _format_hop_path(raw_path: str, hash_size: int) -> str:
        """Format a raw hop path using hash-size aware separators."""
        if not raw_path or hash_size <= 0:
            return ""
        hop_hex_len = hash_size * 2
        if len(raw_path) < hop_hex_len or len(raw_path) % hop_hex_len != 0:
            return ""
        return ":".join(
            raw_path[i : i + hop_hex_len]
            for i in range(0, len(raw_path), hop_hex_len)
        )

    async def _on_channel_msg(self, event) -> None:
        """Handle incoming channel message — check for trace, ping, or paths."""
        data = event.payload
        sender, msg_body = self._parse_sender(data)
        text = data.get("text", "")

        # Determine which channel this message arrived on
        channel_id = data.get("channel_idx", self.config.bot.channel)

        # Path data comes from a correlated RX_LOG_DATA event (radio log).
        # _consume_rx_path_for_channel routes by chan_hash so a packet logged
        # for a different channel cannot be wrongly attributed to this msg.
        rx = self._consume_rx_path_for_channel(channel_id)
        raw_path = rx.get("path", "")
        full_path = rx.get("full_path", raw_path)
        # path_hash_size is also encoded directly in the channel message itself
        # (top 2 bits of the path_len byte = path_hash_mode = path_hash_size-1).
        # Prefer that over any cached value: a stale cached size from a
        # different region/channel would corrupt prefix-command parsing.
        path_hash_mode = data.get("path_hash_mode")
        if isinstance(path_hash_mode, int) and path_hash_mode >= 0:
            path_hash_size = path_hash_mode + 1
        else:
            path_hash_size = rx.get("path_hash_size", 1)
        path_len = rx.get("path_len", data.get("path_len", 0))

        log.debug(
            f"Channel {channel_id} msg from {sender}: {text} "
            f"(path={raw_path}, full_path={full_path}, path_hash_size={path_hash_size}, path_len={path_len})"
        )

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

        is_ping =      bool(_CMD_PATTERNS["ping"].match(msg_body))
        is_trace =     bool(_CMD_PATTERNS["trace"].match(msg_body))
        is_paths =     bool(_CMD_PATTERNS["paths"].match(msg_body))
        is_multipath = bool(_CMD_PATTERNS["multipath"].match(msg_body))
        is_prefix =    bool(_CMD_PATTERNS["prefix"].match(msg_body))
        is_weather =   bool(_CMD_PATTERNS["weather"].match(msg_body))
        is_forecast =  bool(_CMD_PATTERNS["forecast"].match(msg_body))
        is_help =      bool(_CMD_PATTERNS["help"].match(msg_body))

        if not (is_trace or is_ping or is_paths or is_multipath or is_prefix or is_weather or is_forecast or is_help):
            return

        # Determine which command matched and check per-channel permission
        if is_help:
            cmd_name = "help"
        elif is_weather:
            cmd_name = "weather"
        elif is_forecast:
            cmd_name = "forecast"
        elif is_prefix:
            cmd_name = "prefix"
        elif is_multipath:
            cmd_name = "multipath"
        elif is_paths:
            cmd_name = "paths"
        elif is_trace:
            cmd_name = "trace"
        else:
            cmd_name = "ping"

        if not self.config.bot.is_command_enabled(channel_id, cmd_name):
            log.debug(f"Command '{cmd_name}' not enabled on channel {channel_id}, ignoring")
            return

        if self.config.bot.is_rate_limit_enabled(channel_id):
            timeout_s = self.config.bot.get_rate_limit_seconds(channel_id)
            sender_key = sender.strip().lower()
            rate_limit_key = (channel_id, sender_key)
            now = time.time()
            last_cmd_at = self._last_command_at_by_user.get(rate_limit_key)
            if last_cmd_at is not None and (now - last_cmd_at) < timeout_s:
                remaining = timeout_s - (now - last_cmd_at)
                log.debug(
                    "Rate limit hit for %s on channel %s (%ss remaining)",
                    sender,
                    channel_id,
                    int(max(1, remaining)),
                )
                return
            self._last_command_at_by_user[rate_limit_key] = now

        self.stats.commands_processed += 1

        # Handle weather command — current conditions for home suburb or given postcode
        if is_weather:
            log.info(f"Weather from {sender} on ch{channel_id}")
            postcode = msg_body[len("weather"):].strip() or "3976"
            try:
                coords = await weather_svc.get_coords_for_postcode(postcode)
                if coords is None:
                    reply = f"@[{sender}] Could not find postcode {postcode}"
                else:
                    lat, lon, name = coords
                    reply = await weather_svc.current_weather_reply(sender, lat, lon, name)
            except Exception as exc:
                log.warning(f"Weather fetch failed: {exc}")
                reply = f"@[{sender}] Weather unavailable, try again later"
        # Handle forecast command — 3-day outlook for home suburb or given postcode
        elif is_forecast:
            log.info(f"Forecast from {sender} on ch{channel_id}")
            postcode = msg_body[len("forecast"):].strip() or None
            try:
                if postcode:
                    coords = await weather_svc.get_coords_for_postcode(postcode)
                    if coords is None:
                        reply = f"@[{sender}] Could not find postcode {postcode}"
                    else:
                        lat, lon, name = coords
                        reply = await weather_svc.forecast_reply(sender, lat, lon, name)
                else:
                    cfg = self.config.bot
                    reply = await weather_svc.forecast_reply(
                        sender, cfg.weather_home_lat, cfg.weather_home_lon, cfg.weather_home_name
                    )
            except Exception as exc:
                log.warning(f"Forecast fetch failed: {exc}")
                reply = f"@[{sender}] Forecast unavailable, try again later"
        # Handle prefix command — look up repeater names from hex prefixes
        elif is_prefix:
            log.info(f"Prefix lookup from {sender} on ch{channel_id}")
            hex_arg = msg_body[len("prefix"):].strip()
            if hex_arg:
                lookup = self.resolver.lookup_prefixes(hex_arg, path_hash_size=path_hash_size)
                if lookup:
                    reply = f"@[{sender}] {lookup}"
                else:
                    reply = f"@[{sender}] invalid prefix string"
            else:
                reply = f"@[{sender}] usage: prefix <hex> (e.g. prefix fb:1f:7a or fb1f:7ab2)"
        # Handle paths command
        elif is_paths:
            log.info(f"Paths from {sender} on ch{channel_id}")
            reply = self._build_paths_reply(sender)
        # Handle multipath command — every path seen for the sender's most
        # recent (non-multipath) message text on this channel.
        elif is_multipath:
            log.info(f"Multipath from {sender} on ch{channel_id}")
            reply = self._build_multipath_reply(sender, channel_id)
        # Handle trace command
        elif is_trace:
            log.info(f"Trace from {sender} on ch{channel_id}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                resolved = self.resolver.resolve(raw_path)
                if path_hash_size > 1:
                    raw_display = self._format_hop_path(full_path, path_hash_size)
                    if raw_display:
                        resolved = f"{resolved}; raw {raw_display}"
                reply = f"@[{sender}] {resolved}"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops, no path detail)"
            else:
                reply = f"@[{sender}] rxed (no path data)"
        # Handle help command — list enabled commands for this channel
        elif is_help:
            log.info(f"Help from {sender} on ch{channel_id}")
            enabled = []
            for ch in self.config.bot.get_active_channels():
                if ch.id == channel_id:
                    enabled = ch.enabled_commands
                    break
            if enabled:
                reply = f"@[{sender}] cmds: {', '.join(enabled)}"
            else:
                reply = f"@[{sender}] no commands enabled on this channel"
        # Handle ping command
        else:
            log.info(f"Ping from {sender} on ch{channel_id}")
            if raw_path and len(raw_path) >= 2 and len(raw_path) % 2 == 0:
                display_path = self._format_hop_path(full_path, path_hash_size)
                if not display_path:
                    display_path = self._format_hop_path(raw_path, 1)
                if display_path:
                    reply = f"@[{sender}] rxed {display_path} ({path_len} hops)"
                else:
                    reply = f"@[{sender}] rxed ({path_len} hops, no path detail)"
            elif path_len > 0:
                reply = f"@[{sender}] rxed ({path_len} hops, no path detail)"
            else:
                reply = f"@[{sender}] rxed"

        # For ping replies: inject promo URL on every Nth reply, or carry forward
        # if the reply is already too long to fit it.
        if cmd_name == "ping":
            self._ping_reply_count += 1
            wants_url = (self._ping_reply_count % _PING_URL_INTERVAL == 0) or self._pending_url
            self._pending_url = False
            if wants_url:
                url_suffix = " " + _PROMO_URL
                if len(reply) + len(url_suffix) <= _MAX_MSG_LEN:
                    reply += url_suffix
                else:
                    self._pending_url = True  # carry to next ping reply

        log.info(f"Replying on ch{channel_id}: {reply}")
        ok = await self.send_channel_message(channel_id, reply)
        if not ok:
            await self.bus.publish(AppEvent.ERROR, {"message": "Send failed"})
            return

        out_ts = time.time()
        await self.message_store.add(
            "out", sender, reply, out_ts, channel_id,
        )
        await self.bus.publish(
            AppEvent.MSG_OUT,
            {"recipient": sender, "text": reply, "timestamp": out_ts, "channel": channel_id},
        )
        await self.bus.publish(AppEvent.STATS_UPDATE, self.stats.to_dict())
