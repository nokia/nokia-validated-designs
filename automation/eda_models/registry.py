"""Centralized EDA API version, kind, and plural mappings.

Backward-compatible facade over :mod:`automation.eda_models.profiles`. The
module-level constants below are bound to the **default** EDA profile
(:data:`profiles.DEFAULT_EDA_VERSION`) so existing imports keep working.

To target a different EDA release, select a profile explicitly with
:func:`automation.eda_models.profiles.get_registry` and thread the resulting
``Registry`` through the generator/executor (see ``--eda-version``).
"""

from __future__ import annotations

from automation.eda_models.profiles import (  # noqa: F401
    CRType,
    Registry,
    SrlSupport,
    SrlTrain,
    DEFAULT_EDA_VERSION,
    get_default_registry,
    get_registry,
    list_eda_versions,
)

# Default profile — the source of the legacy module-level constants.
_DEFAULT = get_default_registry()

# ---------------------------------------------------------------------------
# Target EDA release and supported SR Linux versions (default profile)
# ---------------------------------------------------------------------------

EDA_VERSION = _DEFAULT.eda_version

# Known-good, explicitly tested exact versions. The deploy-time gate now uses
# a floor + per-train range (see ``check_srl_version``); this list is retained
# for acceptance tests and backward compatibility.
SUPPORTED_SRL_VERSIONS: list[str] = list(_DEFAULT.tested_srl_versions)


def check_srl_version(version: str, registry: Registry | None = None) -> str | None:
    """Return an error message if *version* is unsupported, else ``None``.

    Uses the floor + per-train support window of *registry* (default profile
    when omitted) rather than an exact-match allow-list.
    """
    reg = registry or _DEFAULT
    return reg.check_srl_version(version)


def check_srl_floor(version: str, registry: Registry | None = None) -> str | None:
    """Return an error if *version* is below the SR Linux floor, else ``None``.

    Floor-only gate used on the Ansible path (the version-dispatched builders
    resolve any 24.10+ release; sub-floor versions would silently fall back to
    the 24.10 base builder and emit possibly-wrong payloads).
    """
    reg = registry or _DEFAULT
    return reg.check_srl_floor(version)


# ---------------------------------------------------------------------------
# CR type descriptors (default profile)
# ---------------------------------------------------------------------------

INIT = _DEFAULT.INIT
NODE_USER = _DEFAULT.NODE_USER
NODE_PROFILE = _DEFAULT.NODE_PROFILE
TOPO_NODE = _DEFAULT.TOPO_NODE
TOPO_LINK = _DEFAULT.TOPO_LINK
INDEX_ALLOCATION_POOL = _DEFAULT.INDEX_ALLOCATION_POOL
IP_ALLOCATION_POOL = _DEFAULT.IP_ALLOCATION_POOL
INTERFACE = _DEFAULT.INTERFACE
FABRIC = _DEFAULT.FABRIC
BRIDGE_DOMAIN = _DEFAULT.BRIDGE_DOMAIN
ROUTER = _DEFAULT.ROUTER
IRB_INTERFACE = _DEFAULT.IRB_INTERFACE
VLAN = _DEFAULT.VLAN
ROUTED_INTERFACE = _DEFAULT.ROUTED_INTERFACE
STATIC_ROUTE = _DEFAULT.STATIC_ROUTE
CONFIGLET = _DEFAULT.CONFIGLET
DEFAULT_MTU = _DEFAULT.DEFAULT_MTU
BANNER = _DEFAULT.BANNER
POLICY = _DEFAULT.POLICY
PREFIX_SET = _DEFAULT.PREFIX_SET

ALL_TYPES: list[CRType] = _DEFAULT.ALL_TYPES
BY_KIND: dict[str, CRType] = _DEFAULT.BY_KIND
