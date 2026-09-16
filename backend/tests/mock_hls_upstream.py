"""
可配置的 mock HLS 上游服务器。用于集成测试：让 WaveFlow 真正请求这个假上游，
验证 playlist 重写、handle 签发、chunk 代理、响应头与状态码的完整链路。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import urlparse


@dataclass
class LiveScenario:
    """标准直播场景配置。用 ``.freeze()`` 在启动服务器前捕获当前值。"""
    initial_media_sequence: int = 0
    target_duration: int = 8
    segment_count: int = 3
    cyclic_names: tuple[str, ...] = ()
    segment_data: bytes = field(default_factory=lambda: b"FAKE_TS_DATA" * 1024)
    segment_content_type: str = "video/mp2t"
    discontinuity: bool = False
    key_uri: str = ""
    map_uri: str = ""
    status_sequence: list[int] = field(default_factory=list)
    fail_segments_at: set[int] = field(default_factory=set)
    slow_response_seconds: float = 0.0
    wrong_content_length: bool = False
    playlist_freeze: bool = False
    redirect_target: str = ""
    _generation: int = 0
    _request_count: int = 0

    def freeze(self):
        from copy import deepcopy
        return deepcopy(self)

    @property
    def media_sequence(self) -> int:
        return self.initial_media_sequence + self._generation

    def advance(self) -> None:
        if not self.playlist_freeze:
            self._generation += 1


class MockHLSServer:
    """轻量 asyncio mock HLS 服务器。在 localhost 随机端口上服务 HLS 播放列表与分片。"""
    def __init__(self, *, scenario=None, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self.scenario = (scenario or LiveScenario()).freeze()
        self._server = None
        self._tasks = set()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def playlist_url(self) -> str:
        return f"{self.base_url}/playlist.m3u8"

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        for s in self._server.sockets or ():
            self.port = s.getsockname()[1]
            break

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        s = self.scenario
        try:
            rl = await asyncio.wait_for(reader.readline(), 10.0)
            rl = rl.decode("ascii", "replace").strip()
            if not rl:
                return
            hdrs = {}
            while True:
                ln = await asyncio.wait_for(reader.readline(), 5.0)
                ln = ln.decode("ascii", "replace").strip()
                if not ln:
                    break
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    hdrs[k.strip().lower()] = v.strip()
            parts = rl.split()
            if len(parts) < 2:
                return await self._send(writer, 400, "Bad Request")
            method, path = parts[0], parts[1]
            clean = urlparse(path).path.rstrip("/") or "/"
            s._request_count += 1

            if s.slow_response_seconds > 0:
                await asyncio.sleep(s.slow_response_seconds)
            if s.redirect_target and clean.endswith(".m3u8") and clean != s.redirect_target:
                return await self._send(writer, 302, "", {"Location": s.redirect_target})

            if clean.endswith("/playlist.m3u8"):
                idx = min(s._request_count - 1, max(len(s.status_sequence) - 1, 0))
                st = s.status_sequence[idx] if s.status_sequence else 200
                return await self._playlist(writer, st)

            if clean.endswith("/master.m3u8"):
                body = "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:8\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\nvariant.m3u8\n"
                return await self._send(writer, 200, body, {"Content-Type": "application/x-mpegURL"})

            sm = re.match(r"^/(s(\d+)|(\d+))\.(ts|m4s|mp4)$", clean)
            if sm:
                seq = int(sm.group(2) or sm.group(3))
                ext = sm.group(4)
                return await self._segment(writer, seq, ext, hdrs)
            await self._send(writer, 404, "Not Found")
        except Exception:
            pass

    async def _playlist(self, writer, status):
        s = self.scenario
        if status != 200:
            return await self._send(writer, status, "error")
        seq = s.media_sequence
        n = s.segment_count
        names = list(s.cyclic_names) if s.cyclic_names else [f"s{i}.ts" for i in range(n)]
        lines = ["#EXTM3U", f"#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{s.target_duration}", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
        if s.key_uri:
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{s.key_uri}",IV=0x00000000000000000000000000000000')
        if s.map_uri:
            lines.append(f'#EXT-X-MAP:URI="{s.map_uri}"')
        for i in range(n):
            if s.discontinuity and i == 0:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{s.target_duration:.3f},")
            name = names[i % len(names)] if not (s.fail_segments_at and (seq + i) in s.fail_segments_at) else f"bad_{seq + i}.ts"
            lines.append(f"/{name}")
        s.advance()
        await self._send(writer, 200, "\n".join(lines) + "\n", {"Content-Type": "application/x-mpegURL", "Cache-Control": "no-cache"})

    async def _segment(self, writer, seq, ext, rhdrs):
        s = self.scenario
        if s.fail_segments_at and seq in s.fail_segments_at:
            return await self._send(writer, 404, "gone")
        data = s.segment_data
        ct = s.segment_content_type if ext in ("ts", "m4s", "mp4") else "application/octet-stream"
        hdrs = {"Content-Type": ct, "Cache-Control": "max-age=3600", "Accept-Ranges": "bytes"}
        rng = rhdrs.get("range", "")
        if rng.startswith("bytes="):
            try:
                rv = rng[6:]
                a_str, _, b_str = rv.partition("-")
                a = int(a_str) if a_str else 0
                b = int(b_str) if b_str else len(data) - 1
                b = min(b, len(data) - 1)
                if a <= b:
                    hdrs["Content-Range"] = f"bytes {a}-{b}/{len(data)}"
                    data = data[a : b + 1]
                    return await self._send(writer, 206, data, hdrs)
            except (ValueError, IndexError):
                pass
        if s.wrong_content_length:
            hdrs["Content-Length"] = str(len(data) + 999)
        else:
            hdrs["Content-Length"] = str(len(data))
        await self._send(writer, 200, data, hdrs)

    async def _send(self, writer, status, body, hdrs=None):
        if isinstance(body, str):
            body = body.encode()
        st = HTTPStatus(status).phrase if status in (x.value for x in HTTPStatus) else "Unknown"
        lines = [f"HTTP/1.1 {status} {st}"]
        hdrs = dict(hdrs or {})
        hdrs.setdefault("Content-Length", str(len(body)))
        hdrs.setdefault("Connection", "close")
        hdrs.setdefault("Server", "MockHLS/1.0")
        for k, v in hdrs.items():
            lines.append(f"{k}: {v}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        writer.close()
