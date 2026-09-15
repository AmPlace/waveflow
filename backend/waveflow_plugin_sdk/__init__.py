"""WaveFlow Python Plugin SDK for Plugin API 1.0 and IPC 1.1.

This package is a *convenience* layer over the canonical Plugin contract.  The
canonical contract is the Manifest V1 schema plus the framed JSON stdio IPC
protocol; it is implemented by ``backend/plugin_runtime`` and several official
Plugins hand-roll it without importing this SDK.  Nothing here may grant a
Plugin authority that the runtime does not already enforce.

Stability
---------
``V1_PUBLIC_SURFACE`` is the frozen SDK V1 contract.  Every name in it is
exercised by at least one official Plugin under ``backend/bundled_plugins`` or
by the SDK conformance suite.  It changes only with a major SDK version.

``PREVIEW_SURFACE`` is implemented and reachable, but no official Plugin adopts
it yet.  Preview names may change or be removed without a major SDK version and
must not be relied on by third-party Plugins.
"""
from .application import ChannelCatalogProvider, PluginApplication, RadioProvider, TVProvider, VisualMetadataProvider
from .capabilities import CapabilityClient, CapabilityResponse
from .errors import (
    AuthFailure,
    InvalidResource,
    NotLive,
    PluginError,
    RateLimited,
    TemporaryFailure,
    UpstreamFailure,
)
from .models import ChannelCatalog, ChannelCatalogItem, RadioReference, ResolveContext, StreamDescriptor, TVReference, VisualMetadata
from .resources import load_resource_text

SDK_VERSION = "0.1.0"
SDK_API_VERSION = "1.0"

#: Frozen SDK V1 contract.  See ``docs/development/plugin-sdk-v1.md``.
V1_PUBLIC_SURFACE = frozenset({
    # application / bootstrap
    "PluginApplication",
    # provider extension points validated by official Plugins
    "TVProvider",
    "RadioProvider",
    "VisualMetadataProvider",
    # typed reference and result models
    "TVReference",
    "RadioReference",
    "ResolveContext",
    "StreamDescriptor",
    "VisualMetadata",
    # capability gateway client (managed HTTP only)
    "CapabilityClient",
    "CapabilityResponse",
    # stable error contract
    "PluginError",
    "InvalidResource",
    "NotLive",
    "UpstreamFailure",
    "TemporaryFailure",
    "AuthFailure",
    "RateLimited",
    # packaged read-only resources
    "load_resource_text",
    # version metadata
    "SDK_VERSION",
    "SDK_API_VERSION",
})

#: Implemented but not adopted by any official Plugin.  Not part of SDK V1.
PREVIEW_SURFACE = frozenset({
    "ChannelCatalog",
    "ChannelCatalogItem",
    "ChannelCatalogProvider",
})

# ``import *`` exports the frozen V1 contract plus the two surface markers, so a
# caller can introspect the split.  Preview names are deliberately *not* in the
# star-import surface: they stay reachable through an explicit import, which is
# the speed bump that keeps "implemented but unstable" from becoming an
# accidental third-party dependency.  A conformance test asserts this shape.
__all__ = sorted(V1_PUBLIC_SURFACE | {"V1_PUBLIC_SURFACE", "PREVIEW_SURFACE"})
