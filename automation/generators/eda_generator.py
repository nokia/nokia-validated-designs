"""
EDA CR generator.

Converts a design-agnostic FabricIntent into an ordered list of
EDA Custom Resource dicts, ready for the EDA Transaction API.

This module is 100% design-agnostic — it works for any NVD design
as long as it receives a valid FabricIntent.

Uses auto-generated Pydantic models from automation.eda_models for
type-safe spec construction with automatic camelCase serialization.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automation.core.models import (
    BridgeDomainIntent,
    BreakoutIntent,
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    LinkIntent,
    NodeIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    StaticRouteIntent,
    VlanIntent,
)

from automation.eda_models.services import (
    BridgeDomainSpec,
    BridgeDomainMacDuplicationDetection,
    IRBInterfaceSpec,
    IRBInterfaceIpAddresses,
    IRBInterfaceIpv4Addresses,
    IRBInterfaceIpv6Addresses,
    IRBInterfaceL3ProxyArpNd,
    IRBInterfaceEvpnRouteAdvertisementType,
    IRBInterfaceHostRoutePopulation,
    RouterSpec,
    VLANSpec,
    RoutedInterfaceSpec,
    RoutedInterfaceIpv4Addresses,
)
from automation.eda_models.protocols import StaticRouteSpec
from automation.eda_models.fabrics import (
    FabricSpec,
    FabricUnderlayProtocol,
    FabricUnderlayProtocolBfd,
    FabricOverlayProtocol,
    FabricInterswitchlinks,
    FabricLeafs,
    FabricSpines,
)
from automation.eda_models.core import (
    TopoNodeSpec,
    TopoNodeProductionAddress,
    TopoNodeNpp,
    TopoLinkSpec,
    TopoLinkLinks,
    TopoLinkA,
    TopoLinkB,
    NodeProfileSpec,
    NodeProfileImages,
    NodeUserSpec,
    NodeUserGroupBindings,
    IndexAllocationPoolSpec,
    IndexAllocationPoolSegments,
    IPAllocationPoolSpec,
    IPAllocationPoolSegments,
)
from automation.eda_models.interfaces import (
    InterfaceSpec,
    InterfaceMembers,
    InterfaceFallback,
    InterfaceLag,
    InterfaceLacp,
    InterfaceMultiHoming,
    InterfaceEthernet,
)
from automation.eda_models.bootstrap import InitSpec, InitMgmt
from automation.eda_models.config import ConfigletSpec, ConfigletConfigurations

logger = logging.getLogger(__name__)

MANAGED_BY_LABEL = "eda.nokia.com/managed-by"
MANAGED_BY_VALUE = "nvd-automation"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def generate(intent: FabricIntent, output_dir: Path | None = None) -> list[dict]:
    """
    Generate all EDA CRs from a FabricIntent.

    Args:
        intent: The complete fabric intent
        output_dir: If set, write eda_transaction.json here

    Returns:
        Ordered list of EDA CR dicts
    """
    ns = intent.eda.namespace
    resources: list[dict] = []

    # 1. Init (commitSave)
    resources.append(_cr_init(ns))

    # 2. NodeUser
    resources.append(_cr_node_user(ns))

    # 3. NodeProfile
    if intent.eda.node_profile:
        node = intent.nodes[0] if intent.nodes else None
        version = node.version if node else ""
        resources.append(
            _cr_node_profile(ns, intent.eda.node_profile, version)
        )

    # 4. TopoNodes (individual per-node CRs)
    for node in intent.nodes:
        resources.append(_cr_topo_node(node, intent.eda.node_profile, ns))

    # 5. Interfaces — ISL interfaces (per node endpoint)
    for link in intent.links:
        resources.append(_cr_interface_isl(link.local_node, link.local_interface, ns))
        resources.append(_cr_interface_isl(link.remote_node, link.remote_interface, ns))

    # 6. Interfaces — Edge interfaces
    for ei in intent.edge_interfaces:
        resources.append(_cr_interface_edge(ei, ns))

    # 7. Interfaces — LAG member interfaces
    for lag in intent.lags:
        for member in lag.members:
            resources.append(
                _cr_interface_lag_member(member.node, member.interface, ns)
            )

    # 8. LAG Interfaces
    for lag in intent.lags:
        resources.append(_cr_interface_lag(lag, ns))

    # 9. Links
    for link in intent.links:
        resources.append(_cr_topo_link(link, ns))

    # 10. ASN allocation pools
    resources.append(
        _cr_index_allocation_pool(
            "leaf-asn", intent.leaf_asn_start, 20, ns
        )
    )
    resources.append(
        _cr_index_allocation_pool(
            "spine-asn", intent.spine_asn, 10, ns
        )
    )

    # 11. IP allocation pool (system0)
    resources.append(
        _cr_ip_allocation_pool("system0", intent.system0_prefix, ns)
    )

    # 12. Fabric
    resources.append(_cr_fabric(intent, ns))

    # 13. Bridge domains
    for bd in intent.bridge_domains:
        resources.append(_cr_bridge_domain(bd, ns))

    # 14. Routers
    for router in intent.routers:
        resources.append(_cr_router(router, ns))

    # 15. IRB interfaces
    for irb in intent.irb_interfaces:
        resources.append(_cr_irb_interface(irb, ns))

    # 16. VLANs
    for vlan in intent.vlans:
        resources.append(_cr_vlan(vlan, ns))

    # 17. Routed interfaces
    for ri in intent.routed_interfaces:
        resources.append(_cr_routed_interface(ri, ns))

    # 18. Static routes
    for sr in intent.static_routes:
        resources.append(_cr_static_route(sr, ns))

    # 19. Configlets (BGP rapid, node isolation, ESI DF timers)
    resources.extend(_cr_configlets(intent, ns))

    # De-duplicate ISL interfaces (each endpoint appears once)
    resources = _deduplicate(resources)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        tx_path = output_dir / "eda_transaction.json"
        with open(tx_path, "w") as f:
            json.dump(resources, f, indent=2)
        logger.info("Wrote %d CRs to %s", len(resources), tx_path)

    return resources


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _managed_labels(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return labels with the managed-by marker."""
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE}
    if extra:
        labels.update(extra)
    return labels


