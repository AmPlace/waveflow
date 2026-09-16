from .errors import PluginError
from .manifest import PluginManifest, load_manifest, validate_manifest
from .permissions import MemoryCapabilityStubs, PermissionGate, PermissionPolicy
from .protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, encode_frame, read_frame
from .registry import LifecycleState, PluginInstance, PluginRegistry
from .runtime import PluginRuntime
from .validation import (
    validate_channel_catalog, validate_descriptor_metadata, validate_station_ref, validate_stream_descriptor,
    validate_visual_metadata,
)

__all__ = [
    "LifecycleState", "MAX_FRAME_BYTES", "MemoryCapabilityStubs", "PROTOCOL_VERSION", "PermissionGate",
    "PermissionPolicy", "PluginError", "PluginInstance", "PluginManifest", "PluginRegistry", "PluginRuntime",
    "encode_frame", "load_manifest", "read_frame", "validate_manifest", "validate_station_ref",
    "validate_channel_catalog", "validate_descriptor_metadata", "validate_stream_descriptor", "validate_visual_metadata",
]
