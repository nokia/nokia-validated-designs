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
    Credentials,
    EdaSettings,
    FabricIntent,
    IrbIpAddress,
    IrbInterfaceIntent,
    LagIntent,
    LinkIntent,
    NodeIntent,
    PolicyAction,
    PolicyMatch,
    PolicyStatementIntent,
    PrefixEntry,
    PrefixSetIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
    StaticRouteIntent,
    VlanIntent,
)
from automation.core.extras import merge_by_name
from automation.core.platforms import (
    expand_breakout,
    get_platform,
    interface_name,
)
from automation.designs._common_builders import (
    build_banners as _build_banners,
    build_default_mtus as _build_default_mtus,
    build_edge_interfaces as _build_edge_interfaces,
    build_lags as _build_lags,
    build_prefix_sets as _build_prefix_sets,
    build_routed_interfaces as _build_routed_interfaces,
    build_routers as _build_routers,
    build_routing_policies as _build_routing_policies,
    build_static_routes as _build_static_routes,
    build_vlans as _build_vlans,
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

    # Derive the default IP MTU from the first DefaultMTU entry (if present)
    default_ip_mtu = 1500
    for mtu in default_mtus:
        if mtu.layer3_mtu is not None:
            default_ip_mtu = mtu.layer3_mtu
            break

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

    if topo_extras.get("configlets"):
        configlets = _merge_extras_configlets(
            configlets, topo_extras["configlets"]
        )

    if svc_extras.get("bridge_domains"):
        bridge_domains = _merge_extras_bridge_domains(
            bridge_domains, svc_extras["bridge_domains"]
        )

    if svc_extras.get("irb_interfaces"):
        irb_interfaces = _merge_extras_irb_interfaces(
            irb_interfaces, svc_extras["irb_interfaces"], default_ip_mtu
        )

    if svc_extras.get("routers"):
        routers = _merge_extras_routers(routers, svc_extras["routers"])

    if svc_extras.get("vlans"):
        vlans = _merge_extras_vlans(vlans, svc_extras["vlans"])

    if svc_extras.get("routed_interfaces"):
        routed_interfaces = _merge_extras_routed_interfaces(
            routed_interfaces, svc_extras["routed_interfaces"]
        )

    if svc_extras.get("static_routes"):
        static_routes = _merge_extras_static_routes(
            static_routes, svc_extras["static_routes"]
        )

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
    # Credentials
    # -----------------------------------------------------------------------
    creds_cfg = topology.get("credentials", {})
    credentials = Credentials(**creds_cfg) if creds_cfg else Credentials()

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


def _validate_unique_names(nodes: list[NodeIntent]) -> None:
    """Raise ValueError if any two nodes share the same name."""
    seen: dict[str, int] = {}
    for node in nodes:
        if node.name in seen:
            raise ValueError(
                f"Duplicate node name '{node.name}': name_template must include "
                f"{{i}} placeholder to produce unique names"
            )
        seen[node.name] = 1


def _apply_node_overrides(
    nodes: list[NodeIntent], overrides: list[dict]
) -> None:
    """Apply per-node overrides to auto-generated nodes (in-place).

    Raises ValueError if an override references a node name that was not
    auto-generated.
    """
    by_name = {n.name: n for n in nodes}

    for ovr in overrides:
        name = ovr["name"]
        if name not in by_name:
            raise ValueError(
                f"Node override references unknown node '{name}'. "
                f"Auto-generated nodes: {sorted(by_name)}"
            )
        node = by_name[name]

        if "platform" in ovr:
            node.platform = ovr["platform"]
        if "version" in ovr:
            node.version = ovr["version"]
        if "mgmt_ipv4" in ovr:
            node.mgmt_ipv4 = ovr["mgmt_ipv4"]
        if "labels" in ovr:
            node.labels = {**node.labels, **ovr["labels"]}


def _validate_mgmt_ips(nodes: list[NodeIntent]) -> None:
    """Raise ValueError if any two nodes share the same mgmt_ipv4."""
    seen: dict[str, str] = {}
    for node in nodes:
        if not node.mgmt_ipv4:
            continue
        ip = str(ipaddress.IPv4Address(node.mgmt_ipv4))
        if ip in seen:
            raise ValueError(
                f"Duplicate management IP {ip}: "
                f"assigned to both '{seen[ip]}' and '{node.name}'"
            )
        seen[ip] = node.name


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
# Extras merge — unconstrained additions/overrides
# ---------------------------------------------------------------------------


def _merge_extras_configlets(
    design: list[ConfigletIntent], extras_raw: list[dict]
) -> list[ConfigletIntent]:
    """Merge user-provided extras configlets with design-generated ones.

    If an extras configlet shares a name with a design configlet, the
    extras version replaces it entirely.  New names are appended.
    """
    by_name = {c.name: c for c in design}
    for raw in extras_raw:
        cfglet = ConfigletIntent(
            name=raw["name"],
            endpoint_selector=raw.get("endpoint_selector", []),
            endpoints=raw.get("endpoints", []),
            operating_system=raw.get("operating_system", "srl"),
            priority=raw.get("priority", 100),
            origin="extras",
            configs=[
                ConfigletConfigEntry(
                    path=c["path"],
                    operation=c.get("operation", "Update"),
                    config=c["config"],
                )
                for c in raw.get("configs", [])
            ],
        )
        by_name[cfglet.name] = cfglet
    return list(by_name.values())


def _merge_extras_bridge_domains(
    design: list[BridgeDomainIntent], extras_raw: list[dict]
) -> list[BridgeDomainIntent]:
    """Merge extras bridge domain overrides into design-generated ones."""
    def _set_origin(fields: dict) -> dict:
        fields["origin"] = "extras"
        return fields

    return merge_by_name(design, extras_raw, BridgeDomainIntent, pre_process=_set_origin)


def _merge_extras_irb_interfaces(
    design: list[IrbInterfaceIntent],
    extras_raw: list[dict],
    default_ip_mtu: int = 1500,
) -> list[IrbInterfaceIntent]:
    """Merge extras IRB overrides into design-generated ones."""
    def _pre_process(fields: dict) -> dict:
        if "ip_addresses" in fields:
            fields["ip_addresses"] = [
                IrbIpAddress(**a) for a in fields["ip_addresses"]
            ]
        fields.setdefault("ip_mtu", default_ip_mtu)
        fields["origin"] = "extras"
        return fields

    return merge_by_name(design, extras_raw, IrbInterfaceIntent, pre_process=_pre_process)


def _merge_extras_routers(
    design: list[RouterIntent], extras_raw: list[dict]
) -> list[RouterIntent]:
    """Merge extras router overrides into design-generated ones."""
    return merge_by_name(design, extras_raw, RouterIntent)


def _merge_extras_vlans(
    design: list[VlanIntent], extras_raw: list[dict]
) -> list[VlanIntent]:
    """Merge extras VLAN overrides into design-generated ones."""
    return merge_by_name(design, extras_raw, VlanIntent)


def _merge_extras_routed_interfaces(
    design: list[RoutedInterfaceIntent], extras_raw: list[dict]
) -> list[RoutedInterfaceIntent]:
    """Merge extras routed interface overrides into design-generated ones."""
    return merge_by_name(design, extras_raw, RoutedInterfaceIntent)


def _merge_extras_static_routes(
    design: list[StaticRouteIntent], extras_raw: list[dict]
) -> list[StaticRouteIntent]:
    """Merge extras static route overrides into design-generated ones."""
    return merge_by_name(design, extras_raw, StaticRouteIntent)


# ---------------------------------------------------------------------------
# Routing-policy defaults
# ---------------------------------------------------------------------------


def _normalize_policy_update(fields: dict) -> dict:
    """Pre-process hook for merging user-supplied routing policies.

    ``merge_by_name`` uses ``model_copy(update=...)`` for the overlay path,
    which does *not* validate nested values. Convert raw statement dicts
    (as they appear in YAML) into ``PolicyStatementIntent`` models so the
    resulting ``RoutingPolicyIntent`` has a consistent field type.

    Also force ``internal=False`` so a user override of a design default
    (which has ``internal=True``) becomes a regular user-declared policy.
    """
    fields["internal"] = False
    if "statements" in fields and fields["statements"]:
        converted: list[PolicyStatementIntent] = []
        for stmt in fields["statements"]:
            if isinstance(stmt, PolicyStatementIntent):
                converted.append(stmt)
                continue
            m = stmt.get("match", {}) or {}
            a = stmt.get("action", {}) or {}
            converted.append(
                PolicyStatementIntent(
                    name=str(stmt["name"]),
                    match=PolicyMatch(
                        prefix_set=m.get("prefix_set"),
                        protocol=m.get("protocol"),
                        bgp_evpn_route_types=m.get("bgp_evpn_route_types"),
                    ),
                    action=PolicyAction(
                        result=a.get("result", "accept"),
                        set_local_preference=a.get("set_local_preference"),
                    ),
                )
            )
        fields["statements"] = converted
    return fields


def _normalize_prefix_set_update(fields: dict) -> dict:
    """Pre-process hook for merging user-supplied prefix sets.

    Forces ``internal=False`` so a user override of a design default
    (``internal=True``) becomes a regular user-declared prefix set.
    """
    fields["internal"] = False
    if "prefixes" in fields and fields["prefixes"]:
        converted: list[PrefixEntry] = []
        for p in fields["prefixes"]:
            if isinstance(p, PrefixEntry):
                converted.append(p)
                continue
            converted.append(
                PrefixEntry(
                    ip_prefix=p["ip_prefix"],
                    mask_length_range=p.get("mask_length_range", "exact"),
                )
            )
        fields["prefixes"] = converted
    return fields


def _default_routing_policies(
    fabric_name: str, system0_prefix: str
) -> tuple[PrefixSetIntent, RoutingPolicyIntent, RoutingPolicyIntent]:
    """Return the three routing-policy artifacts the 3-stage design requires.

    - ``prefixset-{fabric}`` matches the system0 loopback supernet on a /32 key.
    - ``ebgp-isl-export-policy-{fabric}`` accepts local/bgp/aggregate and the
      five EVPN route-types, setting local-preference to 100.
    - ``ebgp-isl-import-policy-{fabric}`` accepts bgp and the five EVPN
      route-types, setting local-preference to 100.

    Both policies default-reject. Users can override any entry by name via
    ``topology.prefix_sets`` / ``services.routing_policies`` in input YAML.
    """
    prefix_set_name = f"prefixset-{fabric_name}"
    export_name = f"ebgp-isl-export-policy-{fabric_name}"
    import_name = f"ebgp-isl-import-policy-{fabric_name}"

    ps = PrefixSetIntent(
        name=prefix_set_name,
        prefixes=[PrefixEntry(ip_prefix=system0_prefix, mask_length_range="32..32")],
        internal=True,
    )

    accept = PolicyAction(result="accept", set_local_preference=100)
    export_statements = [
        PolicyStatementIntent(
            name="10",
            match=PolicyMatch(prefix_set=prefix_set_name, protocol="local"),
            action=accept,
        ),
        PolicyStatementIntent(
            name="15", match=PolicyMatch(protocol="bgp"), action=accept
        ),
        PolicyStatementIntent(
            name="20", match=PolicyMatch(protocol="aggregate"), action=accept
        ),
    ]
    for stmt_id, rt in (("25", 1), ("30", 2), ("35", 3), ("40", 4), ("45", 5)):
        export_statements.append(
            PolicyStatementIntent(
                name=stmt_id,
                match=PolicyMatch(bgp_evpn_route_types=[rt]),
                action=accept,
            )
        )

    import_statements = [
        PolicyStatementIntent(
            name="10", match=PolicyMatch(protocol="bgp"), action=accept
        ),
    ]
    for stmt_id, rt in (("25", 1), ("30", 2), ("35", 3), ("40", 4), ("45", 5)):
        import_statements.append(
            PolicyStatementIntent(
                name=stmt_id,
                match=PolicyMatch(bgp_evpn_route_types=[rt]),
                action=accept,
            )
        )

    export = RoutingPolicyIntent(
        name=export_name,
        default_action="reject",
        statements=export_statements,
        internal=True,
    )
    imp = RoutingPolicyIntent(
        name=import_name,
        default_action="reject",
        statements=import_statements,
        internal=True,
    )
    return ps, export, imp


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