def _wrap_cr(
    api_version: str,
    kind: str,
    name: str,
    ns: str,
    spec: BaseModel,
    labels: dict[str, str] | None = None,
) -> dict:
    """Wrap a Pydantic spec model into a full EDA CR dict."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or _managed_labels(),
        },
        "spec": spec.model_dump(by_alias=True, exclude_none=True),
    }


def _wrap_cr_raw(
    api_version: str,
    kind: str,
    name: str,
    ns: str,
    spec: dict,
    labels: dict[str, str] | None = None,
) -> dict:
    """Wrap a raw spec dict into a full EDA CR dict (for complex cases)."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or _managed_labels(),
        },
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# CR builders — using generated models
# ---------------------------------------------------------------------------


def _cr_init(ns: str) -> dict:
    spec = InitSpec(commit_save=True, mgmt=InitMgmt(ipv4_dhcp=True, ipv6_dhcp=True))
    return _wrap_cr("bootstrap.eda.nokia.com/v1alpha1", "Init", "init-base", ns, spec)


def _cr_node_user(ns: str) -> dict:
    spec = NodeUserSpec(
        username="admin",
        password="NokiaSrl1!",
        group_bindings=[NodeUserGroupBindings(groups=["sudo"], node_selector=[""])],
    )
    return _wrap_cr("core.eda.nokia.com/v1", "NodeUser", "admin", ns, spec)


def _cr_node_profile(ns: str, profile_name: str, version: str) -> dict:
    ver_escaped = version.replace(".", "\\.")
    spec = NodeProfileSpec(
        images=[
            NodeProfileImages(
                image=f"srlimages/srlinux-{version}-bin/srlinux.bin",
                image_md5=f"srlimages/srlinux-{version}-md5/srlinux.md5",
            )
        ],
        llm_db=f"https://eda-asvr.eda-system.svc/eda-system/llm-dbs/llm-db-srlinux-ghcr-{version}/llm-embeddings-srl-{version.replace('.', '-')}.tar.gz",
        node_user="admin",
        onboarding_username="admin",
        onboarding_password="NokiaSrl1!",
        operating_system="srl",
        port=57410,
        version=version,
        version_match=f"v{ver_escaped}.*",
        version_path=".system.information.version",
        yang=f"https://eda-asvr.eda-system.svc/eda-system/schemaprofiles/srlinux-ghcr-{version}/srlinux-{version}.zip",
        annotate=True,
    )
    return _wrap_cr("core.eda.nokia.com/v1", "NodeProfile", profile_name, ns, spec)


