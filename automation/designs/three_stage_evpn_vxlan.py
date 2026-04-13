"""
3-Stage EVPN VXLAN design builder.

Transforms simple input (topology.yaml + services.yaml) into a FabricIntent
by applying the locked Nokia Validated Design rules:

- eBGP underlay with IPv6 unnumbered
- Full-mesh leaf↔spine ISLs
- ISL port allocation from highest interface index downward
- BFD, routing policy, BGP afi-safi settings are locked by design
- Overlay services (mac-vrfs, ip-vrfs) passed through from input
"""

from __future__ import annotations

import ipaddress
import logging

from automation.core.models import (
    BannerIntent,
    BridgeDomainIntent,
    BreakoutIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    EdaSettings,
    FabricIntent,
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
from automation.core.platforms import (
    expand_breakout,
    get_platform,
    interface_name,
)

logger = logging.getLogger(__name__)


def build(topology: dict, services: dict) -> FabricIntent:
    """
    Build a FabricIntent from simple 3-stage input.

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

    spine_cfg = topology["spines"]
    leaf_cfg = topology["leafs"]

    # Validate platforms
    spine_platform = get_platform(spine_cfg["platform"])
    leaf_platform = get_platform(leaf_cfg["platform"])

    if not spine_platform.validate_role("spine"):
        raise ValueError(
            f"Platform '{spine_platform.name}' is not allowed as spine in 3-stage design"
        )
    if not leaf_platform.validate_role("leaf"):
        raise ValueError(
            f"Platform '{leaf_platform.name}' is not allowed as leaf in 3-stage design"
        )

    # -----------------------------------------------------------------------
    # Generate nodes
    # -----------------------------------------------------------------------
    nodes = _build_nodes(
        spine_cfg=spine_cfg,
        leaf_cfg=leaf_cfg,
        spine_asn=spine_asn,
        leaf_asn_start=leaf_asn_start,
        system0_prefix=system0_prefix,
    )

    # -----------------------------------------------------------------------
    # Generate ISL links + compute uplink interfaces
    # -----------------------------------------------------------------------
    links, breakouts = _build_isl_links(
        nodes=nodes,
        spine_cfg=spine_cfg,
        leaf_cfg=leaf_cfg,
    )

    # -----------------------------------------------------------------------
    # Edge interfaces (pass through from input)
    # -----------------------------------------------------------------------
    edge_interfaces = _build_edge_interfaces(topology.get("edge_interfaces", []))

    # -----------------------------------------------------------------------
    # LAG interfaces (pass through from input)
    # -----------------------------------------------------------------------
    lags = _build_lags(topology.get("lags", []))

    # -----------------------------------------------------------------------
    # Default MTUs (built before services so ip_mtu can be derived)
    # -----------------------------------------------------------------------
    default_mtus = _build_default_mtus(topology.get("default_mtu", []))

    # Derive the default IP MTU from the first DefaultMTU entry (if present)
    default_ip_mtu = 1500
    for mtu in default_mtus:
        if mtu.layer3_mtu is not None:
            default_ip_mtu = mtu.layer3_mtu
            break

    # -----------------------------------------------------------------------
    # Services (pass through from input)
    # -----------------------------------------------------------------------
    bridge_domains = _build_bridge_domains(services.get("bridge_domains", []))
    routers = _build_routers(services.get("routers", []))
    irb_interfaces = _build_irb_interfaces(services.get("irb_interfaces", []), default_ip_mtu)
    vlans = _build_vlans(services.get("vlans", []))
    routed_interfaces = _build_routed_interfaces(services.get("routed_interfaces", []))
    static_routes = _build_static_routes(services.get("static_routes", []))

    # -----------------------------------------------------------------------
    # Configlets (design-specific device configuration)
    # -----------------------------------------------------------------------
    configlets = _build_configlets(lags)

    # -----------------------------------------------------------------------
    # Banners (optional)
    # -----------------------------------------------------------------------
    banners = _build_banners(topology.get("banners", []))

    # -----------------------------------------------------------------------
    # EDA settings
    # -----------------------------------------------------------------------
    eda_cfg = topology.get("eda", {})
    eda_settings = EdaSettings(
        node_profile=eda_cfg.get("node_profile", ""),
        namespace=eda_cfg.get("namespace", "eda"),
    )

    return FabricIntent(
        design="3-stage-evpn-vxlan",
        fabric_name=fabric_name,
        environment=environment,
        spine_asn=spine_asn,
        leaf_asn_start=leaf_asn_start,
        system0_prefix=system0_prefix,
        nodes=nodes,
        links=links,
        breakouts=breakouts,
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
# Node generation
# ---------------------------------------------------------------------------


def _build_nodes(
    *,
    spine_cfg: dict,
    leaf_cfg: dict,
    spine_asn: int,
    leaf_asn_start: int,
    system0_prefix: str,
) -> list[NodeIntent]:
    """Generate leaf and spine NodeIntent objects."""
    network = ipaddress.IPv4Network(system0_prefix)
    nodes: list[NodeIntent] = []

    # Leafs: IPs from .11 upward (offset 11)
    leaf_count = leaf_cfg["count"]
    leaf_labels = leaf_cfg.get("labels", {})
    leaf_mgmt_base = leaf_cfg.get("mgmt_base_ipv4", "")

    for i in range(1, leaf_count + 1):
        ip_offset = 10 + i  # leaf1=.11, leaf2=.12, ...
        sys0_ip = str(network.network_address + ip_offset)
        mgmt_ip = _increment_ip(leaf_mgmt_base, i - 1) if leaf_mgmt_base else ""

        nodes.append(
            NodeIntent(
                name=f"leaf{i}",
                role="leaf",
                platform=leaf_cfg["platform"],
                version=leaf_cfg.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=leaf_asn_start + i - 1,
                mgmt_ipv4=mgmt_ip,
                labels={
                    **leaf_labels,
                    "eda.nokia.com/name": f"leaf{i}",
                    "eda.nokia.com/security-profile": "managed",
                },
            )
        )

    # Spines: IPs from .101 upward (offset 101)
    spine_count = spine_cfg["count"]
    spine_labels = spine_cfg.get("labels", {})
    spine_mgmt_base = spine_cfg.get("mgmt_base_ipv4", "")

    for i in range(1, spine_count + 1):
        ip_offset = 100 + i  # spine1=.101, spine2=.102, ...
        sys0_ip = str(network.network_address + ip_offset)
        mgmt_ip = _increment_ip(spine_mgmt_base, i - 1) if spine_mgmt_base else ""

        nodes.append(
            NodeIntent(
                name=f"spine{i}",
                role="spine",
                platform=spine_cfg["platform"],
                version=spine_cfg.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=spine_asn,
                mgmt_ipv4=mgmt_ip,
                labels={
                    **spine_labels,
                    "eda.nokia.com/name": f"spine{i}",
                    "eda.nokia.com/security-profile": "managed",
                },
            )
        )

    return nodes


# ---------------------------------------------------------------------------
# ISL link generation
# ---------------------------------------------------------------------------


def _build_isl_links(
    *,
    nodes: list[NodeIntent],
    spine_cfg: dict,
    leaf_cfg: dict,
) -> tuple[list[LinkIntent], list[BreakoutIntent]]:
    """
    Generate full-mesh leaf↔spine ISL links.

    Port allocation:
    - Spine side: highest port index, counting down
    - Leaf side: highest port index, counting down (one per spine)

    If breakouts are configured on spines, expands into channels.
    """
    spine_platform = get_platform(spine_cfg["platform"])
    leaf_platform = get_platform(leaf_cfg["platform"])

    leafs = [n for n in nodes if n.role == "leaf"]
    spines = [n for n in nodes if n.role == "spine"]
    spine_count = len(spines)

    # Parse spine breakout configuration
    spine_breakouts_cfg = spine_cfg.get("breakouts", [])
    breakout_intents: list[BreakoutIntent] = []

    links: list[LinkIntent] = []

    for spine_idx, spine in enumerate(spines):
        # Determine available ports on this spine (lowest-up for downlinks)
        all_ports = sorted(spine_platform.all_port_indices())

        # Apply breakouts: expand specified connectors into channels
        spine_bo_map: dict[int, list[str]] = {}  # port_index → channel names
        for bo in spine_breakouts_cfg:
            bo_intf = bo["interface"]  # e.g. "ethernet-1-32"
            bo_port_idx = _parse_port_index(bo_intf)
            channels = bo["channels"]
            speed = bo["speed"]
            channel_names = expand_breakout(bo_intf, channels)
            spine_bo_map[bo_port_idx] = channel_names
            breakout_intents.append(
                BreakoutIntent(
                    node=spine.name,
                    interface=bo_intf,
                    channels=channels,
                    speed=speed,
                )
            )

        # Build the available spine interface list (lowest-up)
        # For breakout ports, replace the connector with its channels
        spine_interfaces: list[str] = []
        for port_idx in all_ports:
            if port_idx in spine_bo_map:
                # Channels are already ordered 1..N, add them
                spine_interfaces.extend(spine_bo_map[port_idx])
            else:
                spine_interfaces.append(interface_name(port_idx))

        # Allocate spine interfaces to leaf connections (ascending from port 1)
        spine_intf_cursor = 0
        for leaf_idx, leaf in enumerate(leafs):
            if spine_intf_cursor >= len(spine_interfaces):
                raise ValueError(
                    f"Spine {spine.name} ran out of ports for leaf {leaf.name}. "
                    f"Platform {spine_platform.name} has {spine_platform.total_ports} ports "
                    f"but {len(leafs)} leafs require connections."
                )

            spine_intf = spine_interfaces[spine_intf_cursor]
            spine_intf_cursor += 1

            # Leaf side: highest ports, counting down (one per spine)
            # leaf uplink to spine{spine_idx+1} uses the (spine_idx+1)th highest port
            leaf_all_ports = sorted(leaf_platform.all_port_indices(), reverse=True)
            leaf_intf_idx = spine_idx  # spine1→highest, spine2→next, ...
            if leaf_intf_idx >= len(leaf_all_ports):
                raise ValueError(
                    f"Leaf {leaf.name} doesn't have enough ports for {spine_count} spine uplinks"
                )
            leaf_port = leaf_all_ports[leaf_intf_idx]
            leaf_intf = interface_name(leaf_port)

            # Record uplink on the leaf node
            if leaf_intf not in leaf.uplink_interfaces:
                leaf.uplink_interfaces.append(leaf_intf)

            # Record uplink on the spine node
            if spine_intf not in spine.uplink_interfaces:
                spine.uplink_interfaces.append(spine_intf)

            link_name = f"{leaf.name}-{spine.name}"
            links.append(
                LinkIntent(
                    name=link_name,
                    local_node=leaf.name,
                    local_interface=leaf_intf,
                    remote_node=spine.name,
                    remote_interface=spine_intf,
                )
            )

    return links, breakout_intents


# ---------------------------------------------------------------------------
# Edge interfaces
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


# ---------------------------------------------------------------------------
# LAGs
# ---------------------------------------------------------------------------


def _build_lags(raw: list[dict]) -> list[LagIntent]:
    """Build LAG intents from raw input."""
    lags = []
    for lag in raw:
        lacp_cfg = lag.get("lacp", {})
        members = [
            LagMember(
                node=m["node"],
                interface=m["interface"],
                aggregate_id=m["aggregate_id"],
                lacp_port_priority=m.get("lacp_port_priority", 32768),
            )
            for m in lag.get("members", [])
        ]
        lags.append(
            LagIntent(
                name=lag["name"],
                type=lag.get("type", "lacp"),
                multihoming_mode=lag.get("mode", "all-active"),
                min_links=lag.get("min_links", 1),
                lacp=LacpConfig(
                    interval=lacp_cfg.get("interval", "fast"),
                    system_id_mac=lacp_cfg.get("system_id_mac", ""),
                    system_priority=lacp_cfg.get("system_priority", 32768),
                    admin_key=lacp_cfg.get("admin_key"),
                    fallback=lacp_cfg.get("fallback"),
                ),
                members=members,
                labels=lag.get("labels", {}),
                revertive=lag.get("revertive", False),
                preferred_active_node=lag.get("preferred_active_node", ""),
                standby_signaling=lag.get("standby_signaling", ""),
                reload_delay_timer=lag.get("reload_delay_timer", 100),
            )
        )
    return lags


# ---------------------------------------------------------------------------
# Services passthrough
# ---------------------------------------------------------------------------


def _build_bridge_domains(raw: list[dict]) -> list[BridgeDomainIntent]:
    return [
        BridgeDomainIntent(
            name=bd["name"],
            vni=bd["vni"],
            evi=bd["evi"],
            mac_learning=bd.get("mac_learning", True),
            mac_aging=bd.get("mac_aging", 300),
            mac_duplication=bd.get(
                "mac_duplication",
                {
                    "enabled": True,
                    "hold_down_time": 9,
                    "monitoring_window": 3,
                    "action": "StopLearning",
                    "num_moves": 5,
                },
            ),
        )
        for bd in raw
    ]


def _build_routers(raw: list[dict]) -> list[RouterIntent]:
    return [
        RouterIntent(
            name=r["name"],
            vni=r["vni"],
            evi=r["evi"],
            node_selector=r.get("node_selector", []),
        )
        for r in raw
    ]


def _build_irb_interfaces(raw: list[dict], default_ip_mtu: int = 1500) -> list[IrbInterfaceIntent]:
    """Build IRB interface intents, using default_ip_mtu when ip_mtu is not explicitly set."""
    return [
        IrbInterfaceIntent(
            name=irb["name"],
            bridge_domain=irb["bridge_domain"],
            router=irb["router"],
            ipv4=irb["ipv4"],
            proxy_arp=irb.get("proxy_arp", True),
            proxy_nd=irb.get("proxy_nd", False),
            arp_timeout=irb.get("arp_timeout", 280),
            ip_mtu=irb.get("ip_mtu", default_ip_mtu),
            evpn_route_advertisement_type=irb.get(
                "evpn_route_advertisement_type",
                {
                    "rfc9135SymmetricMode": False,
                    "arpDynamic": True,
                    "arpStatic": True,
                    "ndDynamic": False,
                    "ndStatic": False,
                },
            ),
            host_route_populate=irb.get(
                "host_route_populate",
                {"dynamic": True, "static": True, "evpn": False},
            ),
        )
        for irb in raw
    ]


def _build_vlans(raw: list[dict]) -> list[VlanIntent]:
    return [
        VlanIntent(
            name=v["name"],
            bridge_domain=v["bridge_domain"],
            vlan_id=str(v["vlan_id"]),
            interface_selector=v.get("interface_selector", []),
        )
        for v in raw
    ]


def _build_routed_interfaces(raw: list[dict]) -> list[RoutedInterfaceIntent]:
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


# ---------------------------------------------------------------------------
# Configlets (design-specific device configuration)
# ---------------------------------------------------------------------------


def _build_configlets(lags: list[LagIntent]) -> list[ConfigletIntent]:
    """
    Build design-specific configlets for the 3-stage EVPN-VXLAN design.

    - BGP EVPN rapid update + rapid withdrawal (all nodes)
    - Node isolation event-handler (leafs with LAG members)
    - ESI DF election activation timer (per LAG)
    """
    import json

    configlets: list[ConfigletIntent] = []

    # BGP rapid update (all nodes)
    configlets.append(
        ConfigletIntent(
            name="bgp-evpn-rapid",
            endpoint_selector=["eda.nokia.com/role=leaf", "eda.nokia.com/role=spine"],
            operating_system="srl",
            priority=100,
            configs=[
                ConfigletConfigEntry(
                    path='.network-instance{.name=="default"}.protocols.bgp.afi-safi{.afi-safi-name=="evpn"}.evpn',
                    operation="Update",
                    config='{\n  "rapid-update": "true"\n}',
                )
            ],
        )
    )

    # BGP rapid withdrawal (all nodes)
    configlets.append(
        ConfigletIntent(
            name="bgp-rapid-route-withdraw",
            endpoint_selector=["eda.nokia.com/role=leaf", "eda.nokia.com/role=spine"],
            operating_system="srl",
            priority=100,
            configs=[
                ConfigletConfigEntry(
                    path='.network-instance{.name=="default"}.protocols.bgp.route-advertisement',
                    operation="Update",
                    config='{\n  "rapid-withdrawal": "true"\n}',
                )
            ],
        )
    )

    # Node isolation configlets — for each leaf that has LAG members
    leaf_lag_interfaces: dict[str, list[str]] = {}
    for lag in lags:
        for member in lag.members:
            leaf_lag_interfaces.setdefault(member.node, []).append(member.interface)

    for node_name, down_links in leaf_lag_interfaces.items():
        config_json = json.dumps(
            {
                "event-handler": {
                    "instance": [
                        {
                            "name": "overlay-bgp",
                            "admin-state": "enable",
                            "upython-script": "node-isolation.py",
                            "paths": [
                                "network-instance default protocols bgp neighbor * session-state"
                            ],
                            "options": {
                                "object": [
                                    {"name": "down-links", "values": sorted(set(down_links))},
                                    {"name": "hold-down-time", "value": "20000"},
                                    {"name": "required-bgp-sessions-established", "value": "1"},
                                ]
                            },
                        }
                    ]
                }
            },
            indent=4,
        )
        configlets.append(
            ConfigletIntent(
                name=f"node-isolation-{node_name}-lag",
                endpoints=[node_name],
                operating_system="srl",
                priority=50,
                configs=[
                    ConfigletConfigEntry(path=".system", operation="Create", config=config_json)
                ],
            )
        )

    # ESI DF election activation timer configlets — one per LAG
    for lag in lags:
        endpoints = sorted(set(m.node for m in lag.members))
        configlets.append(
            ConfigletIntent(
                name=lag.name,
                endpoints=endpoints,
                operating_system="srl",
                priority=100,
                configs=[
                    ConfigletConfigEntry(
                        path=f'.system.network-instance.protocols.evpn.ethernet-segments.bgp-instance{{.id==1}}.ethernet-segment{{.name=="{lag.name}"}}.df-election.timers',
                        operation="Update",
                        config='{\n  "activation-timer": 0\n}',
                    )
                ],
            )
        )

    return configlets


# ---------------------------------------------------------------------------
# Default MTU
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


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------


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
# Utilities
# ---------------------------------------------------------------------------


def _increment_ip(base_ip: str, offset: int) -> str:
    """Increment an IPv4 address by an offset."""
    addr = ipaddress.IPv4Address(base_ip)
    return str(addr + offset)


def _parse_port_index(interface_name: str) -> int:
    """
    Parse port index from interface name.

    'ethernet-1-32' → 32
    'ethernet-1-5'  → 5
    """
    parts = interface_name.split("-")
    return int(parts[-1])
