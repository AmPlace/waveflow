import httpx
import unittest
from unittest import mock

from infrastructure import http_client


class _ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123"
        yield b"45"


class SubscriptionHttpBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def _fetch_with_transport(
        self,
        handler,
        *,
        url="https://source.test/list.m3u",
        headers=None,
        max_bytes=50,
    ):
        real_async_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            return real_async_client(transport=transport, **kwargs)

        with mock.patch.object(
            http_client.httpx,
            "AsyncClient",
            side_effect=client_factory,
        ), mock.patch.object(
            http_client,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ):
            return await http_client.fetch_bytes(
                url,
                headers=headers,
                policy=http_client.FetchPolicy(
                    allow_private=True,
                    allow_loopback=True,
                    max_response_bytes=max_bytes,
                ),
            )

    async def test_declared_response_size_is_rejected_before_body_read(self):
        body_read = False

        def handler(request):
            nonlocal body_read
            body_read = True
            return httpx.Response(
                200,
                headers={"content-length": "51"},
                content=b"small",
                request=request,
            )

        with self.assertRaises(http_client.ResponseTooLargeError):
            await self._fetch_with_transport(handler, max_bytes=50)

        self.assertTrue(body_read)

    async def test_streamed_response_size_is_rejected_without_content_length(self):
        def handler(request):
            return httpx.Response(200, stream=_ChunkStream(), request=request)

        with self.assertRaises(http_client.ResponseTooLargeError):
            await self._fetch_with_transport(handler, max_bytes=4)

    async def test_cross_origin_redirect_drops_sensitive_headers(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"location": "https://cdn.test/final"},
                    request=request,
                )
            return httpx.Response(200, content=b"#EXTM3U\n", request=request)

        await self._fetch_with_transport(
            handler,
            url="https://source.test/start",
            headers={
                "Authorization": "Bearer fixture-secret",
                "Cookie": "session=fixture-secret",
                "User-Agent": "WaveFlow-Test/1",
            },
        )

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].headers["authorization"], "Bearer fixture-secret")
        self.assertEqual(seen[0].headers["cookie"], "session=fixture-secret")
        self.assertNotIn("authorization", seen[1].headers)
        self.assertNotIn("cookie", seen[1].headers)
        self.assertEqual(seen[1].headers["user-agent"], "WaveFlow-Test/1")


if __name__ == "__main__":
    unittest.main()
