from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

import httpx

import plugin_capabilities
import ssrf_guard
from plugin_capabilities import CapabilityGateway
from plugin_runtime import PluginError


class _ManagedGate:
    def require(self, name: str):
        if name != "network":
            raise AssertionError(name)
        return {"managed": True, "allowed_hosts": ["public.example", "redirect.example"]}


class FakeIpManagedHttpTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ssrf_guard.reset_real_dns_cache_for_tests()
        self.settings = mock.patch.object(
            ssrf_guard,
            "get_effective_settings",
            new=mock.AsyncMock(return_value=SimpleNamespace(allow_private=False, allow_loopback=False)),
        )
        self.settings.start()
        self.actual_guard = mock.patch.object(
            plugin_capabilities, "assert_safe_target_url", ssrf_guard.assert_safe_target_url,
        )
        self.actual_guard.start()

    async def asyncTearDown(self):
        self.actual_guard.stop()
        self.settings.stop()
        ssrf_guard.reset_real_dns_cache_for_tests()

    async def _fetch(self, url: str, handler=None):
        handler = handler or (lambda request: httpx.Response(200, text="ok", request=request))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            gateway = CapabilityGateway(client=client)
            return await gateway.managed_http_fetch(
                "org.waveflow/fake-ip", _ManagedGate(),
                {"url": url, "response_mode": "text"}, timeout=1,
            )
        finally:
            await client.aclose()

    async def test_fake_ip_public_real_answer_allows_and_request_keeps_hostname(self):
        independent = mock.AsyncMock(return_value=["93.184.216.34"])
        system = mock.AsyncMock(return_value=["198.18.1.10"])
        seen = []
        with mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent), \
             mock.patch.object(ssrf_guard, "resolve_host", new=system):
            result = await self._fetch(
                "https://public.example/provider.json?room=42",
                lambda request: (seen.append(request.url), httpx.Response(200, text="ok", request=request))[1],
            )
        self.assertEqual(result["status"], 200)
        self.assertEqual(seen[0].host, "public.example")
        self.assertEqual(seen[0].path, "/provider.json")
        self.assertEqual(seen[0].query, b"room=42")
        system.assert_awaited_once_with("public.example")
        independent.assert_awaited_once_with("public.example")

    async def test_fake_ip_private_real_answer_is_denied(self):
        with mock.patch.object(ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.11"])), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(return_value=["127.0.0.1"])):
            with self.assertRaises(PluginError) as raised:
                await self._fetch("https://public.example/provider.json")
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_fake_ip_rfc1918_real_answer_is_denied(self):
        with mock.patch.object(ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.12"])), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(return_value=["10.0.0.5"])):
            with self.assertRaises(PluginError) as raised:
                await self._fetch("https://public.example/provider.json")
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_fake_ip_independent_resolution_failure_fails_closed(self):
        with mock.patch.object(ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.13"])), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(PluginError) as raised:
                await self._fetch("https://public.example/provider.json")
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_ordinary_public_dns_does_not_use_independent_path(self):
        system = mock.AsyncMock(return_value=["93.184.216.34"])
        independent = mock.AsyncMock(return_value=["203.0.113.8"])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent):
            await self._fetch("https://public.example/provider.json")
        independent.assert_not_awaited()
        system.assert_awaited_once_with("public.example")

    async def test_existing_loopback_and_private_literal_blocks_remain(self):
        with mock.patch.object(ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["127.0.0.1"])):
            with self.assertRaises(PluginError) as raised:
                await self._fetch("https://public.example/provider.json")
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")
        with self.assertRaises(ssrf_guard.UnsafeTargetError):
            await ssrf_guard.assert_safe_target_url("https://127.0.0.1/provider.json")
        with self.assertRaises(ssrf_guard.UnsafeTargetError):
            await ssrf_guard.assert_safe_target_url("https://198.18.1.10/provider.json")

    async def test_mixed_public_and_blocked_independent_answers_are_denied(self):
        with mock.patch.object(ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.14"])), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(return_value=["93.184.216.34", "10.0.0.5"])):
            with self.assertRaises(PluginError) as raised:
                await self._fetch("https://public.example/provider.json")
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_redirect_fake_ip_target_is_revalidated_and_original_hosts_are_used(self):
        system = mock.AsyncMock(side_effect=[["93.184.216.34"], ["198.18.1.15"]])
        independent = mock.AsyncMock(return_value=["93.184.216.35"])
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "public.example":
                return httpx.Response(302, headers={"Location": "https://redirect.example/final"}, request=request)
            return httpx.Response(200, text="ok", request=request)

        with mock.patch.object(ssrf_guard, "resolve_host", new=system), \
             mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent):
            result = await self._fetch("https://public.example/start", handler)
        self.assertEqual(result["status"], 200)
        self.assertEqual([url.split("/", 3)[2] for url in seen], ["public.example", "redirect.example"])
        independent.assert_awaited_once_with("redirect.example")

    async def test_independent_resolver_follows_cname_before_authorization(self):
        def doh_query(_endpoint, host, qtype, _timeout):
            if host == "alias.example":
                return set(), {"target.example"}
            if host == "target.example":
                return ({"93.184.216.34"} if qtype == "A" else set()), set()
            raise AssertionError(host)

        with mock.patch.object(ssrf_guard, "_configured_real_dns_endpoints", return_value=("https://dns.example/resolve",)), \
             mock.patch.object(ssrf_guard, "_doh_query", side_effect=doh_query):
            result = await ssrf_guard.resolve_host_independently("alias.example", timeout=1)
        self.assertEqual(result, ["93.184.216.34"])

    async def test_fake_ip_independent_resolution_is_short_cached(self):
        system = mock.AsyncMock(return_value=["198.18.1.20"])
        independent = mock.AsyncMock(return_value=["93.184.216.34"])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=independent
        ):
            await ssrf_guard.assert_safe_target_url("https://public.example/one")
            await ssrf_guard.assert_safe_target_url("https://public.example/two")

        self.assertEqual(system.await_count, 2)
        independent.assert_awaited_once_with("public.example")

    async def test_fake_ip_independent_resolution_is_single_flight(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def independent(host):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return ["93.184.216.34"]

        with mock.patch.object(
            ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.21"])
        ), mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent):
            tasks = [asyncio.create_task(
                ssrf_guard.assert_safe_target_url("https://public.example/live")
            ) for _ in range(20)]
            await entered.wait()
            release.set()
            await asyncio.gather(*tasks)

        self.assertEqual(calls, 1)

    async def test_cached_dns_answer_does_not_cache_effective_policy(self):
        self.settings.stop()
        effective = mock.AsyncMock(side_effect=[
            SimpleNamespace(allow_private=True, allow_loopback=False),
            SimpleNamespace(allow_private=False, allow_loopback=False),
        ])
        independent = mock.AsyncMock(return_value=["10.0.0.8"])
        try:
            with mock.patch.object(ssrf_guard, "get_effective_settings", new=effective), mock.patch.object(
                ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.22"])
            ), mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent):
                await ssrf_guard.assert_safe_target_url("https://private.example/one")
                with self.assertRaises(ssrf_guard.UnsafeTargetError):
                    await ssrf_guard.assert_safe_target_url("https://private.example/two")
        finally:
            self.settings.start()

        independent.assert_awaited_once_with("private.example")

    async def test_fake_ip_waiter_cancellation_does_not_cancel_owner(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def independent(_host):
            entered.set()
            await release.wait()
            return ["93.184.216.34"]

        with mock.patch.object(
            ssrf_guard, "resolve_host", new=mock.AsyncMock(return_value=["198.18.1.23"])
        ), mock.patch.object(ssrf_guard, "resolve_host_independently", new=independent):
            owner = asyncio.create_task(ssrf_guard.assert_safe_target_url("https://public.example/owner"))
            await entered.wait()
            waiter = asyncio.create_task(ssrf_guard.assert_safe_target_url("https://public.example/waiter"))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await owner

        self.assertEqual(ssrf_guard._REAL_DNS_INFLIGHT, {})

    async def test_fake_ip_owner_cancellation_cleans_inflight_and_allows_retry(self):
        entered = asyncio.Event()

        async def blocked(_host):
            entered.set()
            await asyncio.Event().wait()

        system = mock.AsyncMock(return_value=["198.18.1.24"])
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=blocked
        ):
            owner = asyncio.create_task(ssrf_guard.assert_safe_target_url("https://retry.example/one"))
            await entered.wait()
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner

        self.assertEqual(ssrf_guard._REAL_DNS_INFLIGHT, {})
        with mock.patch.object(ssrf_guard, "resolve_host", new=system), mock.patch.object(
            ssrf_guard, "resolve_host_independently", new=mock.AsyncMock(return_value=["93.184.216.34"])
        ):
            await ssrf_guard.assert_safe_target_url("https://retry.example/two")


if __name__ == "__main__":
    import unittest

    unittest.main()
