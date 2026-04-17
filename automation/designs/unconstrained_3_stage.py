"""
Unconstrained 3-stage design builder.

This is the "fully-explicit" design — every node, link, edge interface,
configlet, and optional siteinfo resource is specified directly in the
topology and services YAML files. Unlike the constrained 3-stage-evpn-vxlan
design, there is NO auto-generation of nodes, links, or ASN assignment.
The builder is essentially a passthrough that maps input directly into the
FabricIntent model.
"""

from __future__ import annotations

import logging
from typing import Any

from automation.core.models import (
    BridgeDomainIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    Credentials,
    EdaSettings,
    FabricIntent,
    IrbIpAddress,
    IrbInterfaceIntent,
    LinkIntent,
    NodeIntent,
    RouterIntent,
)
from automation.designs._common_builders import (
    build_banners as _build_banners,
    build_default_mtus as _build_default_mtus,
    build_edge_interfaces as _build_edge_interfaces,
    build_lags as _build_lags,
    build_routed_interfaces as _build_routed_interfaces,
    build_routers as _build_routers,
    build_static_routes as _build_static_routes,
    build_vlans as _build_vlans,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build(topology: dict, services: dict) -> FabricIntent:
    """
    Build a FabricIntent from unconstrained 3-stage input.

    All nodes, links, and resources are specified explicitly — the builder
    performs a direct mapping from input to intent without any auto-generation.

    Args:
        topology: Parsed topology.yaml
        services: Parsed services.yaml

    Returns:
        Complete FabricIntent
    """
    fabric_name = topology["fabric_name"]
    environment = topology.get("environment", "containerlab")
    underlay = topology["underlay"]

    spine_asn = underlay["spine_asn"]
    leaf_asn_start = underlay["leaf_asn_start"]
    system0_prefix = underlay["system0_prefix"]

    # -----------------------------------------------------------------------
    # Nodes (explicit — each node is pre-defined with ASN, IPs, etc.)
    # -----------------------------------------------------------------------
    nodes = _build_nodes(topology["nodes"])

    # -----------------------------------------------------------------------
    # Links (explicit — each ISL endpoint is pre-defined)
    # -----------------------------------------------------------------------
    links = _build_links(topology["links"])

    # -----------------------------------------------------------------------
    # Edge interfaces
    # -----------------------------------------------------------------------
    edge_interfaces = _build_edge_interfaces(topology.get("edge_interfaces", []))

    # -----------------------------------------------------------------------
    # LAG interfaces
    # -----------------------------------------------------------------------
    lags = _build_lags(topology.get("lags", []))

    # -----------------------------------------------------------------------
    # Configlets (explicit — user-defined SR Linux config patches)
    # -----------------------------------------------------------------------
    configlets = _build_configlets(topology.get("configlets", []))

    # -----------------------------------------------------------------------
    # Default MTUs (optional)
    # -----------------------------------------------------------------------
    default_mtus = _build_default_mtus(topology.get("default_mtu", []))

    # Derive the default IP MTU from the first DefaultMTU entry (if present)
    default_ip_mtu = 1500
    for mtu in default_mtus:
        if mtu.layer3_mtu is not None:
            default_ip_mtu = mtu.layer3_mtu
            break

    # -----------------------------------------------------------------------
    # Banners (optional)
    # -----------------------------------------------------------------------
    banners = _build_banners(topology.get("banners", []))

    # -----------------------------------------------------------------------
    # Services
    # -----------------------------------------------------------------------
    bridge_domains = _build_bridge_domains(services.get("bridge_domains", []))
    routers = _build_routers(services.get("routers", []))
    irb_interfaces = _build_irb_interfaces(
        services.get("irb_interfaces", []), default_ip_mtu
    )
    vlans = _build_vlans(services.get("vlans", []))
    routed_interfaces = _build_routed_interfaces(
        services.get("routed_interfaces", [])
    )
    static_routes = _build_static_routes(services.get("static_routes", []))

    # -----------------------------------------------------------------------
    # EDA settings
    # -----------------------------------------------------------------------
    eda_cfg = topology.get("eda", {})
    creds_cfg = topology.get("credentials", {})
    credentials = Credentials(**creds_cfg) if creds_cfg else Credentials()

    eda_settings = EdaSettings(
        node_profile=eda_cfg.get("node_profile", ""),
        namespace=eda_cfg.get("namespace", "eda"),
    )

    return FabricIntent(
        design="unconstrained-3-stage",
        fabric_name=fabric_name,
        environment=environment,
        spine_asn=spine_asn,
        leaf_asn_start=leaf_asn_start,
        system0_prefix=system0_prefix,
        mgmt_subnet=topology.get("mgmt_subnet", ""),
        nodes=nodes,
        links=links,
        breakouts=[],  # No auto-generated breakouts in unconstrained design
        edge_interfaces=edge_interfaces,
        lags=lags,
        bridge_domains=bridge_domains,
        routers=routers,
        irb_interfaces=irb_interfaces,
        vlans=vlans,
        routed_interfaces=routed_interfaces,
        static_routes=static_routes,
        configlets=configlets,
        default_mtus=default_mtus,
        banners=banners,
        credentials=credentials,
        eda=eda_settings,
    )


# ---------------------------------------------------------------------------
# Node / link builders (passthrough — no auto-generation)
# ---------------------------------------------------------------------------


def _build_nodes(raw: list[dict]) -> list[NodeIntent]:
    """Build node intents from explicit node definitions."""
    return [
        NodeIntent(
            name=n["name"],
            role=n["role"],
            platform=n["platform"],
            version=n["version"],
            asn=n["asn"],
            system0_ipv4=n["system0_ipv4"],
            mgmt_ipv4=n["mgmt_ipv4"],
            labels=n.get("labels", {}),
            uplink_interfaces=n.get("uplink_interfaces", []),
        )
        for n in raw
    ]


def _build_links(raw: list[dict]) -> list[LinkIntent]:
    """Build link intents from explicit link definitions."""
    return [
        LinkIntent(
            name=link["name"],
            local_node=link["local_node"],
            local_interface=link["local_interface"],
            remote_node=link["remote_node"],
            remote_interface=link["remote_interface"],
        )
        for link in raw
    ]


# ---------------------------------------------------------------------------
# Configlets (explicit — user-defined in topology.yaml)
# ---------------------------------------------------------------------------


def _build_configlets(raw: list[dict]) -> list[ConfigletIntent]:
    """Build configlet intents from explicit configlet definitions."""
    return [
        ConfigletIntent(
            name=c["name"],
            endpoint_selector=c.get("endpoint_selector", []),
            endpoints=c.get("endpoints", []),
            priority=c.get("priority", 100),
            configs=[
                ConfigletConfigEntry(
                    path=cfg["path"],
                    operation=cfg.get("operation", "Update"),
                    config=cfg["config"],
                )
                for cfg in c.get("configs", [])
            ],
        )
        for c in raw
    ]


# ---------------------------------------------------------------------------
# Service builders
# ---------------------------------------------------------------------------


def _build_bridge_domains(raw: list[dict]) -> list[BridgeDomainIntent]:
    """Build bridge domain intents from raw input."""
    return [
        BridgeDomainIntent(
            name=bd["name"],
            vni=bd["vni"],
            evi=bd["evi"],
            mac_learning=bd.get("mac_learning", True),
            mac_aging=bd.get("mac_aging", 300),
            mac_duplication=bd.get("mac_duplication"),
        )
        for bd in raw
    ]


def _coerce_evpn_adv_type(val: Any) -> dict | None:
    """Coerce evpn_route_advertisement_type from string or dict input."""
    if val is None or val == "":
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        return {"type": val}
    return None


def _build_irb_interfaces(
    raw: list[dict], default_ip_mtu: int = 1500
) -> list[IrbInterfaceIntent]:
    """Build IRB interface intents with full dual-stack support."""
    result: list[IrbInterfaceIntent] = []
    for irb in raw:
        # Build ip_addresses from the explicit array
        ip_addresses = [
            IrbIpAddress(ipv4=addr.get("ipv4"), ipv6=addr.get("ipv6"))
            for addr in irb.get("ip_addresses", [])
        ]

        result.append(
            IrbInterfaceIntent(
                name=irb["name"],
                description=irb.get("description", ""),
                bridge_domain=irb["bridge_domain"],
                router=irb["router"],
                ip_addresses=ip_addresses,
                ip_mtu=irb.get("ip_mtu", default_ip_mtu),
                arp_timeout=irb.get("arp_timeout", 280),
                learn_unsolicited=irb.get("learn_unsolicited", "NONE"),
                proxy_arp=irb.get("proxy_arp", True),
                proxy_nd=irb.get("proxy_nd", False),
                evpn_route_advertisement_type=_coerce_evpn_adv_type(
                    irb.get("evpn_route_advertisement_type")
                ),
                host_route_populate=irb.get("host_route_populate"),
            )
        )
    return result


