"""Monkey-patch meshcore MessageReader to extract path bytes from channel messages.

The meshcore library (as of 2.2.8) reads ``path_len`` from channel message
packets but does not read the actual path bytes that follow.  The binary
format is::

    [packet_type][channel_idx][path_len][path bytes …][txt_type][timestamp][text]

Without this patch, ``path_len`` bytes of path data are silently consumed as
``txt_type`` / ``sender_timestamp`` / ``text``, causing the parsed payload to
lack a ``path`` field and garble the remaining fields for relayed messages.
"""

from __future__ import annotations

import io
import logging

from meshcore.events import Event, EventType
from meshcore.packets import PacketType
from meshcore.reader import MessageReader

log = logging.getLogger("pathbot.reader_patch")

_original_handle_rx = MessageReader.handle_rx


async def _patched_handle_rx(self, data: bytearray):
    """Wrap ``handle_rx`` to intercept channel message packets."""
    if len(data) == 0:
        await _original_handle_rx(self, data)
        return

    packet_type = data[0]

    if packet_type == PacketType.CHANNEL_MSG_RECV.value:
        await _parse_channel_msg(self, data, v3=False)
    elif packet_type == 17:  # CHANNEL_MSG_RECV_V3
        await _parse_channel_msg(self, data, v3=True)
    else:
        await _original_handle_rx(self, data)


async def _parse_channel_msg(reader, data: bytearray, *, v3: bool):
    """Parse a channel message with proper path extraction."""
    dbuf = io.BytesIO(data)
    dbuf.read(1)  # skip packet type

    res = {"type": "CHAN"}

    if v3:
        res["SNR"] = int.from_bytes(dbuf.read(1), byteorder="little", signed=True) / 4
        dbuf.read(2)  # reserved

    res["channel_idx"] = dbuf.read(1)[0]

    plen = dbuf.read(1)[0]
    res["path_len"] = plen
    if plen > 0:
        res["path"] = dbuf.read(plen).hex()
    else:
        res["path"] = ""

    res["txt_type"] = dbuf.read(1)[0]
    res["sender_timestamp"] = int.from_bytes(dbuf.read(4), byteorder="little")
    res["text"] = dbuf.read().decode("utf-8", "ignore")

    attributes = {
        "channel_idx": res["channel_idx"],
        "txt_type": res["txt_type"],
    }

    await reader.dispatcher.dispatch(
        Event(EventType.CHANNEL_MSG_RECV, res, attributes)
    )


def apply():
    """Apply the monkey-patch (idempotent)."""
    if MessageReader.handle_rx is _patched_handle_rx:
        return
    log.info("Patching meshcore MessageReader to extract channel message path bytes")
    MessageReader.handle_rx = _patched_handle_rx
