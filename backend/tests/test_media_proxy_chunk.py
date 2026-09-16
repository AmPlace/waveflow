import asyncio
import unittest

import httpx

from routers.media_proxy import (
    _safe_upstream_response_headers,
    _stream_httpx_response,
    _HOP_BY_HOP_HEADERS,
    _CHUNK_PASSTHROUGH_HEADERS,
)


class StreamingResponseHelpersTest(unittest.TestCase):
    def test_hop_by_hop_headers_are_filtered(self):
        upstream = httpx.Headers({
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Transfer-Encoding": "chunked",
            "Content-Encoding": "gzip",
            "Content-Length": "1234",
            "Content-Range": "bytes 0-99/1234",
            "Accept-Ranges": "bytes",
            "ETag": '"abc"',
            "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        })
        out = _safe_upstream_response_headers(upstream, live_chunk=True)
        lowered = {k.lower(): v for k, v in out.items()}
        for h in _HOP_BY_HOP_HEADERS:
            self.assertNotIn(h, lowered)
        self.assertNotIn("content-length", lowered)
        self.assertEqual(lowered.get("content-range"), "bytes 0-99/1234")
        self.assertEqual(lowered.get("accept-ranges"), "bytes")
        self.assertEqual(lowered.get("cache-control"), "no-store")

    def test_content_length_passes_through_when_unencoded(self):
        upstream = httpx.Headers({
            "Content-Length": "398936",
            "Content-Type": "video/mp2t",
            "Accept-Ranges": "bytes",
        })
        out = _safe_upstream_response_headers(upstream, live_chunk=True)
        lowered = {k.lower(): v for k, v in out.items()}
        self.assertEqual(lowered.get("content-length"), "398936")
        self.assertEqual(lowered.get("cache-control"), "no-store")
        self.assertNotIn("content-type", lowered)

    def test_passthrough_set_only_includes_safe_keys(self):
        self.assertEqual(
            _CHUNK_PASSTHROUGH_HEADERS,
            frozenset({
                "content-length",
                "content-range",
                "accept-ranges",
                "etag",
                "last-modified",
            }),
        )


class FakeUpstream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.raw_calls = 0
        self.bytes_calls = 0
        self.closed = False

    async def aiter_raw(self, _size=0):
        self.raw_calls += 1
        for c in self._chunks:
            yield c

    async def aiter_bytes(self, _size=0):
        self.bytes_calls += 1
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


class StreamHttpxResponseTest(unittest.TestCase):
    def test_uses_aiter_raw_and_closes(self):
        upstream = FakeUpstream([b"hello", b"world"])

        async def consume():
            buf = b""
            async for chunk in _stream_httpx_response(upstream):
                buf += chunk
            return buf

        out = asyncio.run(consume())
        self.assertEqual(out, b"helloworld")
        self.assertEqual(upstream.raw_calls, 1)
        self.assertEqual(upstream.bytes_calls, 0)
        self.assertTrue(upstream.closed)

    def test_closes_even_on_consumer_break(self):
        upstream = FakeUpstream([b"a", b"b", b"c"])

        async def consume_once():
            async for _chunk in _stream_httpx_response(upstream):
                break

        asyncio.run(consume_once())
        self.assertTrue(upstream.closed)

    def test_closes_when_consumer_task_is_cancelled(self):
        entered = asyncio.Event()

        class BlockingUpstream(FakeUpstream):
            async def aiter_raw(self, _size=0):
                self.raw_calls += 1
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        upstream = BlockingUpstream([])

        async def cancel_consumer():
            async def consume():
                async for _chunk in _stream_httpx_response(upstream):
                    pass

            task = asyncio.create_task(consume())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_consumer())
        self.assertTrue(upstream.closed)


if __name__ == "__main__":
    unittest.main()
