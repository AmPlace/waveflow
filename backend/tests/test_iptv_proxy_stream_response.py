import asyncio
import unittest
from unittest import mock

import httpx


def _main_module():
    import main

    return main


class FakeRequest:
    def __init__(self, disconnected_after=10_000):
        self.calls = 0
        self.disconnected_after = disconnected_after

    async def is_disconnected(self):
        self.calls += 1
        return self.calls > self.disconnected_after


class FakeUpstreamResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = list(chunks)
        self.headers = dict(headers or {})
        self.chunk_size_seen = object()
        self.raw_calls = 0
        self.closed = False
        self.started = asyncio.Event()

    def raise_for_status(self):
        return None

    async def aiter_raw(self, chunk_size=None):
        self.raw_calls += 1
        self.chunk_size_seen = chunk_size
        self.started.set()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class GatedUpstreamResponse(FakeUpstreamResponse):
    def __init__(self, first_chunk, remaining_chunks, release_event):
        super().__init__([first_chunk, *remaining_chunks])
        self.first_chunk = first_chunk
        self.remaining_chunks = list(remaining_chunks)
        self.release_event = release_event
        self.first_chunk_yielded = asyncio.Event()

    async def aiter_raw(self, chunk_size=None):
        self.raw_calls += 1
        self.chunk_size_seen = chunk_size
        self.started.set()
        self.first_chunk_yielded.set()
        yield self.first_chunk
        await self.release_event.wait()
        for chunk in self.remaining_chunks:
            yield chunk


class FakeAsyncClient:
    instances = []

    def __init__(self, *args, response=None, **kwargs):
        self.response = response if response is not None else FakeUpstreamResponse([])
        self.closed = False
        self.requests = []
        FakeAsyncClient.instances.append(self)

    def build_request(self, method, url, headers=None):
        request = {"method": method, "url": url, "headers": headers or {}}
        self.requests.append(request)
        return request

    async def send(self, request, stream=False):
        self.stream = stream
        return self.response

    async def aclose(self):
        self.closed = True


async def _make_stream_response(
    upstream,
    stream_type="http_flv",
    request=None,
    upstream_url="https://stream.example.test/live.flv",
):
    # The production stream proxy reconnects live streams after EOF. Unit tests use
    # finite fake streams, so the fake client disconnects after the current fake
    # upstream has been fully consumed and the loop checks for disconnection.
    request = request or FakeRequest(disconnected_after=len(getattr(upstream, "chunks", [])) + 1)

    main_mod = _main_module()

    def client_factory(*args, **kwargs):
        return FakeAsyncClient(*args, response=upstream, **kwargs)

    async def fake_stream(client, method, url, *, headers=None, **_kwargs):
        request = client.build_request(method, url, headers=headers)
        return await client.send(request, stream=True)

    with mock.patch.object(main_mod.httpx, "AsyncClient", client_factory):
        with mock.patch.object(main_mod, "stream_with_safe_redirects", new=fake_stream):
            response = await main_mod.serve_iptv_proxy_stream_response(
                request=request,
                upstream_url=upstream_url,
                upstream_headers={"User-Agent": "UA"},
                stream_type=stream_type,
            )
    return response


async def _consume_response(response):
    body = []
    async for chunk in response.body_iterator:
        body.append(chunk)
    return body


class IptvProxyStreamResponseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAsyncClient.instances.clear()

    async def test_http_flv_does_not_force_64k_chunking_and_preserves_bytes(self):
        chunks = [b"flv", b"\x00" * 512, b"tail"]
        upstream = FakeUpstreamResponse(chunks)

        response = await _make_stream_response(upstream, stream_type="http_flv")
        out_chunks = await _consume_response(response)

        self.assertIsNone(upstream.chunk_size_seen)
        self.assertEqual(out_chunks, chunks)
        self.assertEqual(b"".join(out_chunks), b"".join(chunks))
        self.assertEqual(upstream.raw_calls, 1)
        self.assertTrue(upstream.closed)
        self.assertEqual(response.media_type, "video/x-flv")
        self.assertTrue(FakeAsyncClient.instances[-1].closed)

    async def test_small_chunk_is_delivered_before_later_chunks_are_available(self):
        release = asyncio.Event()
        first = b"a" * 512
        rest = [b"b" * 1024, b"c"]
        upstream = GatedUpstreamResponse(first, rest, release)

        response = await _make_stream_response(upstream, stream_type="http_flv")
        iterator = response.body_iterator.__aiter__()

        first_task = asyncio.create_task(iterator.__anext__())
        first_out = await asyncio.wait_for(first_task, timeout=0.5)

        self.assertEqual(first_out, first)
        self.assertTrue(upstream.first_chunk_yielded.is_set())
        self.assertFalse(release.is_set())
        self.assertIsNone(upstream.chunk_size_seen)

        second_task = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())

        release.set()
        remaining = [await asyncio.wait_for(second_task, timeout=0.5)]
        async for chunk in iterator:
            remaining.append(chunk)

        self.assertEqual([first_out, *remaining], [first, *rest])
        self.assertEqual(b"".join([first_out, *remaining]), first + b"".join(rest))
        self.assertTrue(upstream.closed)

    async def test_http_flv_and_mpegts_share_streaming_loop_contract(self):
        for stream_type, media_type in (("http_flv", "video/x-flv"), ("mpegts", "video/MP2T")):
            with self.subTest(stream_type=stream_type):
                chunks = [b"one", b"two", b"three"]
                upstream = FakeUpstreamResponse(chunks)

                response = await _make_stream_response(upstream, stream_type=stream_type)
                out_chunks = await _consume_response(response)

                self.assertIsNone(upstream.chunk_size_seen)
                self.assertEqual(out_chunks, chunks)
                self.assertEqual(b"".join(out_chunks), b"onetwothree")
                self.assertTrue(upstream.closed)
                self.assertEqual(response.media_type, media_type)
                self.assertTrue(FakeAsyncClient.instances[-1].closed)

    async def test_read_timeout_reconnects_once_and_reuses_stream_headers(self):
        class TimeoutUpstream(FakeUpstreamResponse):
            async def aiter_raw(self, chunk_size=None):
                self.raw_calls += 1
                self.chunk_size_seen = chunk_size
                yield b"first"
                raise httpx.ReadTimeout("read timed out")

        first_upstream = TimeoutUpstream([])
        second_upstream = FakeUpstreamResponse([b"second"])

        class SequenceClient:
            def __init__(self, *args, **kwargs):
                self.responses = [first_upstream, second_upstream]
                self.requests = []
                self.closed = False

            def build_request(self, method, url, headers=None):
                return {"method": method, "url": url, "headers": headers or {}}

            async def send(self, request, stream=False):
                self.requests.append(request)
                return self.responses.pop(0)

            async def aclose(self):
                self.closed = True

        client = None

        def client_factory(*args, **kwargs):
            nonlocal client
            client = SequenceClient(*args, **kwargs)
            return client

        async def fake_stream(client, method, url, *, headers=None, **_kwargs):
            request = client.build_request(method, url, headers=headers)
            return await client.send(request, stream=True)

        main_mod = _main_module()
        with mock.patch.object(main_mod.httpx, "AsyncClient", client_factory):
            with mock.patch.object(main_mod, "stream_with_safe_redirects", new=fake_stream):
                response = await main_mod.serve_iptv_proxy_stream_response(
                    request=FakeRequest(disconnected_after=10),
                    upstream_url="https://stream.example.test/live.flv",
                    upstream_headers={
                        "User-Agent": "fixture-ua",
                        "Referer": "https://origin.example/",
                        "Cookie": "fixture-cookie",
                    },
                    stream_type="http_flv",
                )
                output = await _consume_response(response)

        self.assertEqual(output, [b"first", b"second"])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0]["headers"], client.requests[1]["headers"])
        self.assertTrue(first_upstream.closed)
        self.assertTrue(second_upstream.closed)
        self.assertTrue(client.closed)

    async def test_audio_http_preserves_upstream_mpeg_mime(self):
        upstream = FakeUpstreamResponse(
            [b"mp3"],
            headers={"content-type": "audio/mpeg"},
        )

        response = await _make_stream_response(
            upstream,
            stream_type="audio_http",
            upstream_url="https://stream.example.test/live",
        )

        self.assertEqual(response.media_type, "audio/mpeg")
        self.assertEqual(await _consume_response(response), [b"mp3"])

    async def test_audio_http_infers_aac_mime_from_audio_url_when_upstream_is_generic(self):
        upstream = FakeUpstreamResponse(
            [b"aac"],
            headers={"content-type": "application/octet-stream"},
        )

        response = await _make_stream_response(
            upstream,
            stream_type="audio_http",
            upstream_url="https://stream.example.test/live.aac",
        )

        self.assertEqual(response.media_type, "audio/aac")

    async def test_audio_http_unknown_mime_does_not_fall_back_to_mpegts(self):
        upstream = FakeUpstreamResponse(
            [b"audio"],
            headers={"content-type": "video/MP2T"},
        )

        response = await _make_stream_response(
            upstream,
            stream_type="audio_http",
            upstream_url="https://stream.example.test/live",
        )

        self.assertEqual(response.media_type, "application/octet-stream")

    async def test_upstream_closes_when_consumer_cancels_iteration(self):
        release = asyncio.Event()
        upstream = GatedUpstreamResponse(b"first", [b"second"], release)
        response = await _make_stream_response(upstream, stream_type="http_flv")
        iterator = response.body_iterator.__aiter__()

        self.assertEqual(await asyncio.wait_for(iterator.__anext__(), timeout=0.5), b"first")
        await iterator.aclose()

        self.assertTrue(upstream.closed)
        self.assertTrue(FakeAsyncClient.instances[-1].closed)

    async def test_client_disconnect_stops_without_reading_forever(self):
        chunks = [b"first", b"second"]
        upstream = FakeUpstreamResponse(chunks)
        request = FakeRequest(disconnected_after=0)

        response = await _make_stream_response(upstream, stream_type="http_flv", request=request)
        out_chunks = await _consume_response(response)

        self.assertEqual(out_chunks, [])
        self.assertEqual(upstream.raw_calls, 0)
        self.assertTrue(upstream.closed)
        self.assertTrue(FakeAsyncClient.instances[-1].closed)

    async def test_upstream_iteration_exception_propagates_and_closes(self):
        class BrokenUpstream(FakeUpstreamResponse):
            async def aiter_raw(self, chunk_size=None):
                self.raw_calls += 1
                self.chunk_size_seen = chunk_size
                yield b"first"
                raise RuntimeError("boom")

        upstream = BrokenUpstream([])
        response = await _make_stream_response(upstream, stream_type="http_flv", request=FakeRequest(disconnected_after=10))
        iterator = response.body_iterator.__aiter__()

        self.assertEqual(await asyncio.wait_for(iterator.__anext__(), timeout=0.5), b"first")
        with self.assertRaises(RuntimeError):
            await iterator.__anext__()
        self.assertTrue(upstream.closed)
        self.assertTrue(FakeAsyncClient.instances[-1].closed)

    async def test_stream_diagnostics_do_not_log_upstream_query_parameters(self):
        class BrokenUpstream(FakeUpstreamResponse):
            async def aiter_raw(self, chunk_size=None):
                self.raw_calls += 1
                self.chunk_size_seen = chunk_size
                yield b"first"
                raise httpx.ReadTimeout(f"upstream failed: {secret_url}")

        secret_url = "https://stream.example.test/live.flv?token=secret-value&accountinfo=private"
        upstream = BrokenUpstream([])
        response = await _make_stream_response(
            upstream,
            stream_type="http_flv",
            request=FakeRequest(disconnected_after=3),
            upstream_url=secret_url,
        )

        with self.assertLogs("waveflow", level="WARNING") as logs:
            self.assertEqual(await _consume_response(response), [b"first"])

        output = "\n".join(logs.output)
        self.assertIn("https://stream.example.test", output)
        self.assertNotIn("secret-value", output)
        self.assertNotIn("accountinfo=private", output)
        self.assertNotIn("?token=", output)

    async def test_midstream_http_error_does_not_echo_url_query_parameters(self):
        secret_url = "https://stream.example.test/live.flv?token=secret-value&signature=private"

        class BrokenUpstream(FakeUpstreamResponse):
            async def aiter_raw(self, chunk_size=None):
                self.raw_calls += 1
                self.chunk_size_seen = chunk_size
                yield b"first"
                raise httpx.ReadError(f"connection reset: {secret_url}")

        response = await _make_stream_response(
            BrokenUpstream([]),
            stream_type="http_flv",
            request=FakeRequest(disconnected_after=3),
            upstream_url=secret_url,
        )

        with self.assertLogs("waveflow", level="WARNING") as logs:
            self.assertEqual(await _consume_response(response), [b"first"])

        output = "\n".join(logs.output)
        self.assertIn("ReadError", output)
        self.assertNotIn("secret-value", output)
        self.assertNotIn("signature=private", output)

    async def test_initial_upstream_error_does_not_echo_url_query_parameters(self):
        secret_url = "https://stream.example.test/live.ts?token=secret-value&signature=private"

        class FailedUpstream(FakeUpstreamResponse):
            def raise_for_status(self):
                request = httpx.Request("GET", secret_url)
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError(
                    "upstream denied: " + secret_url,
                    request=request,
                    response=response,
                )

        with self.assertRaises(_main_module().HTTPException) as raised:
            await _make_stream_response(
                FailedUpstream([]),
                stream_type="mpegts",
                upstream_url=secret_url,
            )

        self.assertEqual(raised.exception.status_code, 502)
        detail = str(raised.exception.detail)
        self.assertIn("HTTP 403", detail)
        self.assertNotIn("secret-value", detail)
        self.assertNotIn("signature=private", detail)

    async def test_local_transport_fixture_preserves_http_flv_and_mpegts_bytes_and_headers(self):
        main_mod = _main_module()
        real_async_client = httpx.AsyncClient

        for stream_type, expected_media_type, chunks in (
            (
                "http_flv",
                "video/x-flv",
                [b"FLV\x01\x05\x00\x00\x00\x09", b"\x00" * 32, b"FLV-tail"],
            ),
            (
                "mpegts",
                "video/MP2T",
                [b"\x47" + bytes(range(187)), b"\x47" + bytes(range(187))],
            ),
        ):
            seen_headers = {}

            class FixtureStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for chunk in chunks:
                        yield chunk

            async def handle(request):
                seen_headers.update(dict(request.headers))
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/octet-stream"},
                    stream=FixtureStream(),
                    request=request,
                )

            def client_factory(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handle)
                return real_async_client(*args, **kwargs)

            async def allow_fixture_target(_url, **_kwargs):
                return None

            with mock.patch.object(main_mod.httpx, "AsyncClient", client_factory):
                with mock.patch("infrastructure.http_client.assert_safe_target_url", new=allow_fixture_target):
                    response = await main_mod.serve_iptv_proxy_stream_response(
                        request=FakeRequest(disconnected_after=6),
                        upstream_url="http://fixture.test/live?token=fixture-secret",
                        upstream_headers={
                            "User-Agent": "fixture-ua",
                            "Referer": "https://origin.example/",
                            "Cookie": "fixture-cookie",
                        },
                        stream_type=stream_type,
                    )
                    output = await _consume_response(response)

            self.assertEqual(b"".join(output), b"".join(chunks))
            self.assertEqual(response.media_type, expected_media_type)
            self.assertEqual(seen_headers["user-agent"], "fixture-ua")
            self.assertEqual(seen_headers["referer"], "https://origin.example/")
            self.assertEqual(seen_headers["cookie"], "fixture-cookie")
            self.assertEqual(seen_headers["accept-encoding"], "identity")

if __name__ == "__main__":
    unittest.main()
