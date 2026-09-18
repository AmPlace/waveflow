"""Expected contents of the committed official distribution.

Several suites assert what a fresh bootstrap installs.  Those expectations used
to be hardcoded lists of the ten-Plugin distribution, so every release had to
edit them.  They are derived here instead, from the distribution itself, and
stay correct as the release grows.

Two distinctions matter:

* ``PUBLISHED_IDENTITIES`` is everything the distribution ships.
* ``BOOTSTRAP_IDENTITIES`` excludes the Plugins that gate on a high-risk network
  permission.  ``network.direct`` and ``network.allow_http`` both require an
  explicit admin approval, so those Plugins land as ``unavailable`` on a fresh
  bootstrap rather than installed.
"""

from __future__ import annotations

import json
from pathlib import Path


DISTRIBUTION_ROOT = Path(__file__).resolve().parents[1] / "official_plugins" / "distribution"
PLAN_PATH = DISTRIBUTION_ROOT.parent / "release-plan.json"


def published_market() -> dict:
    return json.loads((DISTRIBUTION_ROOT / "market.json").read_text(encoding="utf-8"))


def identity(package: dict) -> str:
    manifest = package["plugin_manifest"]
    return f"{manifest['publisher_id']}/{manifest['plugin_id']}"


def needs_high_risk_approval(package: dict) -> bool:
    network = (package["plugin_manifest"].get("permissions") or {}).get("network") or {}
    return network.get("direct") is True or network.get("allow_http") is True


PUBLISHED_PACKAGES = tuple(published_market()["packages"])
PUBLISHED_IDENTITIES = frozenset(identity(package) for package in PUBLISHED_PACKAGES)
APPROVAL_REQUIRED_IDENTITIES = frozenset(
    identity(package) for package in PUBLISHED_PACKAGES if needs_high_risk_approval(package)
)
BOOTSTRAP_IDENTITIES = PUBLISHED_IDENTITIES - APPROVAL_REQUIRED_IDENTITIES

PUBLISHED_SCHEMES = frozenset(
    scheme["scheme"]
    for package in PUBLISHED_PACKAGES
    for scheme in (package["plugin_manifest"].get("owned_schemes") or [])
)
BOOTSTRAP_SCHEMES = frozenset(
    scheme["scheme"]
    for package in PUBLISHED_PACKAGES
    if identity(package) in BOOTSTRAP_IDENTITIES
    for scheme in (package["plugin_manifest"].get("owned_schemes") or [])
)

from tests.plugin_sources import plugin_source  # noqa: E402

PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
PLAN_IDENTITIES = frozenset(f"org.waveflow/{item['plugin_id']}" for item in PLAN["plugins"])
#: Plan entries that opt into the staged ownership rollout.  The three Plugins
#: without a ``rollout`` policy are the ones that also gate on a high-risk
#: approval, so they never take ownership automatically.
ROLLOUT_IDENTITIES = frozenset(
    f"org.waveflow/{item['plugin_id']}" for item in PLAN["plugins"] if item.get("rollout")
)
#: Those rollout entries that actually take ownership after a fresh bootstrap.
#: A Plugin that still waits on its high-risk approval never activates, so it is
#: not rolled out either.
ROLLED_OUT_IDENTITIES = ROLLOUT_IDENTITIES & BOOTSTRAP_IDENTITIES
MARKET_VERSION = published_market()["market_version"]

#: The version each Plugin source declares, so a test that builds from source
#: asserts against the source instead of a literal that goes stale on a bump.
SOURCE_VERSIONS = {
    item["plugin_id"]: json.loads(
        (plugin_source(item["source"]) / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    for item in PLAN["plugins"]
}
