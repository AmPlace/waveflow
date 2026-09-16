from __future__ import annotations

import asyncio
import json
from typing import Any

from .errors import invalid_response


LEGACY_PROTOCOL_VERSION = "1.0"
PROTOCOL_VERSION = "1.1"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({LEGACY_PROTOCOL_VERSION, PROTOCOL_VERSION})
MAX_FRAME_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192


def encode_frame(payload: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise invalid_response("Protocol payload is not JSON serializable") from exc
    if len(body) > max_bytes:
        raise invalid_response("Plugin protocol frame exceeds the size limit")
    return (f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode("ascii") + body)


async def read_frame(reader: asyncio.StreamReader, *, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        try:
            chunk = await reader.readexactly(1)
        except asyncio.IncompleteReadError as exc:
            raise invalid_response("Plugin protocol frame is truncated") from exc
        header.extend(chunk)
        if len(header) > MAX_HEADER_BYTES:
            raise invalid_response("Plugin protocol header is too large")
    try:
        lines = header[:-4].decode("ascii").split("\r\n")
    except UnicodeDecodeError as exc:
        raise invalid_response("Plugin protocol header is not ASCII") from exc
    fields: dict[str, str] = {}
    for line in lines:
        if ": " not in line:
            raise invalid_response("Malformed plugin protocol header")
        name, value = line.split(": ", 1)
        key = name.lower()
        if key in fields:
            raise invalid_response("Duplicate plugin protocol header")
        fields[key] = value
    if set(fields) != {"content-length", "content-type"} or fields["content-type"].lower() != "application/json; charset=utf-8":
        raise invalid_response("Unsupported plugin protocol headers")
    if not fields["content-length"].isdigit():
        raise invalid_response("Invalid plugin protocol Content-Length")
    length = int(fields["content-length"])
    if length <= 0 or length > max_bytes:
        raise invalid_response("Plugin protocol frame exceeds the size limit")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise invalid_response("Plugin protocol payload is truncated") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise invalid_response("Plugin protocol payload is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise invalid_response("Plugin protocol payload must be an object")
    return payload