def _cr_topo_node(node: NodeIntent, node_profile: str, ns: str) -> dict:
    """Generate a TopoNode CR for each node."""
    labels = _managed_labels({
        "eda.nokia.com/name": node.name,
        **node.labels,
    })
    if "eda.nokia.com/security-profile" not in labels:
        labels["eda.nokia.com/security-profile"] = "managed"

    spec = TopoNodeSpec(
        node_profile=node_profile,
        operating_system="srl",
        platform=node.platform,
        version=node.version,
        on_boarded=True,
        production_address=TopoNodeProductionAddress(ipv4=node.mgmt_ipv4, ipv6=""),
        npp=TopoNodeNpp(mode="normal"),
    )
    return _wrap_cr("core.eda.nokia.com/v1", "TopoNode", node.name, ns, spec, labels)


def _cr_interface_isl(node: str, interface: str, ns: str) -> dict:
    """Generate an ISL Interface CR."""
    name = f"{node}-{interface.replace('/', '-')}"
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=interface, node=node)],
        type="interface",
    )
    return _wrap_cr(
        "interfaces.eda.nokia.com/v1alpha1", "Interface", name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "interSwitch"}),
    )


def _cr_interface_edge(ei: EdgeInterfaceIntent, ns: str) -> dict:
    """Generate an edge Interface CR."""
    labels = _managed_labels({**ei.labels, "eda.nokia.com/role": "edge"})
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=ei.interface, node=ei.node)],
        type="interface",
        encap_type=ei.encap if ei.encap in ("dot1q", "null") else None,
    )
    return _wrap_cr("interfaces.eda.nokia.com/v1alpha1", "Interface", ei.name, ns, spec, labels)


def _cr_interface_lag_member(node: str, interface: str, ns: str) -> dict:
    """Generate a LAG member Interface CR."""
    name = f"{node}-{interface.replace('/', '-')}"
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        encap_type="null",
        members=[InterfaceMembers(enabled=True, interface=interface, node=node)],
        type="interface",
    )
    return _wrap_cr(
        "interfaces.eda.nokia.com/v1alpha1", "Interface", name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "edge"}),
    )


def _cr_interface_lag(lag: LagIntent, ns: str) -> dict:
    """Generate a LAG Interface CR."""
    labels = _managed_labels({**lag.labels, "eda.nokia.com/role": "edge"})

    members = [
        InterfaceMembers(
            enabled=True,
            interface=m.interface,
            node=m.node,
            aggregate_id=m.aggregate_id,
            lacp_port_priority=m.lacp_port_priority,
        )
        for m in lag.members
    ]

    lacp = InterfaceLacp(
        mode="active",
        interval=lag.lacp.interval,
        system_priority=lag.lacp.system_priority,
        system_id_mac=lag.lacp.system_id_mac,
    )
    if lag.lacp.fallback:
        lacp.lacp_fallback = InterfaceFallback(
            mode=lag.lacp.fallback.get("mode", "static"),
            timeout=lag.lacp.fallback.get("timeout", 60),
        )

    lag_config = InterfaceLag(
        type="lacp",
        min_links=lag.min_links,
        lacp=lacp,
        multihoming=InterfaceMultiHoming(
            mode=lag.multihoming_mode,
            revertive=lag.revertive,
            preferred_active_node=lag.preferred_active_node,
            reload_delay_timer=lag.reload_delay_timer,
            esi="auto",
        ),
    )

    ethernet = InterfaceEthernet(reload_delay_timer=lag.reload_delay_timer)
    if lag.multihoming_mode == "port-active" and lag.standby_signaling:
        ethernet.standby_signaling = lag.standby_signaling

    spec = InterfaceSpec(
        enabled=True,
        type="lag",
        encap_type="dot1q",
        lldp=True,
        ethernet=ethernet,
        members=members,
        lag=lag_config,
    )
    return _wrap_cr("interfaces.eda.nokia.com/v1alpha1", "Interface", lag.name, ns, spec, labels)


