"""Locate the official Plugin source tree from inside the Core repository.

Plugin sources no longer live in this repository.  Core tests that still need
real Plugin sources resolve them through an explicit external root:

* ``WAVEFLOW_PLUGIN_SOURCE_ROOT`` when set, pointing at the market repository
  root, or
* the sibling ``waveflow-market`` checkout otherwise.

The root is derived from this file's location, never from the working directory,
so the suites behave the same whatever ``pytest`` is invoked from.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKET_DIRECTORY_NAME = "waveflow-market"


def market_root() -> Path:
    """Return the repository that holds the extracted Plugin sources."""
    override = os.environ.get("WAVEFLOW_PLUGIN_SOURCE_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        candidate = Path(__file__).resolve().parents[2].parent / MARKET_DIRECTORY_NAME
    if not candidate.is_dir():
        raise RuntimeError(
            "The official Plugin source tree is unavailable; set "
            f"WAVEFLOW_PLUGIN_SOURCE_ROOT to the market checkout (looked at {candidate})",
        )
    return candidate


def plugins_root() -> Path:
    """Return the root holding Plugins that the release plan publishes."""
    return market_root() / "plugins"


def legacy_root() -> Path:
    """Return the root holding Plugins with no official release channel yet."""
    return market_root() / "legacy"


def plugin_source(plugin_id: str) -> Path:
    """Return the source directory of an officially released Plugin."""
    return plugins_root() / plugin_id


def all_plugin_source_files(name: str = "plugin.py") -> list[Path]:
    """Return every Plugin source file, official and legacy, sorted by path."""
    found: list[Path] = []
    for root in (plugins_root(), legacy_root()):
        if root.is_dir():
            found.extend(sorted(path / name for path in root.iterdir() if path.is_dir()))
    return found
