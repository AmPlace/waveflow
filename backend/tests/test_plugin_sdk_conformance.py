"""Conformance guards for the Plugin SDK V1 public contract.

These tests do not exercise Plugin *behaviour*; they exist so the three places
that state the contract cannot drift apart silently:

* ``waveflow_plugin_sdk.__init__`` -- what a third party may import,
* ``plugin_runtime`` -- what Core actually implements and enforces,
* ``plugin_capabilities`` -- which Core capabilities really have a handler.

Every guard below fails on *disagreement*, not on a hardcoded copy of the
contract, so renaming or promoting a name is a deliberate one-line change in the
SDK plus a review of the matching assertion here.
"""
from __future__ import annotations

import inspect
import io
import json
import re
import time
import unittest

from plugin_capabilities import CoreCapabilityDispatcher
from plugin_runtime import (
    validate_stream_descriptor,
    validate_visual_metadata,
)
from plugin_runtime.errors import ERROR_CODES as RUNTIME_ERROR_CODES
from plugin_runtime.errors import PluginError as RuntimePluginError
from plugin_runtime.manifest import API_VERSION as CORE_API_VERSION
from plugin_runtime.manifest import SCHEME_RE as CORE_SCHEME_RE

import waveflow_plugin_sdk as sdk
from waveflow_plugin_sdk import (
    CapabilityResponse,
    InvalidResource,
    NotLive,
    PluginApplication,
    RadioProvider,
    RadioReference,
    RateLimited,
    StreamDescriptor,
    TVProvider,
    TVReference,
    TemporaryFailure,
    UpstreamFailure,
    VisualMetadata,
    VisualMetadataProvider,
)
from waveflow_plugin_sdk.application import SCHEME_RE as SDK_SCHEME_RE
from waveflow_plugin_sdk.capabilities import (
    PREVIEW_CAPABILITY_METHODS,
    V1_CAPABILITY_METHODS,
)


# Codes the SDK itself raises as part of the lifecycle contract, independent of
# the provider-facing error classes.  If one of these is missing from the runtime
# ``ERROR_CODES`` set, Core rewrites it to ``INVALID_PLUGIN_RESPONSE`` and the
# semantic is lost -- which is exactly how cancellation used to be swallowed.
SDK_LIFECYCLE_CODES = ("PLUGIN_CANCELLED", "PLUGIN_TIMEOUT")

STABLE_ERROR_CLASSES = (
    InvalidResource, NotLive, UpstreamFailure, TemporaryFailure, RateLimited,
)


def _dispatcher_methods() -> set[str]:
    """Core capability methods that ``CoreCapabilityDispatcher`` really handles."""
    source = inspect.getsource(CoreCapabilityDispatcher.dispatch)
    return set(re.findall(r'"(core\.[a-z._]+)"', source))


def _frame(request: dict) -> bytes:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    return (f"Content-Length: {len(body)}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n\r\n").encode() + body


def _frames(raw: bytes) -> list[dict]:
    frames: list[dict] = []
    pos = 0
    while pos < len(raw):
        head, _, rest = raw[pos:].partition(b"\r\n\r\n")
        if not head:
            break
        length = 0
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b": ")
            if name.decode().lower() == "content-length":
                length = int(value.decode())
        body = rest[:length]
        frames.append(json.loads(body))
        pos += len(head) + 4 + length
    return frames


def _drive(app: PluginApplication, method: str, payload: dict | None = None,
           timeout: float = 5.0) -> list[dict]:
    """Send one request through the real stdio loop and collect the responses.

    Provider methods are dispatched on a worker thread, so ``run`` returns as
    soon as stdin hits EOF; poll briefly for the frame instead of reaching into
    the SDK's internals.
    """
    out = io.BytesIO()
    app.run(input_stream=io.BytesIO(_frame({
        "protocol_version": "1.1", "kind": "request", "sender": "core",
        "request_id": "core:conformance", "method": method, "payload": payload or {},
    })), output_stream=out)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not out.getvalue():
        time.sleep(0.01)
    return _frames(out.getvalue())