def _cr_topo_link(link: LinkIntent, ns: str) -> dict:
    """Generate a TopoLink CR."""
    local_intf_name = f"{link.local_node}-{link.local_interface.replace('/', '-')}"
    remote_intf_name = f"{link.remote_node}-{link.remote_interface.replace('/', '-')}"
    spec = TopoLinkSpec(
        links=[
            TopoLinkLinks(
                local=TopoLinkA(
                    node=link.local_node,
                    interface=link.local_interface,
                    interface_resource=local_intf_name,
                ),
                remote=TopoLinkB(
                    node=link.remote_node,
                    interface=link.remote_interface,
                    interface_resource=remote_intf_name,
                ),
                type="interSwitch",
            )
        ],
    )
    return _wrap_cr(
        "core.eda.nokia.com/v1", "TopoLink", link.name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "interSwitch"}),
    )


def _cr_index_allocation_pool(
    name: str, start: int, size: int, ns: str
) -> dict:
    spec = IndexAllocationPoolSpec(
        segments=[IndexAllocationPoolSegments(start=start, size=size)]
    )
    return _wrap_cr("core.eda.nokia.com/v1", "IndexAllocationPool", name, ns, spec)


def _cr_ip_allocation_pool(name: str, subnet: str, ns: str) -> dict:
    spec = IPAllocationPoolSpec(
        segments=[IPAllocationPoolSegments(subnet=subnet)]
    )
    return _wrap_cr("core.eda.nokia.com/v1", "IPAllocationPool", name, ns, spec)


def _cr_fabric(intent: FabricIntent, ns: str) -> dict:
    """Generate the Fabric CR."""
    leaf_selector = []
    spine_selector = []
    for node in intent.nodes:
        role_label = node.labels.get("eda.nokia.com/role", node.role)
        if node.role == "leaf":
            sel = f"eda.nokia.com/role={role_label}"
            if sel not in leaf_selector:
                leaf_selector.append(sel)
        else:
            sel = f"eda.nokia.com/role={role_label}"
            if sel not in spine_selector:
                spine_selector.append(sel)

    spec = FabricSpec(
        underlay_protocol=FabricUnderlayProtocol(
            protocol=["EBGP"],
            bfd=FabricUnderlayProtocolBfd(
                enabled=True,
                desired_min_transmit_int=1000000,
                required_min_receive=1000000,
                detection_multiplier=3,
                min_echo_receive_interval=1000000,
            ),
        ),
        overlay_protocol=FabricOverlayProtocol(protocol="EBGP"),
        system_pool_ipv4="system0",
        inter_switch_links=FabricInterswitchlinks(
            link_selector=["eda.nokia.com/role=interSwitch"],
            unnumbered="IPV6",
        ),
        leafs=FabricLeafs(asn_pool="leaf-asn", leaf_node_selector=leaf_selector),
        spines=FabricSpines(asn_pool="spine-asn", spine_node_selector=spine_selector),
    )
    return _wrap_cr("fabrics.eda.nokia.com/v1alpha1", "Fabric", intent.fabric_name, ns, spec)


def _cr_bridge_domain(bd: BridgeDomainIntent, ns: str) -> dict:
    """Generate a BridgeDomain CR."""
    mac_dup = None
    if bd.mac_duplication:
        mac_dup = BridgeDomainMacDuplicationDetection(
            enabled=bd.mac_duplication.get("enabled", True),
            hold_down_time=bd.mac_duplication.get("hold_down_time", 9),
            monitoring_window=bd.mac_duplication.get("monitoring_window", 3),
            action=bd.mac_duplication.get("action", "StopLearning"),
            num_moves=bd.mac_duplication.get("num_moves", 5),
        )
    spec = BridgeDomainSpec(vni=bd.vni, evi=bd.evi, mac_duplication_detection=mac_dup)
    return _wrap_cr("services.eda.nokia.com/v1", "BridgeDomain", bd.name, ns, spec)


