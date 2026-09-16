from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

import httpx

import market
import ssrf_guard

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _MockAsyncClient:
    def __init__(self, handler):
        self._client = _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return await self._client.__aexit__(exc_type, exc, traceback)

    def stream(self, *args, **kwargs):
        return self._client.stream(*args, **kwargs)


class MarketHttpBoundaryTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ssrf_guard.reset_real_dns_cache_for_tests()
        self.settings = mock.patch.object(
            ssrf_guard,
            "get_effective_settings",
            new=mock.AsyncMock(return_value=SimpleNamespace(allow_private=False, allow_loopback=False)),
        )
        self.settings.start()

    async def asyncTearDown(self):
        self.settings.stop()
        ssrf_guard.reset_real_dns_cache_for_tests()

    async def test_fake_ip_public_answer_is_allowed_without_using_fake_ip_as_policy_answer(self):
        system = mock.AsyncMock(return_value=["198.18.21.178"])
        independent = mock.AsyncMock(return_value=["93.184.216.34"])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=independent
        ):
            result = await market._validate_fetch_url(" https://market.example/index.json ")

        self.assertEqual(result, "https://market.example/index.json")
        system.assert_awaited_once_with("market.example")
        independent.assert_awaited_once_with("market.example")

    async def test_fake_ip_private_answer_is_denied(self):
        with mock.patch.object(
            ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.21.179"])
        ), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(return_value=["192.168.1.20"])
        ):
            with self.assertRaises(market.MarketError) as raised:
                await market._validate_fetch_url("https://market.example/index.json")

        self.assertEqual(raised.exception.status_code, 400)

    async def test_dns_failure_keeps_market_bad_gateway_semantics(self):
        with mock.patch.object(
            ssrf_guard, "resolve_host", new=mock.AsyncMock(side_effect=OSError("resolver unavailable"))
        ):
            with self.assertRaises(market.MarketError) as raised:
                await market._validate_fetch_url("https://market.example/index.json")

        self.assertEqual(raised.exception.status_code, 502)

    async def test_private_ip_families_and_private_dns_are_rejected_through_market(self):
        for url in (
            "https://127.0.0.1/metadata",
            "https://169.254.169.254/metadata",
            "https://[::1]/metadata",
            "https://[fd00::1]/metadata",
            "https://[fe80::1]/metadata",
        ):
            with self.subTest(url=url), self.assertRaises(market.MarketError) as raised:
                await market._validate_fetch_url(url)
            self.assertEqual(raised.exception.status_code, 400)

        with mock.patch.object(
            ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["10.0.0.5"])
        ):
            with self.assertRaises(market.MarketError) as raised:
                await market._validate_fetch_url("https://private.example/metadata")
        self.assertEqual(raised.exception.status_code, 400)

    async def test_redirect_fake_ip_target_is_validated_before_request(self):
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "market.example":
                return httpx.Response(
                    302,
                    headers={"location": "https://cdn.example/final"},
                    request=request,
                )
            return httpx.Response(200, text="{}", request=request)

        system = mock.AsyncMock(side_effect=[["93.184.216.34"], ["198.18.21.180"]])
        independent = mock.AsyncMock(return_value=["93.184.216.35"])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=independent
        ), mock.patch.object(
            market.httpx, "AsyncClient", side_effect=lambda **_: _MockAsyncClient(handler)
        ):
            _final_url, body, _headers = await market.safe_http_fetch("https://market.example/start")

        self.assertEqual(body, "{}")
        self.assertEqual(seen, ["https://market.example/start", "https://cdn.example/final"])
        independent.assert_awaited_once_with("cdn.example")

    async def test_redirect_to_private_target_is_rejected_before_private_request(self):
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://private.example/metadata"},
                request=request,
            )

        system = mock.AsyncMock(side_effect=[["93.184.216.34"], ["10.0.0.5"]])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            market.httpx, "AsyncClient", side_effect=lambda **_: _MockAsyncClient(handler)
        ):
            with self.assertRaises(market.MarketError) as raised:
                await market.safe_http_fetch("https://market.example/start")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(seen, ["https://market.example/start"])

    async def test_allow_private_is_forwarded_to_shared_guard(self):
        guard = mock.AsyncMock()
        with mock.patch.object(market, "assert_safe_target_url", new=guard):
            await market._validate_fetch_url("https://private.example/index.json", allow_private=True)

        guard.assert_awaited_once_with(
            "https://private.example/index.json",
            allow_private=True,
            allow_loopback=True,
            allowed_schemes={"http", "https"},
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