class PublicSurfaceTest(unittest.TestCase):
    def test_star_import_matches_the_declared_surface(self):
        self.assertEqual(
            set(sdk.__all__),
            sdk.V1_PUBLIC_SURFACE | {"V1_PUBLIC_SURFACE", "PREVIEW_SURFACE"},
        )

    def test_every_declared_name_is_importable(self):
        for name in sorted(sdk.V1_PUBLIC_SURFACE | sdk.PREVIEW_SURFACE):
            with self.subTest(name=name):
                self.assertTrue(hasattr(sdk, name), f"{name} is declared but not exported")

    def test_preview_names_stay_out_of_the_star_import(self):
        for name in sorted(sdk.PREVIEW_SURFACE):
            with self.subTest(name=name):
                self.assertNotIn(name, sdk.__all__)

    def test_stable_and_preview_do_not_overlap(self):
        self.assertEqual(set(), sdk.V1_PUBLIC_SURFACE & sdk.PREVIEW_SURFACE)

    def test_version_metadata_agrees_with_core(self):
        self.assertIsInstance(sdk.SDK_VERSION, str)
        self.assertEqual(sdk.SDK_API_VERSION, CORE_API_VERSION)


class CapabilityAlignmentTest(unittest.TestCase):
    def test_stable_capabilities_have_a_runtime_handler(self):
        handled = _dispatcher_methods()
        for method in sorted(V1_CAPABILITY_METHODS):
            with self.subTest(method=method):
                self.assertIn(method, handled)

    def test_preview_capabilities_are_reachable_but_not_stable(self):
        handled = _dispatcher_methods()
        for method in sorted(PREVIEW_CAPABILITY_METHODS):
            with self.subTest(method=method):
                self.assertIn(method, handled)
                self.assertNotIn(method, V1_CAPABILITY_METHODS)

    def test_secret_stays_outside_the_stable_capability_set(self):
        self.assertNotIn("core.secret.get", V1_CAPABILITY_METHODS)

    def test_capability_client_exposes_the_stable_method(self):
        from waveflow_plugin_sdk import CapabilityClient
        self.assertTrue(callable(CapabilityClient.managed_http))
        response = CapabilityResponse(200, {"content-type": "text/plain"}, "body", "text")
        self.assertEqual((response.status, response.headers, response.body, response.response_mode),
                         (200, {"content-type": "text/plain"}, "body", "text"))


class ErrorContractTest(unittest.TestCase):
    def test_stable_error_codes_are_recognised_by_the_runtime(self):
        for cls in STABLE_ERROR_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertIn(cls().code, RUNTIME_ERROR_CODES)

    def test_sdk_lifecycle_codes_are_recognised_by_the_runtime(self):
        # Regression guard: an unrecognised code is silently rewritten to
        # INVALID_PLUGIN_RESPONSE, which erases cancellation and timeout signals.
        for code in SDK_LIFECYCLE_CODES:
            with self.subTest(code=code):
                self.assertIn(code, RUNTIME_ERROR_CODES)

    def test_unknown_codes_are_still_rewritten(self):
        self.assertEqual(RuntimePluginError("NOT_A_REAL_CODE", "x").code,
                         "INVALID_PLUGIN_RESPONSE")

    def test_stable_errors_declare_retryable_semantics(self):
        self.assertFalse(InvalidResource().retryable)
        self.assertFalse(sdk.AuthFailure().retryable)
        self.assertTrue(NotLive().retryable)
        self.assertTrue(RateLimited().retryable)
        self.assertTrue(UpstreamFailure().retryable)


