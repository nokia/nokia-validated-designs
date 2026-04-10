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

from automation.core.models import (
    BannerIntent,
    BridgeDomainIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    EdaSettings,
    FabricIntent,
    IrbIpAddress,
    IrbInterfaceIntent,
    LacpConfig,
    LagIntent,
    LagMember,
    LinkIntent,
    NodeIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    StaticRouteIntent,
    VlanIntent,
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
# Edge / access builders (shared logic with 3-stage design)
# ---------------------------------------------------------------------------


def _build_edge_interfaces(raw: list[dict]) -> list[EdgeInterfaceIntent]:
    """Build edge interface intents from raw input."""
    return [
        EdgeInterfaceIntent(
            name=ei["name"],
            node=ei["node"],
            interface=ei["interface"],
            encap=ei.get("encap", "dot1q"),
            labels=ei.get("labels", {}),
        )
        for ei in raw
    ]


def _build_lags(raw: list[dict]) -> list[LagIntent]:
    """Build LAG intents from raw input."""
    result: list[LagIntent] = []
    for lag in raw:
        lacp_cfg = lag.get("lacp", {})
        fallback = lacp_cfg.get("fallback")

        members = [
            LagMember(
                node=m["node"],
                interface=m["interface"],
                aggregate_id=m["aggregate_id"],
            )
            for m in lag.get("members", [])
        ]

        result.append(
            LagIntent(
                name=lag["name"],
                type=lag.get("type", "lacp"),
                multihoming_mode=lag.get("mode", "all-active"),
                min_links=lag.get("min_links", 1),
                lacp=LacpConfig(
                    interval=lacp_cfg.get("interval", "fast"),
                    system_id_mac=lacp_cfg.get("system_id_mac", ""),
                    system_priority=lacp_cfg.get("system_priority", 32768),
                    fallback=fallback,
                ),
                members=members,
                labels=lag.get("labels", {}),
                revertive=lag.get("revertive", False),
                preferred_active_node=lag.get("preferred_active_node", ""),
                standby_signaling=lag.get("standby_signaling", ""),
                reload_delay_timer=lag.get("reload_delay_timer", 100),
            )
        )
    return result


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
# Siteinfo resources (DefaultMTU, Banner)
# ---------------------------------------------------------------------------


def _build_default_mtus(raw: list[dict]) -> list[DefaultMtuIntent]:
    """Build default MTU intents from raw input."""
    return [
        DefaultMtuIntent(
            name=mtu["name"],
            interface_mtu=mtu.get("interface_mtu"),
            layer2_subif_mtu=mtu.get("layer2_subif_mtu"),
            layer3_mtu=mtu.get("layer3_mtu"),
            node_selector=mtu.get("node_selector", []),
            nodes=mtu.get("nodes", []),
        )
        for mtu in raw
    ]


def _build_banners(raw: list[dict]) -> list[BannerIntent]:
    """Build banner intents from raw input."""
    return [
        BannerIntent(
            name=b["name"],
            login_banner=b.get("login_banner", ""),
            motd=b.get("motd", ""),
            node_selector=b.get("node_selector", []),
            nodes=b.get("nodes", []),
        )
        for b in raw
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


def _build_routers(raw: list[dict]) -> list[RouterIntent]:
    """Build router intents from raw input."""
    return [
        RouterIntent(
            name=r["name"],
            vni=r["vni"],
            evi=r["evi"],
            node_selector=r.get("node_selector", []),
        )
        for r in raw
    ]


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
                evpn_route_advertisement_type=irb.get(
                    "evpn_route_advertisement_type", ""
                ),
                host_route_populate=irb.get("host_route_populate"),
            )
        )
    return result


def _build_vlans(raw: list[dict]) -> list[VlanIntent]:
    """Build VLAN intents from raw input."""
    return [
        VlanIntent(
            name=v["name"],
            bridge_domain=v["bridge_domain"],
            vlan_id=v["vlan_id"],
            interface_selector=v.get("interface_selector", []),
        )
        for v in raw
    ]


def _build_routed_interfaces(raw: list[dict]) -> list[RoutedInterfaceIntent]:
    """Build routed interface intents from raw input."""
    return [
        RoutedInterfaceIntent(
            name=ri["name"],
            interface=ri["interface"],
            router=ri["router"],
            vlan_id=ri.get("vlan_id", "null"),
            ipv4_addresses=ri.get("ipv4_addresses", []),
            ip_mtu=ri.get("ip_mtu", 1500),
            arp_timeout=ri.get("arp_timeout", 14400),
        )
        for ri in raw
    ]


def _build_static_routes(raw: list[dict]) -> list[StaticRouteIntent]:
    """Build static route intents from raw input."""
    return [
        StaticRouteIntent(
            name=sr["name"],
            router=sr["router"],
            nodes=sr.get("nodes", []),
            prefixes=sr.get("prefixes", []),
            nexthop_group=sr.get("nexthop_group", {}),
        )
        for sr in raw
    ]