def _cr_router(router: RouterIntent, ns: str) -> dict:
    """Generate a Router CR."""
    spec = RouterSpec(vni=router.vni, evi=router.evi, node_selector=router.node_selector)
    return _wrap_cr("services.eda.nokia.com/v1", "Router", router.name, ns, spec)


def _cr_irb_interface(irb: IrbInterfaceIntent, ns: str) -> dict:
    """Generate an IRBInterface CR."""

    # Build ipAddresses from either ip_addresses list or legacy ipv4 shorthand
    ip_addrs: list[IRBInterfaceIpAddresses] = []
    if irb.ip_addresses:
        for addr in irb.ip_addresses:
            ipv4 = None
            ipv6 = None
            if addr.ipv4:
                ipv4 = IRBInterfaceIpv4Addresses(
                    ip_prefix=addr.ipv4.get("ip_prefix", ""),
                    primary=addr.ipv4.get("primary", True),
                )
            if addr.ipv6:
                ipv6 = IRBInterfaceIpv6Addresses(
                    ip_prefix=addr.ipv6.get("ip_prefix", ""),
                    primary=addr.ipv6.get("primary", True),
                )
            ip_addrs.append(IRBInterfaceIpAddresses(ipv4_address=ipv4, ipv6_address=ipv6))
    elif irb.ipv4:
        ip_addrs.append(
            IRBInterfaceIpAddresses(
                ipv4_address=IRBInterfaceIpv4Addresses(ip_prefix=irb.ipv4, primary=True)
            )
        )

    # Build optional nested objects
    evpn_adv = None
    if irb.evpn_route_advertisement_type:
        evpn_adv = IRBInterfaceEvpnRouteAdvertisementType(
            **irb.evpn_route_advertisement_type
        ) if isinstance(irb.evpn_route_advertisement_type, dict) else None

    host_pop = None
    if irb.host_route_populate is not None:
        if isinstance(irb.host_route_populate, dict):
            host_pop = IRBInterfaceHostRoutePopulation(**irb.host_route_populate)
        elif isinstance(irb.host_route_populate, bool):
            # Legacy boolean shorthand — map to dynamic/static/evpn
            host_pop = IRBInterfaceHostRoutePopulation(
                dynamic=irb.host_route_populate,
                static=irb.host_route_populate,
                evpn=False,
            )

    spec = IRBInterfaceSpec(
        bridge_domain=irb.bridge_domain,
        router=irb.router,
        ip_mtu=irb.ip_mtu,
        ip_addresses=ip_addrs,
        arp_timeout=irb.arp_timeout,
        l3_proxy_arpnd=IRBInterfaceL3ProxyArpNd(
            proxy_arp=irb.proxy_arp,
            proxy_nd=irb.proxy_nd,
        ),
        learn_unsolicited=irb.learn_unsolicited,
        description=irb.description if irb.description else None,
        evpn_route_advertisement_type=evpn_adv,
        host_route_populate=host_pop,
    )
    return _wrap_cr("services.eda.nokia.com/v1", "IRBInterface", irb.name, ns, spec)


def _cr_vlan(vlan: VlanIntent, ns: str) -> dict:
    """Generate a VLAN CR."""
    spec = VLANSpec(
        bridge_domain=vlan.bridge_domain,
        interface_selector=vlan.interface_selector,
        vlan_id=vlan.vlan_id,
    )
    return _wrap_cr("services.eda.nokia.com/v1", "VLAN", vlan.name, ns, spec)


def _cr_routed_interface(ri: RoutedInterfaceIntent, ns: str) -> dict:
    """Generate a RoutedInterface CR."""
    ipv4_addrs = [
        RoutedInterfaceIpv4Addresses(
            ip_prefix=a.get("ipPrefix", a.get("ip_prefix", "")),
            primary=a.get("primary"),
        )
        for a in ri.ipv4_addresses
    ]
    spec = RoutedInterfaceSpec(
        vlan_id=ri.vlan_id,
        ip_mtu=ri.ip_mtu,
        learn_unsolicited="NONE",
        arp_timeout=ri.arp_timeout,
        interface=ri.interface,
        router=ri.router,
        ipv4_addresses=ipv4_addrs,
    )
    return _wrap_cr("services.eda.nokia.com/v1", "RoutedInterface", ri.name, ns, spec)


