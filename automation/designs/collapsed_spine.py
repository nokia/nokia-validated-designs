"""
Collapsed-spine design builder.

Transforms simple input (topology.yaml + services.yaml) into a FabricIntent
for the collapsed-spine EVPN-VXLAN design.

Characteristics:
- Two collapsed-spines (role="leaf", label eda.nokia.com/role=collapsed-spine)
  host all overlay services and are the EVPN "leafs" in the Fabric CR.
- Explicit ToR nodes (role="tor") are onboarded into EDA but kept out of
  the Fabric CR's leafs/spines selectors.
- ISL links are enumerated explicitly — no full-mesh auto-generation.
- Single collapsed_spine_asn_start — each collapsed-spine gets
  start+i; ToRs have no ASN.
- Bridge domains may be EVPNVXLAN (default) or SIMPLE (L2-only,
  used by ToRs); the ``type`` field flows through into EDA CRs.
- IRBs are dual-stack (IPv4 + IPv6).
"""

from __future__ import annotations

import ipaddress
import logging

from automation.core.extras import merge_by_name
from automation.core.models import (
    BridgeDomainIntent,
    FabricIntent,
    IrbInterfaceIntent,
    IrbIpAddress,
    LinkIntent,
    NodeIntent,
    PrefixSetIntent,
    RoutingPolicyIntent,
)
from automation.core.platforms import get_platform
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build(topology: dict, services: dict) -> FabricIntent:
    """Build a FabricIntent from collapsed-spine input."""
    fabric_name = topology["fabric_name"]
    environment = topology.get("environment", "containerlab")
    underlay = topology["underlay"]

    cs_asn_start = underlay["collapsed_spine_asn_start"]
    system0_prefix = underlay["system0_prefix"]

    cs_cfg = topology["collapsed_spines"]
    tor_entries: list[dict] = topology.get("tors", [])

    # Validate platforms
    cs_platform = get_platform(cs_cfg["platform"])
    if not (
        cs_platform.validate_role("collapsed-spine")
        or cs_platform.validate_role("leaf")
    ):
        raise ValueError(
            f"Platform '{cs_platform.name}' is not allowed as "
            f"collapsed-spine/leaf in collapsed-spine design"
        )
    for entry in tor_entries:
        tor_platform = get_platform(entry["platform"])
        if not tor_platform.validate_role("tor"):
            raise ValueError(
                f"Platform '{tor_platform.name}' (node '{entry['name']}') "
                f"is not allowed as tor in collapsed-spine design"
            )

    # -----------------------------------------------------------------------
    # Nodes
    # -----------------------------------------------------------------------
    nodes = _build_nodes(
        cs_cfg=cs_cfg,
        tor_entries=tor_entries,
        cs_asn_start=cs_asn_start,
        system0_prefix=system0_prefix,
        node_overrides=topology.get("nodes"),
    )

    # -----------------------------------------------------------------------
    # ISLs — explicit, no full-mesh
    # -----------------------------------------------------------------------
    links = _build_isl_links(topology.get("isl_links", []), nodes)

    # -----------------------------------------------------------------------
    # Edge + LAG
    # -----------------------------------------------------------------------
    edge_interfaces = _build_edge_interfaces(topology.get("edge_interfaces", []))
    lags = _build_lags(topology.get("lags", []))

    # -----------------------------------------------------------------------
    # Default MTUs (used to derive default IP MTU for IRBs)
    # -----------------------------------------------------------------------
    default_mtus = _build_default_mtus(topology.get("default_mtu", []))
    default_ip_mtu = _derive_default_ip_mtu(default_mtus)

    # -----------------------------------------------------------------------
    # Services
    # -----------------------------------------------------------------------
    bridge_domains = _build_bridge_domains(
        services.get("bridge_domains", []), origin="collapsed-spine"
    )
    routers = _build_routers(services.get("routers", []))
    irb_interfaces = _build_irb_interfaces(
        services.get("irb_interfaces", []), default_ip_mtu, origin="collapsed-spine"
    )
    vlans = _build_vlans(services.get("vlans", []))
    routed_interfaces = _build_routed_interfaces(
        services.get("routed_interfaces", [])
    )
    static_routes = _build_static_routes(services.get("static_routes", []))

    # -----------------------------------------------------------------------
    # Configlets — design-specific device config, then extras overrides.
    # Extras configlets replace by name, so they don't go through the generic
    # apply_extras table.
    # -----------------------------------------------------------------------
    configlets = _build_configlets(lags)
    topo_extras = topology.get("extras", {})
    if topo_extras.get("configlets"):
        configlets = _merge_extras_configlets(configlets, topo_extras["configlets"])

    # -----------------------------------------------------------------------
    # Service extras — unconstrained additions/overrides from user input
    # -----------------------------------------------------------------------
    merged_extras = _apply_service_extras(
        {
            "bridge_domains": bridge_domains,
            "irb_interfaces": irb_interfaces,
            "routers": routers,
            "vlans": vlans,
            "routed_interfaces": routed_interfaces,
            "static_routes": static_routes,
        },
        services.get("extras", {}),
        default_ip_mtu=default_ip_mtu,
    )
    bridge_domains = merged_extras["bridge_domains"]
    irb_interfaces = merged_extras["irb_interfaces"]
    routers = merged_extras["routers"]
    vlans = merged_extras["vlans"]
    routed_interfaces = merged_extras["routed_interfaces"]
    static_routes = merged_extras["static_routes"]

    # -----------------------------------------------------------------------
    # Banners + routing-policy defaults
    # -----------------------------------------------------------------------
    banners = _build_banners(topology.get("banners", []))

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
    # Credentials + EDA
    # -----------------------------------------------------------------------
    credentials = _build_credentials(topology)
    eda_settings = _build_eda_settings(topology)

    # The collapsed-spine design has no independent spine_asn — reuse the
    # same pool for both slots so FabricIntent bookkeeping (which was
    # built for a 3-stage shape) doesn't complain. The EDA generator's
    # Fabric CR builder detects the absence of role=spine nodes and omits
    # the spines block entirely, using a single 'collapsed-spine-asn' pool.
    return FabricIntent(
        design="collapsed-spine",
        fabric_name=fabric_name,
        environment=environment,
        spine_asn=cs_asn_start,
        leaf_asn_start=cs_asn_start,
        system0_prefix=system0_prefix,
        mgmt_subnet=topology.get("mgmt_subnet", ""),
        nodes=nodes,
        links=links,
        breakouts=[],
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
# Nodes
# ---------------------------------------------------------------------------


def _build_nodes(
    *,
    cs_cfg: dict,
    tor_entries: list[dict],
    cs_asn_start: int,
    system0_prefix: str,
    node_overrides: list[dict] | None = None,
) -> list[NodeIntent]:
    """Generate collapsed-spine and ToR NodeIntent objects."""
    network = ipaddress.IPv4Network(system0_prefix)
    nodes: list[NodeIntent] = []

    # Collapsed-spines — hosted as role="leaf" so existing generators that
    # iterate by role (e.g. routers placed on leafs, underlay wiring) keep
    # working. The EDA label eda.nokia.com/role=collapsed-spine drives the
    # Fabric CR leaf selector.
    cs_count = cs_cfg["count"]
    cs_template = cs_cfg.get("name_template", "spine{i}")
    cs_labels = cs_cfg.get("labels", {})
    cs_mgmt_base = cs_cfg.get("mgmt_base_ipv4", "")
    for i in range(1, cs_count + 1):
        ip_offset = 100 + i  # spine1=.101, spine2=.102, ...
        sys0_ip = str(network.network_address + ip_offset)
        mgmt_ip = _increment_ip(cs_mgmt_base, i - 1) if cs_mgmt_base else ""
        node_name = cs_template.format(i=i)

        # Ensure the role label is set even if the user didn't provide one.
        node_labels = {
            "eda.nokia.com/role": "collapsed-spine",
            **cs_labels,
            "eda.nokia.com/name": node_name,
            "eda.nokia.com/security-profile": "managed",
        }
        nodes.append(
            NodeIntent(
                name=node_name,
                role="leaf",  # core role — participates in Fabric CR as leaf
                platform=cs_cfg["platform"],
                version=cs_cfg.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=cs_asn_start + i - 1,
                mgmt_ipv4=mgmt_ip,
                labels=node_labels,
            )
        )

    # ToRs — explicit list. No ASN, no system0 loopback (they don't run
    # BGP), but an IPv4 is still assigned to keep cross-ref checks happy.
    for idx, entry in enumerate(tor_entries):
        sys0_ip = str(network.network_address + 10 + idx + 1)  # .11, .12, ...
        labels = {
            "eda.nokia.com/role": "tor",
            **entry.get("labels", {}),
            "eda.nokia.com/name": entry["name"],
            "eda.nokia.com/security-profile": "managed",
        }
        nodes.append(
            NodeIntent(
                name=entry["name"],
                role="tor",
                platform=entry["platform"],
                version=entry.get("version", ""),
                system0_ipv4=f"{sys0_ip}/32",
                asn=0,
                mgmt_ipv4=entry["mgmt_ipv4"],
                labels=labels,
            )
        )

    _validate_unique_names(nodes)

    if node_overrides:
        _apply_node_overrides(nodes, node_overrides)

    _validate_mgmt_ips(nodes)

    return nodes


# ---------------------------------------------------------------------------
# ISL links (explicit pass-through, compute uplink interfaces)
# ---------------------------------------------------------------------------


def _build_isl_links(
    raw_links: list[dict], nodes: list[NodeIntent]
) -> list[LinkIntent]:
    """Turn explicit isl_links input into LinkIntent + record uplink
    interfaces on the involved nodes."""
    by_name = {n.name: n for n in nodes}
    links: list[LinkIntent] = []
    for i, link in enumerate(raw_links, start=1):
        a_node = link["local_node"]
        b_node = link["remote_node"]
        if a_node not in by_name:
            raise ValueError(f"ISL references unknown node '{a_node}'")
        if b_node not in by_name:
            raise ValueError(f"ISL references unknown node '{b_node}'")
        a_intf = link["local_interface"]
        b_intf = link["remote_interface"]
        # Record uplinks on each endpoint for downstream config generation.
        if a_intf not in by_name[a_node].uplink_interfaces:
            by_name[a_node].uplink_interfaces.append(a_intf)
        if b_intf not in by_name[b_node].uplink_interfaces:
            by_name[b_node].uplink_interfaces.append(b_intf)

        name = link.get("name") or f"{a_node}-{b_node}-{i}"
        links.append(
            LinkIntent(
                name=name,
                local_node=a_node,
                local_interface=a_intf,
                remote_node=b_node,
                remote_interface=b_intf,
            )
        )
    return links


# ---------------------------------------------------------------------------
# Service builders (design-local, honor SIMPLE BDs and dual-stack IRBs)
# ---------------------------------------------------------------------------


def _build_bridge_domains(
    raw: list[dict], origin: str = ""
) -> list[BridgeDomainIntent]:
    result: list[BridgeDomainIntent] = []
    for bd in raw:
        bd_type = bd.get("type", "EVPNVXLAN")
        kwargs: dict = {
            "name": bd["name"],
            "type": bd_type,
            "mac_learning": bd.get("mac_learning", True),
            "mac_aging": bd.get("mac_aging", 300),
            "export_target": bd.get("export_target"),
            "import_target": bd.get("import_target"),
            "origin": origin,
        }
        if bd_type == "EVPNVXLAN":
            kwargs["vni"] = bd["vni"]
            kwargs["evi"] = bd["evi"]
            kwargs["mac_duplication"] = bd.get(
                "mac_duplication",
                {
                    "enabled": True,
                    "hold_down_time": 9,
                    "monitoring_window": 3,
                    "action": "StopLearning",
                    "num_moves": 5,
                },
            )
        else:
            # SIMPLE: no VXLAN envelope, but keep the standard
            # MAC-duplication-detection defaults (matches the existing
            # eda-manifests reference).
            kwargs["mac_duplication"] = bd.get(
                "mac_duplication",
                {
                    "enabled": True,
                    "hold_down_time": 9,
                    "monitoring_window": 3,
                    "action": "StopLearning",
                    "num_moves": 5,
                },
            )
        result.append(BridgeDomainIntent(**kwargs))
    return result


def _build_irb_interfaces(
    raw: list[dict], default_ip_mtu: int = 1500, origin: str = ""
) -> list[IrbInterfaceIntent]:
    """Dual-stack IRB builder."""
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
                    "ndDynamic": True,
                    "ndStatic": True,
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


def _build_configlets(lags):
    """Design-specific device configuration for collapsed-spine.

    - BGP EVPN rapid-update + rapid route-withdrawal on all
      collapsed-spines (match the manual reference deployment).
    - ESI DF-election activation-timer per ESI LAG (LAGs with members
      spanning multiple nodes — i.e. multi-homed LAGs on the collapsed
      spines).
    """
    from automation.core.models import ConfigletConfigEntry, ConfigletIntent

    configlets: list[ConfigletIntent] = []

    # BGP rapid update (collapsed-spines only)
    configlets.append(
        ConfigletIntent(
            name="bgp-evpn-rapid",
            endpoint_selector=["eda.nokia.com/role=collapsed-spine"],
            operating_system="srl",
            priority=100,
            origin="collapsed-spine",
            configs=[
                ConfigletConfigEntry(
                    path='.network-instance{.name=="default"}.protocols.bgp.afi-safi{.afi-safi-name=="evpn"}.evpn',
                    operation="Update",
                    config='{\n  "rapid-update": "true"\n}',
                )
            ],
        )
    )

    configlets.append(
        ConfigletIntent(
            name="bgp-rapid-route-withdraw",
            endpoint_selector=["eda.nokia.com/role=collapsed-spine"],
            operating_system="srl",
            priority=100,
            origin="collapsed-spine",
            configs=[
                ConfigletConfigEntry(
                    path='.network-instance{.name=="default"}.protocols.bgp.route-advertisement',
                    operation="Update",
                    config='{\n  "rapid-withdrawal": "true"\n}',
                )
            ],
        )
    )

    # ESI DF election activation timer — only for LAGs whose members span
    # multiple nodes (true ESI LAGs). Single-chassis ToR LAGs are skipped.
    for lag in lags:
        member_nodes = sorted({m.node for m in lag.members})
        if len(member_nodes) < 2:
            continue
        configlets.append(
            ConfigletIntent(
                name=lag.name,
                endpoints=member_nodes,
                operating_system="srl",
                priority=100,
                origin="collapsed-spine",
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


# The eBGP ISL prefix-set and import/export policy defaults are identical to
# the 3-stage design's, so they live in _common_builders.default_routing_policies.
