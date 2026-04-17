"""Centralized EDA API version, kind, and plural mappings.

Single source of truth for all EDA CR type metadata. When EDA promotes
an API (e.g. v1alpha1 -> v1), update only this file.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Target EDA release and supported SR Linux versions
# ---------------------------------------------------------------------------

EDA_VERSION = "25.12.4"

SUPPORTED_SRL_VERSIONS: list[str] = [
    "24.10.1",
    "24.10.2",
    "24.10.3",
    "24.10.4",
    "25.3.1",
    "25.3.2",
    "25.3.3",
    "25.7.1",
    "25.7.2",
    "25.10.1",
    "25.10.2",
]


def check_srl_version(version: str) -> str | None:
    """Return an error message if *version* is unsupported, else None."""
    if version in SUPPORTED_SRL_VERSIONS:
        return None
    return (
        f"SR Linux version {version!r} is not supported by EDA {EDA_VERSION}. "
        f"Supported versions: {', '.join(SUPPORTED_SRL_VERSIONS)}"
    )


# ---------------------------------------------------------------------------
# CR type descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CRType:
    """Immutable descriptor for an EDA Custom Resource type."""

    api_version: str
    kind: str
    plural: str


# --- Bootstrap ---
INIT = CRType("bootstrap.eda.nokia.com/v1alpha1", "Init", "inits")

# --- Core ---
NODE_USER = CRType("core.eda.nokia.com/v1", "NodeUser", "nodeusers")
NODE_PROFILE = CRType("core.eda.nokia.com/v1", "NodeProfile", "nodeprofiles")
TOPO_NODE = CRType("core.eda.nokia.com/v1", "TopoNode", "toponodes")
TOPO_LINK = CRType("core.eda.nokia.com/v1", "TopoLink", "topolinks")
INDEX_ALLOCATION_POOL = CRType("core.eda.nokia.com/v1", "IndexAllocationPool", "indexallocationpools")
IP_ALLOCATION_POOL = CRType("core.eda.nokia.com/v1", "IPAllocationPool", "ipallocationpools")

# --- Interfaces ---
INTERFACE = CRType("interfaces.eda.nokia.com/v1alpha1", "Interface", "interfaces")

# --- Fabrics ---
FABRIC = CRType("fabrics.eda.nokia.com/v1alpha1", "Fabric", "fabrics")

# --- Services ---
BRIDGE_DOMAIN = CRType("services.eda.nokia.com/v1", "BridgeDomain", "bridgedomains")
ROUTER = CRType("services.eda.nokia.com/v1", "Router", "routers")
IRB_INTERFACE = CRType("services.eda.nokia.com/v1", "IRBInterface", "irbinterfaces")
VLAN = CRType("services.eda.nokia.com/v1", "VLAN", "vlans")
ROUTED_INTERFACE = CRType("services.eda.nokia.com/v1", "RoutedInterface", "routedinterfaces")

# --- Protocols ---
STATIC_ROUTE = CRType("protocols.eda.nokia.com/v1", "StaticRoute", "staticroutes")

# --- Config ---
CONFIGLET = CRType("config.eda.nokia.com/v1alpha1", "Configlet", "configlets")

# --- Siteinfo ---
DEFAULT_MTU = CRType("siteinfo.eda.nokia.com/v1alpha1", "DefaultMTU", "defaultmtus")
BANNER = CRType("siteinfo.eda.nokia.com/v1alpha1", "Banner", "banners")

ALL_TYPES: list[CRType] = [
    INIT, NODE_USER, NODE_PROFILE, TOPO_NODE, TOPO_LINK,
    INDEX_ALLOCATION_POOL, IP_ALLOCATION_POOL,
    INTERFACE, FABRIC,
    BRIDGE_DOMAIN, ROUTER, IRB_INTERFACE, VLAN, ROUTED_INTERFACE,
    STATIC_ROUTE, CONFIGLET, DEFAULT_MTU, BANNER,
]

BY_KIND: dict[str, CRType] = {t.kind: t for t in ALL_TYPES}
