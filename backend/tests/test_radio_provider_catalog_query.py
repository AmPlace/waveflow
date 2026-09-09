from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from routers.radio import query_radio_provider_catalog


class _Runtime:
    def __init__(self):
        self.calls = []

    async def request(self, instance, method, payload, *, timeout):
        self.calls.append((instance, method, payload, timeout))
        return {"stations": [], "next_cursor": "100"}


def _request(*, state="HEALTHY_ACTIVE", health="healthy", catalog=True):
    contract = SimpleNamespace(contract="radio_provider", features=("catalog",) if catalog else ())
    manifest = SimpleNamespace(
        identity="org.waveflow/radiobrowser",
        provider_contracts=(contract,),
        owned_schemes=(("radiobrowser", "radio_provider"),),
    )
    runtime = _Runtime()
    instance = SimpleNamespace(state=state, health=health, manifest=manifest)
    service = SimpleNamespace(_active={"org.waveflow/radiobrowser": instance}, runtime=runtime)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        plugin_subsystem=SimpleNamespace(service=service),
    ))), runtime


class RadioProviderCatalogQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_provider_owned_query_without_writing_the_home_catalog(self):
        request, runtime = _request()
        payload = {"country": "DE", "query": "News", "cursor": "200", "page_size": 100}

        result = await query_radio_provider_catalog(
            "org.waveflow/radiobrowser", request, payload,
        )

        self.assertEqual(result, {"stations": [], "next_cursor": "100"})
        self.assertEqual(len(runtime.calls), 1)
        _instance, method, forwarded, timeout = runtime.calls[0]
        self.assertEqual((method, forwarded, timeout), ("radio.catalog", payload, 30.0))

    async def test_rejects_unhealthy_or_non_catalog_provider(self):
        for kwargs in ({"state": "INSTALLED_DISABLED"}, {"health": "unhealthy"}, {"catalog": False}):
            with self.subTest(kwargs=kwargs):
                request, runtime = _request(**kwargs)
                with self.assertRaises(HTTPException) as error:
                    await query_radio_provider_catalog("org.waveflow/radiobrowser", request, {})
                self.assertEqual(error.exception.status_code, 404)
                self.assertEqual(runtime.calls, [])

    async def test_rejects_identity_that_does_not_match_the_active_manifest(self):
        request, runtime = _request()
        with self.assertRaises(HTTPException) as error:
            await query_radio_provider_catalog("org.waveflow/other", request, {})
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(runtime.calls, [])

    async def test_rejects_non_object_query_before_the_plugin_boundary(self):
        request, runtime = _request()
        with self.assertRaises(HTTPException) as error:
            await query_radio_provider_catalog("org.waveflow/radiobrowser", request, ["DE"])
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(runtime.calls, [])
