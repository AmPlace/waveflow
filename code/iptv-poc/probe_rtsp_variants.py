#!/usr/bin/env python3
"""Probe common operator RTSP catch-up URL variants without logging secrets."""

from __future__ import annotations

import datetime as dt
import os
import re
import socket
import sys
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TIMEOUT = float(os.getenv("IPTV_POC_TIMEOUT", "12"))
END_OFFSET = int(os.getenv("IPTV_POC_END_OFFSET_MINUTES", "5"))
START_OFFSET = int(os.getenv("IPTV_POC_START_OFFSET_MINUTES", "10"))


@dataclass
class ProbeResult:
    name: str
    ok: bool
    status: str
    range_header: str = ""
    detail: str = ""


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() != "rtsp" or not parts.hostname:
        raise ValueError("IPTV_RTSP_URL must be an rtsp URL")


def add_query(url: str, **updates: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(updates)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def path_variant(url: str, segment: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/PLTV(?=/|$)", f"/{segment}", parts.path, count=1, flags=re.IGNORECASE)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def rtsp_exchange(sock: socket.socket, url: str, method: str, cseq: int, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"CSeq": str(cseq), "User-Agent": "WaveFlow-IPTV-POC/1.0"}
    if headers:
        request_headers.update(headers)
    request = f"{method} {url} RTSP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items()) + "\r\n"
    sock.sendall(request.encode("ascii", "ignore"))
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(65536)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
    response_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.lower()] = value.strip()
    content_length = int(response_headers.get("content-length", "0"))
    while len(body) < content_length:
        body += sock.recv(65536)
    return status, response_headers, body[:content_length]


def probe(name: str, url: str, play_range: str | None = None) -> ProbeResult:
    try:
        parts = urlsplit(url)
        with socket.create_connection((parts.hostname, parts.port or 554), TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            status, headers, sdp = rtsp_exchange(sock, url, "DESCRIBE", 1, {"Accept": "application/sdp"})
            if status != 200:
                return ProbeResult(name, False, f"DESCRIBE {status}", detail=safe_url(url))
            status, headers, _ = rtsp_exchange(
                sock,
                url,
                "SETUP",
                2,
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            )
            if status != 200:
                return ProbeResult(name, False, f"SETUP {status}", detail=safe_url(url))
            session = headers.get("session", "").split(";", 1)[0]
            play_headers: dict[str, str] = {"Session": session} if session else {}
            if play_range:
                play_headers["Range"] = play_range
            status, headers, _ = rtsp_exchange(sock, url, "PLAY", 3, play_headers)
            response_range = headers.get("range", "")
            return ProbeResult(name, status == 200, f"PLAY {status}", response_range, safe_url(url))
    except (OSError, ValueError, TimeoutError) as exc:
        return ProbeResult(name, False, type(exc).__name__, detail=safe_url(url))


def main() -> int:
    url = os.getenv("IPTV_RTSP_URL")
    if not url:
        print("missing IPTV_RTSP_URL", file=sys.stderr)
        return 2
    validate_url(url)
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(minutes=START_OFFSET)
    end = now - dt.timedelta(minutes=END_OFFSET)
    window = f"{start.astimezone(dt.timezone(dt.timedelta(hours=8))):%Y%m%d%H%M%S}-{end.astimezone(dt.timezone(dt.timedelta(hours=8))):%Y%m%d%H%M%S}"

    variants = [
        ("live", url, None),
        ("PLTV + playseek", add_query(url, playseek=window), None),
        ("TVOD + playseek", add_query(path_variant(url, "TVOD"), playseek=window), None),
        ("PLTV + tvdr", add_query(url, tvdr=window), None),
        ("npt historical", url, "npt=3600-"),
        ("clock historical", url, f"clock={(now - dt.timedelta(minutes=END_OFFSET)).strftime('%Y%m%dT%H%M%SZ')}-"),
    ]
    print(f"window={window} (Asia/Shanghai), target={safe_url(url)}")
    results = [probe(name, variant, play_range) for name, variant, play_range in variants]
    for result in results:
        suffix = f", response_range={result.range_header}" if result.range_header else ""
        print(f"{result.name}: {'PASS' if result.ok else 'FAIL'} {result.status}{suffix}")
    print("PASS means the RTSP request was accepted only; verify media timestamps before calling it replay.")
    return 0 if any(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
