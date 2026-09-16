"""Backend authority for installed package logos.

Package logos are stable, package-scoped candidates.  Provider visual
metadata remains source-scoped and is intentionally not written here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import database as db


PACKAGE_ASSET_URL_PREFIX = "/api/media/package-assets"


def _asset_url(package_id: str, asset_id: str) -> str:
    return (
        f"{PACKAGE_ASSET_URL_PREFIX}/{quote(package_id, safe='')}/"
        f"{quote(asset_id, safe='')}"
    )


class LogoResolver:
    """Resolve a logical channel's stable logo without changing its identity."""

    async def resolve_many(self, channels: list[dict]) -> dict[str, dict]:
        logical_ids = [str(channel.get("logical_channel_id") or "") for channel in channels]
        bindings = await db.get_logical_channel_logo_bindings_many(logical_ids)
        result: dict[str, dict] = {}
        for channel in channels:
            logical_id = str(channel.get("logical_channel_id") or "")
            chosen = None
            for binding in bindings.get(logical_id, []):
                path = Path(str(binding.get("stored_path") or ""))
                if not path.is_file():
                    # A missing package file must never make a channel
                    # disappear; the normal channel logo fallback remains.
                    continue
                chosen = {
                    "logo_url": _asset_url(
                        str(binding["package_id"]), str(binding["asset_id"])
                    ),
                    "source_type": str(binding.get("binding_type") or "package"),
                    "package_id": str(binding["package_id"]),
                    "asset_id": str(binding["asset_id"]),
                    "match_type": str(binding.get("match_type") or ""),
                    "provenance": "installed_package_asset",
                }
                break
            if chosen:
                result[logical_id] = chosen
        return result


logo_resolver = LogoResolver()


__all__ = ["LogoResolver", "logo_resolver"]