def _cr_static_route(sr: StaticRouteIntent, ns: str) -> dict:
    """Generate a StaticRoute CR."""
    spec = StaticRouteSpec(
        nexthop_group=sr.nexthop_group,
        prefixes=sr.prefixes,
        router=sr.router,
        nodes=sr.nodes,
    )
    return _wrap_cr("protocols.eda.nokia.com/v1", "StaticRoute", sr.name, ns, spec)


# ---------------------------------------------------------------------------
# Configlets — still using raw dicts (complex SR Linux JSON config blobs)
# ---------------------------------------------------------------------------


def _cr_configlets(intent: FabricIntent, ns: str) -> list[dict]:
    """
    Generate configlets for design-specific node configuration.

    - BGP EVPN rapid update + rapid withdrawal (all nodes)
    - Node isolation (leafs with LAG members)
    - ESI DF election activation timer (LAG pairs)
    """
    configlets: list[dict] = []

    # BGP rapid update (all nodes)
    spec = ConfigletSpec(
        endpoint_selector=["eda.nokia.com/role=leaf", "eda.nokia.com/role=spine"],
        operating_system="srl",
        priority=100,
        configs=[ConfigletConfigurations(
            path='.network-instance{.name=="default"}.protocols.bgp.afi-safi{.afi-safi-name=="evpn"}.evpn',
            operation="Update",
            config='{\n  "rapid-update": "true"\n}',
        )],
    )
    configlets.append(_wrap_cr("config.eda.nokia.com/v1alpha1", "Configlet", "bgp-evpn-rapid", ns, spec))

    # BGP rapid withdrawal (all nodes)
    spec = ConfigletSpec(
        endpoint_selector=["eda.nokia.com/role=leaf", "eda.nokia.com/role=spine"],
        operating_system="srl",
        priority=100,
        configs=[ConfigletConfigurations(
            path='.network-instance{.name=="default"}.protocols.bgp.route-advertisement',
            operation="Update",
            config='{\n  "rapid-withdrawal": "true"\n}',
        )],
    )
    configlets.append(_wrap_cr("config.eda.nokia.com/v1alpha1", "Configlet", "bgp-rapid-route-withdraw", ns, spec))

    # Node isolation configlets — for each leaf that has LAG members
    leaf_lag_interfaces: dict[str, list[str]] = {}
    for lag in intent.lags:
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
        spec = ConfigletSpec(
            endpoints=[node_name],
            operating_system="srl",
            priority=50,
            configs=[ConfigletConfigurations(path=".system", operation="Create", config=config_json)],
        )
        configlets.append(_wrap_cr(
            "config.eda.nokia.com/v1alpha1", "Configlet",
            f"node-isolation-{node_name}-lag", ns, spec,
        ))

    # ESI DF election activation timer configlets — one per LAG
    for lag in intent.lags:
        endpoints = sorted(set(m.node for m in lag.members))
        spec = ConfigletSpec(
            endpoints=endpoints,
            operating_system="srl",
            priority=100,
            configs=[ConfigletConfigurations(
                path=f'.system.network-instance.protocols.evpn.ethernet-segments.bgp-instance{{.id==1}}.ethernet-segment{{.name=="{lag.name}"}}.df-election.timers',
                operation="Update",
                config='{\n  "activation-timer": 0\n}',
            )],
        )
        configlets.append(_wrap_cr("config.eda.nokia.com/v1alpha1", "Configlet", lag.name, ns, spec))

    return configlets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deduplicate(resources: list[dict]) -> list[dict]:
    """Remove duplicate CRs (same kind + metadata.name)."""
    seen: set[str] = set()
    result: list[dict] = []
    for cr in resources:
        key = f"{cr['kind']}:{cr['metadata']['name']}"
        if key not in seen:
            seen.add(key)
            result.append(cr)
    return result