class SchemeContractTest(unittest.TestCase):
    def test_sdk_and_core_use_the_same_scheme_grammar(self):
        self.assertEqual(SDK_SCHEME_RE.pattern, CORE_SCHEME_RE.pattern)

    def _fixture(self):
        class Provider(TVProvider):
            def resolve_stream(self, reference, context):
                return StreamDescriptor.hls("https://example.invalid/live.m3u8")

        return Provider()

    def _radio_fixture(self):
        class Provider(RadioProvider):
            def resolve_stream(self, reference, context):
                return StreamDescriptor.hls("https://example.invalid/radio.m3u8")

        return Provider()

    def test_every_registration_entry_rejects_invalid_schemes(self):
        # "Bad" is *not* here on purpose: every entry point normalizes case, so
        # it is a valid registration that Core sees as "bad".
        for scheme in ("", "bad scheme", "x" * 33, "1abc", "-abc", "a" * 33):
            for label, register in (
                ("register_tv", lambda app: app.register_tv(scheme, self._fixture())),
                ("register_radio", lambda app: app.register_radio(scheme, self._radio_fixture())),
                ("register_tv_visual", lambda app: app.register_tv_visual(scheme, self._fixture())),
            ):
                with self.subTest(scheme=scheme, entry=label):
                    app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
                    with self.assertRaises(ValueError):
                        register(app)

    def test_every_registration_entry_rejects_duplicates(self):
        for label, register in (
            ("register_tv", lambda app, scheme: app.register_tv(scheme, self._fixture())),
            ("register_radio", lambda app, scheme: app.register_radio(scheme, self._radio_fixture())),
            ("register_tv_visual", lambda app, scheme: app.register_tv_visual(scheme, self._fixture())),
        ):
            with self.subTest(entry=label):
                app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
                register(app, "fixture")
                with self.assertRaises(ValueError):
                    register(app, "fixture")

    def test_every_registration_entry_normalizes_case_and_whitespace(self):
        for label, register, read in (
            ("register_tv",
             lambda app, scheme: app.register_tv(scheme, self._fixture()),
             lambda app: [s["scheme"] for s in app.hello()["owned_schemes"]]),
            ("register_radio",
             lambda app, scheme: app.register_radio(scheme, self._radio_fixture()),
             lambda app: [s["scheme"] for s in app.hello()["owned_schemes"]]),
            ("register_tv_visual",
             lambda app, scheme: app.register_tv_visual(scheme, self._fixture()),
             lambda app: next(c for c in app.hello()["provider_contracts"]
                              if c["contract"] == "tv_visual_provider")["schemes"]),
        ):
            with self.subTest(entry=label):
                app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
                register(app, "  FIXTURE  ")
                self.assertEqual(read(app), ["fixture"])


class ProviderContractTest(unittest.TestCase):
    """Stable provider surfaces must be drivable through the real SDK."""

    def test_tv_provider_resolve_stream_reaches_a_valid_descriptor(self):
        seen: list[TVReference] = []

        class Provider(TVProvider):
            def resolve_stream(self, reference, context):
                seen.append(reference)
                return StreamDescriptor.hls("https://example.invalid/live.m3u8", ttl_seconds=60)

        app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
        app.register_tv("fixture", Provider())
        [response] = _drive(app, "tv.resolve_stream",
                            {"scheme": "fixture", "resource_id": "1"})
        self.assertEqual(response["status"], "ok")
        validate_stream_descriptor(response["result"])
        self.assertEqual((seen[0].scheme, seen[0].resource_id), ("fixture", "1"))

    def test_radio_provider_catalog_and_resolve_stream(self):
        class Provider(RadioProvider):
            def catalog(self, payload, context):
                return {"stations": [{"provider_key": "fixture", "provider_station_id": "1"}]}

            def resolve_stream(self, reference, context):
                return StreamDescriptor.hls("https://example.invalid/radio.m3u8", ttl_seconds=60)

        app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
        app.register_radio("fixture", Provider())
        [catalog] = _drive(app, "radio.catalog", {})
        self.assertEqual(catalog["status"], "ok")
        self.assertEqual(catalog["result"]["stations"][0]["provider_station_id"], "1")

        [resolved] = _drive(app, "radio.resolve_stream",
                            {"station_ref": {"provider_key": "fixture", "provider_station_id": "1"}})
        self.assertEqual(resolved["status"], "ok")
        validate_stream_descriptor(resolved["result"])

    def test_visual_metadata_provider_reaches_a_valid_payload(self):
        class Provider(VisualMetadataProvider):
            def visual_metadata(self, reference, context):
                return VisualMetadata(avatar_url="https://img.example/a.jpg", ttl_seconds=60)

        app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
        app.register_tv_visual("fixture", Provider())
        [response] = _drive(app, "tv.visual_metadata", {"scheme": "fixture", "resource_id": "1"})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(validate_visual_metadata(response["result"])["cover_role"], "live")

    def test_unregistered_scheme_reports_resource_not_found(self):
        app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
        [response] = _drive(app, "tv.resolve_stream", {"scheme": "absent", "resource_id": "1"})
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["code"], "RESOURCE_NOT_FOUND")
        self.assertFalse(response["error"]["retryable"])

    def test_radio_provider_defaults_report_resource_not_found(self):
        class Provider(RadioProvider):
            def resolve_stream(self, reference, context):
                return StreamDescriptor.hls("https://example.invalid/radio.m3u8")

        provider = Provider()
        for call in (lambda: provider.catalog({}, None),
                     lambda: provider.programme(RadioReference("fixture", "1"), None)):
            with self.subTest(call=call):
                with self.assertRaises(sdk.PluginError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "RESOURCE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
