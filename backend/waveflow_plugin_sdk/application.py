from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import uuid
import re
from abc import ABC, abstractmethod
from typing import Any, BinaryIO

from .capabilities import CapabilityClient, capability_error
from .errors import PluginError
from .models import ChannelCatalog, RadioReference, ResolveContext, StreamDescriptor, TVReference, VisualMetadata


#: Canonical scheme grammar shared with Core.  It must stay identical to
#: ``plugin_runtime.manifest.SCHEME_RE``; the SDK ships standalone inside the
#: built ``.pyz`` artifact and cannot import Core.  A test asserts the two
#: patterns remain equal, because a divergence makes a manifest pass
#: ``validate`` and then crash when the Plugin registers its provider.
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{1,31}$")
SCHEME_FORMAT = "2-32 chars, [a-z] followed by [a-z0-9+.-]"


def _normalize_scheme(scheme: str, label: str) -> str:
    """Normalize and validate a single provider scheme.

    Every registration entry point funnels through this helper so ``register_tv``,
    ``register_radio`` and ``register_tv_visual`` cannot drift apart.  Core
    re-validates the same grammar from the manifest and again when the Plugin
    hello declares its owned schemes, so a scheme accepted here must also be
    accepted there -- otherwise registration succeeds and the Plugin is rejected
    later with an unrelated manifest error.
    """
    normalized = str(scheme or "").strip().lower()
    if not SCHEME_RE.fullmatch(normalized):
        raise ValueError(f"{label} provider scheme is invalid ({SCHEME_FORMAT}): {scheme!r}")
    return normalized


class TVProvider(ABC):
    @abstractmethod
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        raise NotImplementedError


class VisualMetadataProvider(ABC):
    """Optional source-scoped TV visual metadata provider."""

    @abstractmethod
    def visual_metadata(self, reference: TVReference, context: ResolveContext) -> VisualMetadata | dict[str, Any]:
        raise NotImplementedError


class ChannelCatalogProvider(ABC):
    """Optional runtime catalog provider; it does not own Core channel storage."""

    @abstractmethod
    def discover_channels(self, context: ResolveContext) -> ChannelCatalog | dict[str, Any]:
        raise NotImplementedError


class RadioProvider(ABC):
    def catalog(self, payload: dict[str, Any], context: ResolveContext) -> dict[str, Any]:
        raise PluginError("RESOURCE_NOT_FOUND", "Radio catalog is not implemented")

    @abstractmethod
    def resolve_stream(self, reference: RadioReference, context: ResolveContext) -> StreamDescriptor:
        raise NotImplementedError

    def programme(self, reference: RadioReference, context: ResolveContext) -> dict[str, Any]:
        raise PluginError("RESOURCE_NOT_FOUND", "Radio programme is not implemented")


