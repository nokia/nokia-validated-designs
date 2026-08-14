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
    BridgeDomainIntent,
    BreakoutIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    FabricIntent,
    IrbIpAddress,
    IrbInterfaceIntent,
    LagIntent,
    LinkIntent,
    NodeIntent,
    PrefixSetIntent,
    RoutingPolicyIntent,
)
from automation.core.extras import merge_by_name
from automation.core.platforms import (
    expand_breakout,
    get_platform,
    interface_name,
)
from automation.designs._common_builders import (
    apply_node_overrides as _apply_node_overrides,
    apply_service_extras as _apply_service_extras,
    build_banners as _build_banners,
    build_credentials as _build_credentials,
    build_default_mtus as _build_default_mtus,
    build_eda_settings as _build_eda_settings,
    build_edge_interfaces as _build_edge_interfaces,
    build_lags as _build_lags,
    build_routed_interfaces as _build_routed_interfaces,
    build_routers as _build_routers,
    build_static_routes as _build_static_routes,
    build_vlans as _build_vlans,
    default_routing_policies as _default_routing_policies,
    derive_default_ip_mtu as _derive_default_ip_mtu,
    increment_ip as _increment_ip,
    merge_extras_configlets as _merge_extras_configlets,
    normalize_policy_update as _normalize_policy_update,
    normalize_prefix_set_update as _normalize_prefix_set_update,
    validate_mgmt_ips as _validate_mgmt_ips,
    validate_unique_names as _validate_unique_names,
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
        node_overrides=topology.get("nodes"),
    )

    # -----------------------------------------------------------------------
    # Generate ISL links + compute uplink interfaces
    # -----------------------------------------------------------------------
    links, breakouts = _build_isl_links(
        nodes=nodes,
        spine_cfg=spine_cfg,
        leaf_cfg=leaf_cfg,
        link_overrides=topology.get("links", []),
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
    default_ip_mtu = _derive_default_ip_mtu(default_mtus)

    # -----------------------------------------------------------------------
    # Services (pass through from input, tagged as design origin)
    # -----------------------------------------------------------------------
    bridge_domains = _build_bridge_domains(
        services.get("bridge_domains", []), origin="3-stage"
    )
    routers = _build_routers(services.get("routers", []))
    irb_interfaces = _build_irb_interfaces(
        services.get("irb_interfaces", []), default_ip_mtu, origin="3-stage"
    )
    vlans = _build_vlans(services.get("vlans", []))
    routed_interfaces = _build_routed_interfaces(services.get("routed_interfaces", []))
    static_routes = _build_static_routes(services.get("static_routes", []))

    # -----------------------------------------------------------------------
    # Configlets (design-specific device configuration)
    # -----------------------------------------------------------------------
    configlets = _build_configlets(lags)

    # -----------------------------------------------------------------------
    # Extras — unconstrained additions/overrides from user input
    # -----------------------------------------------------------------------
    topo_extras = topology.get("extras", {})
    svc_extras = services.get("extras", {})

    # Configlets fully replace by name (not a field overlay), so they stay a
    # dedicated special case rather than going through the generic table.
    if topo_extras.get("configlets"):
        configlets = _merge_extras_configlets(
            configlets, topo_extras["configlets"]
        )

    _merged_extras = _apply_service_extras(
        {
            "bridge_domains": bridge_domains,
            "irb_interfaces": irb_interfaces,
            "routers": routers,
            "vlans": vlans,
            "routed_interfaces": routed_interfaces,
            "static_routes": static_routes,
        },
        svc_extras,
        default_ip_mtu=default_ip_mtu,
    )
    bridge_domains = _merged_extras["bridge_domains"]
    irb_interfaces = _merged_extras["irb_interfaces"]
    routers = _merged_extras["routers"]
    vlans = _merged_extras["vlans"]
    routed_interfaces = _merged_extras["routed_interfaces"]
    static_routes = _merged_extras["static_routes"]

    # -----------------------------------------------------------------------
    # Banners (optional)
    # -----------------------------------------------------------------------
    banners = _build_banners(topology.get("banners", []))

    # -----------------------------------------------------------------------
    # Routing policies (design defaults + user overrides merged by name)
    # -----------------------------------------------------------------------
    default_ps, default_export, default_import = _default_routing_policies(
        fabric_name, system0_prefix
    )
    prefix_sets = merge_by_name(
        [default_ps],
        topology.get("prefix_sets", []),
        PrefixSetIntent,
        pre_process=_normalize_prefix_set_update,
    )
    routing_policies = merge_by_name(
        [default_export, default_import],
        services.get("routing_policies", []),
        RoutingPolicyIntent,
        pre_process=_normalize_policy_update,
    )
    fabric_export_policies = topology.get(
        "fabric_export_policies", [default_export.name]
    )
    fabric_import_policies = topology.get(
        "fabric_import_policies", [default_import.name]
    )

    # -----------------------------------------------------------------------
    # Credentials + EDA settings
    # -----------------------------------------------------------------------
    credentials = _build_credentials(topology)
    eda_settings = _build_eda_settings(topology)

    return FabricIntent(
        design="3-stage-evpn-vxlan",
        fabric_name=fabric_name,
        environment=environment,
        spine_asn=spine_asn,
        leaf_asn_start=leaf_asn_start,
        system0_prefix=system0_prefix,
        mgmt_subnet=topology.get("mgmt_subnet", ""),
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
        prefix_sets=prefix_sets,
        routing_policies=routing_policies,
        fabric_export_policies=fabric_export_policies,
        fabric_import_policies=fabric_import_policies,
        credentials=credentials,
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
    node_overrides: list[dict] | None = None,
) -> list[NodeIntent]:
    """Generate leaf and spine NodeIntent objects."""
    network = ipaddress.IPv4Network(system0_prefix)
    nodes: list[NodeIntent] = []

    leaf_template = leaf_cfg.get("name_template", "leaf{i}")
    spine_template = spine_cfg.get("name_template", "spine{i}")

    # Leafs: IPs from .11 upward (offset 11)
    leaf_count = leaf_cfg["count"]
    leaf_labels = leaf_cfg.get("labels", {})
    leaf_mgmt_base = leaf_cfg.get("mgmt_base_ipv4", "")

    for i in range(1, leaf_count + 1):
        ip_offset = 10 + i  # leaf1=.11, leaf2=.12, ...
        sys0_ip = str(network.network_address + ip_offset)
        mgmt_ip = _increment_ip(leaf_mgmt_base, i - 1) if leaf_mgmt_base else ""
        node_name = leaf_template.format(i=i)

        nodes.append(
            NodeIntent(
                name=node_name,
                role="leaf",
                platform=leaf_cfg["platform"],
                version=leaf_cfg.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=leaf_asn_start + i - 1,
                mgmt_ipv4=mgmt_ip,
                labels={
                    **leaf_labels,
                    "eda.nokia.com/name": node_name,
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
        node_name = spine_template.format(i=i)

        nodes.append(
            NodeIntent(
                name=node_name,
                role="spine",
                platform=spine_cfg["platform"],
                version=spine_cfg.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=spine_asn,
                mgmt_ipv4=mgmt_ip,
                labels={
                    **spine_labels,
                    "eda.nokia.com/name": node_name,
                    "eda.nokia.com/security-profile": "managed",
                },
            )
        )

    _validate_unique_names(nodes)

    if node_overrides:
        _apply_node_overrides(nodes, node_overrides)

    _validate_mgmt_ips(nodes)

    return nodes


# ---------------------------------------------------------------------------
# ISL link generation
# ---------------------------------------------------------------------------


def _build_isl_links(
    *,
    nodes: list[NodeIntent],
    spine_cfg: dict,
    leaf_cfg: dict,
    link_overrides: list[dict] | None = None,
) -> tuple[list[LinkIntent], list[BreakoutIntent]]:
    """
    Generate full-mesh leaf↔spine ISL links.

    Port allocation:
    - Spine side: highest port index, counting down
    - Leaf side: highest port index, counting down (one per spine)

    If breakouts are configured on spines, expands into channels.

    If ``link_overrides`` is provided, each entry replaces the auto-allocated
    interface assignment for the matching (leaf, spine) pair. Pair matching is
    orientation-agnostic; the override's local/remote orientation is preserved
    in the resulting LinkIntent. Each node's ``uplink_interfaces`` list is
    rewritten to drop the auto-assigned port and add the user-supplied one.
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

    if link_overrides:
        _apply_link_overrides(links, link_overrides, nodes)

    return links, breakout_intents


def _apply_link_overrides(
    links: list[LinkIntent],
    overrides: list[dict],
    nodes: list[NodeIntent],
) -> None:
    """Apply user-supplied per-link overrides to the auto-allocated ISL list.

    Mutates ``links`` in place and rewrites the matching nodes'
    ``uplink_interfaces`` lists.

    Each override is matched against the auto-allocated link sharing the same
    unordered (local_node, remote_node) pair. The override's own orientation
    and ``name`` are preserved on the resulting LinkIntent. After all
    overrides are applied, the final link list is checked for (node, interface)
    collisions to catch the case where two overrides — or an override and an
    untouched auto-link — would land on the same physical port.
    """
    nodes_by_name = {n.name: n for n in nodes}

    by_pair: dict[frozenset[str], int] = {}
    for idx, link in enumerate(links):
        by_pair[frozenset({link.local_node, link.remote_node})] = idx

    seen: set[frozenset[str]] = set()
    for ov in overrides:
        a, b = ov["local_node"], ov["remote_node"]
        if a not in nodes_by_name:
            raise ValueError(
                f"links override references unknown node '{a}'"
            )
        if b not in nodes_by_name:
            raise ValueError(
                f"links override references unknown node '{b}'"
            )
        if a == b:
            raise ValueError(
                f"links override has identical local_node and remote_node '{a}'"
            )

        key = frozenset({a, b})
        if key in seen:
            raise ValueError(
                f"duplicate links override for pair {a}<->{b}"
            )
        seen.add(key)

        if key not in by_pair:
            raise ValueError(
                f"links override {a}<->{b} does not match any auto-allocated "
                f"leaf<->spine link (3-stage design generates one ISL per "
                f"leaf/spine pair)"
            )

        idx = by_pair[key]
        old = links[idx]

        # Map old → new interface per node, regardless of override orientation.
        new_intf_for: dict[str, str] = {
            ov["local_node"]: ov["local_interface"],
            ov["remote_node"]: ov["remote_interface"],
        }
        old_intf_for: dict[str, str] = {
            old.local_node: old.local_interface,
            old.remote_node: old.remote_interface,
        }
        for node_name, new_intf in new_intf_for.items():
            old_intf = old_intf_for[node_name]
            uplinks = nodes_by_name[node_name].uplink_interfaces
            if old_intf in uplinks:
                uplinks.remove(old_intf)
            if new_intf not in uplinks:
                uplinks.append(new_intf)

        links[idx] = LinkIntent(
            name=ov.get("name", old.name),
            local_node=a,
            local_interface=ov["local_interface"],
            remote_node=b,
            remote_interface=ov["remote_interface"],
        )

    seen_endpoints: dict[tuple[str, str], str] = {}
    for link in links:
        for node_name, intf in (
            (link.local_node, link.local_interface),
            (link.remote_node, link.remote_interface),
        ):
            ep = (node_name, intf)
            if ep in seen_endpoints and seen_endpoints[ep] != link.name:
                raise ValueError(
                    f"links override produced an interface collision on "
                    f"{node_name} {intf}: used by both '{seen_endpoints[ep]}' "
                    f"and '{link.name}'"
                )
            seen_endpoints[ep] = link.name


# ---------------------------------------------------------------------------
# Services passthrough (design-specific builders only — shared builders
# are imported from automation.designs._common_builders at module top)
# ---------------------------------------------------------------------------


def _build_bridge_domains(
    raw: list[dict], origin: str = ""
) -> list[BridgeDomainIntent]:
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
            export_target=bd.get("export_target"),
            import_target=bd.get("import_target"),
            origin=origin,
        )
        for bd in raw
    ]


def _build_irb_interfaces(
    raw: list[dict], default_ip_mtu: int = 1500, origin: str = ""
) -> list[IrbInterfaceIntent]:
    """Build IRB interface intents, using default_ip_mtu when ip_mtu is not explicitly set."""
    return [
        IrbInterfaceIntent(
            name=irb["name"],
            bridge_domain=irb["bridge_domain"],
            router=irb["router"],
            ipv4=irb.get("ipv4", ""),
            ip_addresses=[
                IrbIpAddress(**a) for a in irb.get("ip_addresses", [])
            ],
            description=irb.get("description", ""),
            proxy_arp=irb.get("proxy_arp", True),
            proxy_nd=irb.get("proxy_nd", False),
            arp_timeout=irb.get("arp_timeout", 280),
            ip_mtu=irb.get("ip_mtu", default_ip_mtu),
            learn_unsolicited=irb.get("learn_unsolicited", "NONE"),
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
            origin=origin,
        )
        for irb in raw
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
            origin="3-stage",
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
            origin="3-stage",
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
                origin="3-stage",
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
                origin="3-stage",
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
# Utilities
# ---------------------------------------------------------------------------


def _parse_port_index(interface_name: str) -> int:
    """
    Parse port index from interface name.

    'ethernet-1-32' → 32
    'ethernet-1-5'  → 5
    """
    parts = interface_name.split("-")
    return int(parts[-1])