class PluginApplication:
    def __init__(self, *, identity: str, version: str, permissions: list[str] | None = None):
        self.identity = identity
        self.version = version
        self.permissions = list(permissions or [])
        self._tv: dict[str, TVProvider] = {}
        self._tv_visual: dict[str, VisualMetadataProvider] = {}
        self._radio: dict[str, RadioProvider] = {}
        self._channel_catalog: ChannelCatalogProvider | None = None
        self._write_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._callbacks: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}
        self._active_lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._input: BinaryIO = sys.stdin.buffer
        self._output: BinaryIO = sys.stdout.buffer

    def register_tv(self, scheme: str, provider: TVProvider) -> "PluginApplication":
        self._register_scheme(self._tv, self._radio, scheme, provider, "TV")
        return self

    def register_tv_visual(self, scheme: str, provider: VisualMetadataProvider) -> "PluginApplication":
        # Visual schemes are validated like any other scheme: Core requires the
        # manifest to declare every one of them and rejects the hello when they
        # do not match.  Duplicates are rejected here rather than silently
        # overwriting an earlier provider.
        normalized = _normalize_scheme(scheme, "TV visual")
        if normalized in self._tv_visual:
            raise ValueError(f"Provider scheme is registered twice: {normalized}")
        self._tv_visual[normalized] = provider
        return self

    def register_radio(self, scheme: str, provider: RadioProvider) -> "PluginApplication":
        self._register_scheme(self._radio, self._tv, scheme, provider, "Radio")
        return self

    @staticmethod
    def _register_scheme(target: dict[str, Any], other: dict[str, Any], scheme: str,
                         provider: Any, label: str) -> None:
        normalized = _normalize_scheme(scheme, label)
        if normalized in target or normalized in other:
            raise ValueError(f"Provider scheme is registered twice: {normalized}")
        target[normalized] = provider

    def register_channel_catalog(self, provider: ChannelCatalogProvider) -> "PluginApplication":
        self._channel_catalog = provider
        return self

    def _write(self, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        frame = f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode() + body
        with self._write_lock:
            self._output.write(frame)
            self._output.flush()

    def _read(self) -> dict[str, Any] | None:
        headers = {}
        while True:
            line = self._input.readline()
            if not line:
                return None
            if line == b"\r\n":
                break
            name, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
            headers[name.lower()] = value
        return json.loads(self._input.read(int(headers["content-length"])).decode())

    def _response(self, request: dict[str, Any], result: Any = None, error: PluginError | None = None) -> None:
        self._write({"protocol_version": "1.1", "kind": "response", "sender": "plugin",
                     "request_id": request["request_id"], "status": "error" if error else "ok",
                     "result": None if error else result, "error": error.as_contract() if error else None,
                     "diagnostics": {}})

    def _capability(self, parent: dict[str, Any], method: str, payload: dict[str, Any], timeout: float) -> Any:
        parent_id = str(parent["request_id"])
        with self._active_lock:
            cancelled = self._active.get(parent_id)
        if cancelled is None or cancelled.is_set():
            raise PluginError("PLUGIN_CANCELLED", "Provider request was cancelled", category="lifecycle")
        request_id = f"plugin:{uuid.uuid4().hex}"
        event = threading.Event()
        with self._callback_lock:
            self._callbacks[request_id] = (event, None)
        self._write({"protocol_version": "1.1", "kind": "request", "sender": "plugin",
                     "request_id": request_id, "method": method,
                     "plugin_instance": parent.get("plugin_instance"),
                     "deadline_unix_ms": int((time.time() + timeout) * 1000),
                     "context": {"parent_request_id": parent["request_id"]}, "payload": payload})
        deadline = time.monotonic() + timeout
        while not event.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
            if cancelled.is_set():
                with self._callback_lock:
                    self._callbacks.pop(request_id, None)
                raise PluginError("PLUGIN_CANCELLED", "Provider request was cancelled", category="lifecycle")
            if time.monotonic() >= deadline:
                break
        if not event.is_set():
            with self._callback_lock:
                self._callbacks.pop(request_id, None)
            raise PluginError("PLUGIN_TIMEOUT", "Core capability timed out", retryable=True, category="timeout")
        with self._callback_lock:
            response = self._callbacks.pop(request_id)[1] or {}
        if response.get("status") == "error":
            raise capability_error(response.get("error") or {})
        return response.get("result") or {}

    def _context(self, request: dict[str, Any]) -> ResolveContext:
        request_id = str(request["request_id"])
        with self._active_lock:
            cancelled = self._active[request_id]
        capabilities = CapabilityClient(lambda method, payload, timeout: self._capability(request, method, payload, timeout))
        return ResolveContext(request_id, int(request.get("deadline_unix_ms") or 0),
                              dict(request.get("context") or {}), capabilities, cancelled.is_set)

    def _provider_response(self, request: dict[str, Any], result: Any = None,
                           error: PluginError | None = None) -> None:
        with self._active_lock:
            cancelled = self._active.get(str(request["request_id"]))
        if cancelled is not None and not cancelled.is_set():
            self._response(request, result, error)

    def _invoke(self, request: dict[str, Any]) -> None:
        try:
            method = request.get("method")
            payload = request.get("payload") or {}
            context = self._context(request)
            if method == "tv.resolve_stream":
                reference = TVReference.from_payload(payload)
                provider = self._tv.get(reference.scheme)
                if provider is None:
                    raise PluginError("RESOURCE_NOT_FOUND",
                                      f"TV Provider scheme is not registered: {reference.scheme[:32]}")
                result = provider.resolve_stream(reference, context)
                self._provider_response(request, result.as_contract())
            elif method == "tv.visual_metadata":
                reference = TVReference.from_payload(payload)
                provider = self._tv_visual.get(reference.scheme)
                if provider is None:
                    raise PluginError("RESOURCE_NOT_FOUND", "TV visual metadata is not registered")
                result = provider.visual_metadata(reference, context)
                self._provider_response(request, result.as_contract() if isinstance(result, VisualMetadata) else result)
            elif method == "radio.catalog":
                # Core addresses the Plugin for a catalog query; the payload
                # carries no provider key, so a Plugin owning exactly one Radio
                # provider is unambiguous.  More than one provider is a
                # programming error rather than something to guess our way out of.
                provider = next(iter(self._radio.values()), None) if len(self._radio) == 1 else None
                if provider is None:
                    raise PluginError("RESOURCE_NOT_FOUND", "Radio Provider is not registered")
                self._provider_response(request, provider.catalog(payload, context))
            elif method == "radio.resolve_stream":
                reference = RadioReference.from_payload(payload)
                provider = self._radio.get(reference.provider_key)
                if provider is None:
                    raise PluginError(
                        "RESOURCE_NOT_FOUND",
                        f"Radio Provider is not registered: {reference.provider_key[:32]}",
                    )
                self._provider_response(request, provider.resolve_stream(reference, context).as_contract())
            elif method == "radio.programme":
                reference = RadioReference.from_payload(payload)
                provider = self._radio.get(reference.provider_key)
                if provider is None:
                    raise PluginError(
                        "RESOURCE_NOT_FOUND",
                        f"Radio Provider is not registered: {reference.provider_key[:32]}",
                    )
                self._provider_response(request, provider.programme(reference, context))
            elif method == "channel_catalog.discover":
                if self._channel_catalog is None:
                    raise PluginError("RESOURCE_NOT_FOUND", "Channel catalog is not registered")
                result = self._channel_catalog.discover_channels(context)
                self._provider_response(request, result.as_contract() if isinstance(result, ChannelCatalog) else result)
            else:
                raise PluginError("RESOURCE_NOT_FOUND", "Provider method is not supported")
        except PluginError as exc:
            self._provider_response(request, error=exc)
        except Exception:
            # The failure is reported through the contract with a generic
            # message; the real cause goes to stderr only.  Plugin stderr is
            # captured, bounded and sanitized by Core, is never part of
            # ``as_contract``, and is surfaced solely by the SDK ``test``
            # harness and the developer-local install log.  A swallowed
            # traceback is why a missing packaged resource used to look like an
            # opaque upstream failure.
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            self._provider_response(request, error=PluginError(
                "TEMPORARY_UPSTREAM_FAILURE", "Provider handler failed", retryable=True,
            ))
        finally:
            with self._active_lock:
                self._active.pop(str(request["request_id"]), None)

    def hello(self) -> dict[str, Any]:
        contracts = []
        capabilities = []
        schemes = []
        if self._tv:
            contracts.append({"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]})
            capabilities.append("tv.resolve_stream")
            schemes.extend({"scheme": scheme, "contract": "tv_provider"} for scheme in self._tv)
        if self._tv_visual:
            contracts.append({
                "contract": "tv_visual_provider", "contract_version": "1.0",
                "features": ["metadata"], "schemes": sorted(self._tv_visual),
            })
            capabilities.append("tv.visual_metadata")
        if self._radio:
            features = ["catalog", "resolve_stream"]
            if any(type(provider).programme is not RadioProvider.programme for provider in self._radio.values()):
                features.append("programme")
            contracts.append({"contract": "radio_provider", "contract_version": "1.0", "features": features})
            capabilities.extend(["radio.catalog", "radio.resolve_stream"])
            if "programme" in features:
                capabilities.append("radio.programme")
            schemes.extend({"scheme": scheme, "contract": "radio_provider"} for scheme in self._radio)
        if self._channel_catalog is not None:
            contracts.append({"contract": "channel_catalog", "contract_version": "1.0", "features": ["discover"]})
            capabilities.append("channel_catalog.discover")
        return {"protocol_version": "1.1", "plugin": self.identity, "version": self.version,
                "provider_contracts": contracts, "owned_schemes": schemes,
                "capabilities": capabilities, "permissions": self.permissions}

    def run(self, *, input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None) -> None:
        if input_stream is not None:
            self._input = input_stream
        if output_stream is not None:
            self._output = output_stream
        while True:
            request = self._read()
            if request is None:
                return
            if request.get("kind") == "response" and request.get("sender") == "core":
                with self._callback_lock:
                    pending = self._callbacks.get(request.get("request_id"))
                    if pending:
                        self._callbacks[request["request_id"]] = (pending[0], request)
                        pending[0].set()
                continue
            method = request.get("method")
            if method == "runtime.hello":
                self._response(request, self.hello())
            elif method == "runtime.health":
                self._response(request, {"healthy": True})
            elif method == "runtime.shutdown":
                with self._active_lock:
                    for cancelled in self._active.values():
                        cancelled.set()
                self._response(request, {"accepted": True})
                return
            elif method == "runtime.cancel":
                target = str((request.get("payload") or {}).get("request_id") or "")
                with self._active_lock:
                    cancelled = self._active.get(target)
                    if cancelled is not None:
                        cancelled.set()
            else:
                request_id = str(request.get("request_id") or "")
                with self._active_lock:
                    self._active[request_id] = threading.Event()
                threading.Thread(target=self._invoke, args=(request,), daemon=True).start()

    @staticmethod
    def identity_args(default_identity: str, default_version: str = "1.0.0") -> tuple[str, str]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--identity", default=default_identity)
        parser.add_argument("--version", default=default_version)
        args, _unknown = parser.parse_known_args()
        return args.identity, args.version
