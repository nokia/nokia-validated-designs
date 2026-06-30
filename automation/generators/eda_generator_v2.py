"""
EDA CR generator — 26.4.x (services/protocols v2) backend.

EDA 26.4 graduated ``services`` and ``protocols`` to **v2** and most other
groups from ``v1alpha1`` to ``v1``, with breaking spec changes (``vni`` →
``encapOptions.vxlan``, ``nodeSelector`` → ``nodeSelectors``, IRB/RoutedInterface
``ipv4``/``ipv6`` blocks, plural policy arrays, BFD ``*Ms`` units, recased
enums, ...).

This module produces v2-shaped CRs. Builders whose spec shape is unchanged
between releases (NodeUser, NodeProfile, TopoNode, TopoLink, allocation pools,
StaticRoute) are reused verbatim from the v1 generator — their apiVersion is
sourced from the selected :class:`Registry`. The divergent kinds — including
Init (``mgmt`` DHCP fields dropped in bootstrap v1) and Interface (recased
``type``/``encapType`` enums + LACP/multi-homing field renames) — are
reimplemented here against the ``eda_models.eda_26_4`` models.

Entry point: :func:`generate`, dispatched to from
``eda_generator.generate`` when the selected registry's
``generator_variant`` is ``"v2"``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from pathlib import Path

from automation.eda_models.profiles import Registry
from automation.core.models import (
    BannerIntent,
    BridgeDomainIntent,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    FabricConfigInput,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    PolicyStatementIntent,
    PrefixSetIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
    VlanIntent,
)

# Reused, version-neutral helpers + unchanged builders from the v1 generator.
from automation.generators import eda_generator as v1
from automation.generators.clab_generator import _derive_mgmt_subnet

# v2 models (services/protocols v2, fabrics/config/siteinfo/routingpolicies v1).
from automation.eda_models.eda_26_4.services import (
    BridgeDomainSpec,
    BridgeDomainEncapsulationOptions,
    BridgeDomainVxlanOptions,
    BridgeDomainMacLearning,
    BridgeDomainMacDuplicationDetection,
    RouterSpec,
    RouterEncapsulationOptions,
    RouterVxlanOptions,
    IRBInterfaceSpec,
    IRBInterfaceIpAddresses,
    IRBInterfaceIpv4Addresses,
    IRBInterfaceIpv6Addresses,
    IRBInterfaceIpv4SpecificParameters,
    IRBInterfaceL3ProxyArpNd,
    IRBInterfaceEvpnRouteAdvertisementType,
    IRBInterfaceHostRoutePopulation,
    IRBInterfaceDynamic,
    IRBInterfaceStatic,
    IRBInterfaceEvpnLearned,
    VLANSpec,
    RoutedInterfaceSpec,
    RoutedInterfaceIpv4Addresses,
    RoutedInterfaceIpv6Addresses,
    RoutedInterfaceIpv4SpecificParameters,
)
from automation.eda_models.eda_26_4.fabrics import (
    FabricSpec,
    FabricBgp,
    FabricOspf,
    FabricTimers,
    FabricUnderlayProtocol,
    FabricUnderlayProtocolBfd,
    FabricOverlayProtocol,
    FabricOverlayProtocolBfd,
    FabricInterswitchlinks,
    FabricLeafs,
    FabricSpines,
)
from automation.eda_models.eda_26_4.config import (
    ConfigletSpec,
    ConfigletConfigurations,
)
from automation.eda_models.eda_26_4.siteinfo import DefaultMTUSpec, BannerSpec
from automation.eda_models.eda_26_4.bootstrap import InitSpec
from automation.eda_models.eda_26_4.interfaces import (
    InterfaceSpec,
    InterfaceMembers,
    InterfaceLacp,
    InterfaceFallback,
    InterfaceLag,
    InterfaceMultiHoming,
    InterfaceEthernet,
)

logger = logging.getLogger(__name__)


# Intent enum values (v1, lowercase/dashed) → 26.4 interface enums.
_ENCAP_TYPE_V2 = {"dot1q": "Dot1q", "null": "Null"}
_LACP_INTERVAL_V2 = {"fast": "Fast", "slow": "Slow"}
_MH_MODE_V2 = {
    "all-active": "AllActive",
    "port-active": "PortActive",
    "single-active": "SingleActive",
}
_STANDBY_SIGNALING_V2 = {"lacp": "LACP", "power-off": "PowerOff"}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def generate(
    intent: FabricIntent,
    output_dir: Path | None = None,
    registry: Registry | None = None,
) -> list[dict]:
    """Generate all EDA CRs from a FabricIntent for a 26.4.x (v2) target.

    Mirrors :func:`eda_generator.generate`'s ordering exactly; only the
    divergent builders differ.
    """
    if registry is None:  # pragma: no cover - dispatch always passes one
        from automation.eda_models.profiles import get_registry
        registry = get_registry("26.4.2")

    # Rebind v1 module CR_* descriptors so reused v1 builders emit the
    # registry's (v2) apiVersions.
    v1._bind_registry(registry)
    reg = registry

    ns = intent.eda.namespace
    design = intent.design
    resources: list[dict] = []

    # 26.4 renders a *static* mgmt0 address from TopoNode.productionAddress
    # (25.12 used Init mgmt DHCP, which is gone in bootstrap v1). SR Linux
    # requires that address as an ip-prefix (CIDR), so zone the bare mgmt IP
    # with the management subnet's prefix length.
    try:
        _mgmt_prefix = ipaddress.ip_network(
            _derive_mgmt_subnet(intent), strict=False
        ).prefixlen
    except ValueError:
        _mgmt_prefix = 24

    # 1. Init
    resources.append(_cr_init(ns, design, reg))

    # 2. NodeUser
    creds = intent.credentials
    resources.append(v1._cr_node_user(ns, design, creds.username, creds.password))

    # 3. NodeProfile
    node_version = intent.nodes[0].version if intent.nodes else ""
    profile_name = intent.eda.node_profile or f"clab-srlinux-{node_version}"
    if node_version:
        resources.append(
            v1._cr_node_profile(
                ns, profile_name, node_version, design, creds.username, creds.password
            )
        )

    # 4. TopoNodes (productionAddress.ipv4 zoned to a CIDR — see _mgmt_prefix above)
    for node in intent.nodes:
        cr = v1._cr_topo_node(node, profile_name, ns, design)
        pa = cr.get("spec", {}).get("productionAddress") or {}
        ipv4 = pa.get("ipv4")
        if ipv4 and "/" not in ipv4:
            pa["ipv4"] = f"{ipv4}/{_mgmt_prefix}"
        resources.append(cr)

    # 5. ISL interfaces
    seen_isl: set[tuple[str, str]] = set()
    for link in intent.links:
        for node, iface in (
            (link.local_node, link.local_interface),
            (link.remote_node, link.remote_interface),
        ):
            key = (node, iface)
            if key in seen_isl:
                continue
            seen_isl.add(key)
            resources.append(_cr_interface_isl(node, iface, ns, design, reg))

    # 6. Edge interfaces
    for ei in intent.edge_interfaces:
        resources.append(_cr_interface_edge(ei, ns, design, reg))

    # 7. LAG member interfaces
    for lag in intent.lags:
        for member in lag.members:
            resources.append(
                _cr_interface_lag_member(member.node, member.interface, ns, design, reg)
            )

    # 8. LAG interfaces
    for lag in intent.lags:
        resources.append(_cr_interface_lag(lag, ns, design, reg))

    # 9. Links
    for link in intent.links:
        resources.append(v1._cr_topo_link(link, ns, design))

    # 10. ASN allocation pools
    if v1._is_collapsed_spine(intent):
        resources.append(
            v1._cr_index_allocation_pool(
                "collapsed-spine-asn", intent.leaf_asn_start, 20, ns, design
            )
        )
    else:
        resources.append(
            v1._cr_index_allocation_pool("leaf-asn", intent.leaf_asn_start, 20, ns, design)
        )
        resources.append(
            v1._cr_index_allocation_pool("spine-asn", intent.spine_asn, 10, ns, design)
        )

    # 11. IP allocation pool (system0)
    resources.append(
        v1._cr_ip_allocation_pool("system0", intent.system0_prefix, ns, design)
    )

    # 12. Routing policy — PrefixSets + Policies before Fabric.
    for ps in intent.prefix_sets:
        if ps.internal:
            continue
        resources.append(_cr_prefix_set(ps, ns, design, reg))
    for rp in intent.routing_policies:
        if rp.internal:
            continue
        resources.append(_cr_policy(rp, ns, design, reg))

    # 13. Fabric
    resources.append(_cr_fabric(intent, ns, design, reg))

    # 13. Bridge domains
    for bd in intent.bridge_domains:
        resources.append(_cr_bridge_domain(bd, ns, design, reg))

    # 14. Routers
    for router in intent.routers:
        resources.append(_cr_router(router, ns, design, reg))

    # 15. IRB interfaces
    for irb in intent.irb_interfaces:
        resources.append(_cr_irb_interface(irb, ns, design, reg))

    # 16. VLANs
    for vlan in intent.vlans:
        resources.append(_cr_vlan(vlan, ns, design, reg))

    # 17. Routed interfaces
    for ri in intent.routed_interfaces:
        resources.append(_cr_routed_interface(ri, ns, design, reg))

    # 18. Static routes (unchanged spec shape — reuse v1)
    for sr in intent.static_routes:
        resources.append(v1._cr_static_route(sr, ns, design))

    # 19. Configlets
    for cfglet in intent.configlets:
        resources.append(_cr_configlet(cfglet, ns, design, reg))

    # 20. Default MTUs
    for mtu in intent.default_mtus:
        resources.append(_cr_default_mtu(mtu, ns, design, reg))

    # 21. Banners
    for banner in intent.banners:
        resources.append(_cr_banner(banner, ns, design, reg))

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        tx_path = output_dir / "eda_transaction.json"
        with open(tx_path, "w") as f:
            json.dump(resources, f, indent=2)
        logger.info("Wrote %d CRs to %s", len(resources), tx_path)

    return resources


# ---------------------------------------------------------------------------
# Divergent CR builders (v2 shapes)
# ---------------------------------------------------------------------------


def _cr_init(ns: str, design: str, reg: Registry) -> dict:
    """Init CR (v2). ``mgmt.ipv4DHCP``/``ipv6DHCP`` were removed in bootstrap v1."""
    spec = InitSpec(commit_save=True)
    return v1._wrap_cr(reg.INIT.api_version, reg.INIT.kind, "init-base", ns, spec, origin=design)


def _cr_interface_isl(node: str, interface: str, ns: str, design: str, reg: Registry) -> dict:
    """ISL Interface CR (v2). ``type``/``encapType`` enums recased."""
    name = f"{node}-{interface.replace('/', '-')}"
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=interface, node=node)],
        type="Interface",
    )
    return v1._wrap_cr(
        reg.INTERFACE.api_version, reg.INTERFACE.kind, name, ns, spec,
        v1._managed_labels({"eda.nokia.com/role": "interSwitch"}, origin=design),
    )


def _cr_interface_edge(ei: EdgeInterfaceIntent, ns: str, design: str, reg: Registry) -> dict:
    """Edge Interface CR (v2)."""
    labels = v1._managed_labels({**ei.labels, "eda.nokia.com/role": "edge"}, origin=design)
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=ei.interface, node=ei.node)],
        type="Interface",
        encap_type=_ENCAP_TYPE_V2.get(ei.encap),
    )
    return v1._wrap_cr(
        reg.INTERFACE.api_version, reg.INTERFACE.kind, ei.name, ns, spec, labels
    )


def _cr_interface_lag_member(
    node: str, interface: str, ns: str, design: str, reg: Registry
) -> dict:
    """LAG member Interface CR (v2)."""
    name = f"{node}-{interface.replace('/', '-')}"
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        encap_type="Null",
        members=[InterfaceMembers(enabled=True, interface=interface, node=node)],
        type="Interface",
    )
    return v1._wrap_cr(
        reg.INTERFACE.api_version, reg.INTERFACE.kind, name, ns, spec,
        v1._managed_labels({"eda.nokia.com/role": "edge"}, origin=design),
    )


def _cr_interface_lag(lag: LagIntent, ns: str, design: str, reg: Registry) -> dict:
    """LAG Interface CR (v2). Recased enums; LACP/MH/ethernet field renames."""
    labels = v1._managed_labels({**lag.labels, "eda.nokia.com/role": "edge"}, origin=design)

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

    agg_id = lag.members[0].aggregate_id if lag.members else "1"
    admin_key = lag.lacp.admin_key if lag.lacp.admin_key is not None else int(agg_id)

    lacp = InterfaceLacp(
        mode="Active",
        interval=_LACP_INTERVAL_V2.get(lag.lacp.interval, "Fast"),
        system_priority=lag.lacp.system_priority,
        system_mac=lag.lacp.system_id_mac or None,
        admin_key=admin_key,
    )
    if lag.lacp.fallback:
        lacp.lacp_fallback = InterfaceFallback(
            mode="Static",
            timeout_seconds=lag.lacp.fallback.get("timeout", 60),
        )

    lag_config = InterfaceLag(
        type="LACP",
        min_links=lag.min_links,
        lacp=lacp,
        multihoming=InterfaceMultiHoming(
            mode=_MH_MODE_V2.get(lag.multihoming_mode, "AllActive"),
            revertive=lag.revertive,
            preferred_active_node=lag.preferred_active_node or None,
            reload_delay_timer_seconds=lag.reload_delay_timer,
            esi="auto",
        ),
    )

    ethernet = InterfaceEthernet(reload_delay_timer_seconds=lag.reload_delay_timer)
    if lag.multihoming_mode == "port-active" and lag.standby_signaling:
        ethernet.standby_signaling = _STANDBY_SIGNALING_V2.get(lag.standby_signaling)

    spec = InterfaceSpec(
        enabled=True,
        type="LAG",
        encap_type="Dot1q",
        lldp=True,
        ethernet=ethernet,
        members=members,
        lag=lag_config,
    )
    return v1._wrap_cr(
        reg.INTERFACE.api_version, reg.INTERFACE.kind, lag.name, ns, spec, labels
    )


def _cr_bridge_domain(bd: BridgeDomainIntent, ns: str, design: str, reg: Registry) -> dict:
    """BridgeDomain CR (v2).

    ``vni``/pools moved under ``encapOptions.vxlan``; ``macAging`` folded into
    ``macLearning.agingTimeSeconds``; the ``type`` enum recased
    (``SIMPLE`` → ``Simple``).
    """
    mac_dup = None
    if bd.mac_duplication:
        mac_dup = BridgeDomainMacDuplicationDetection(
            enabled=bd.mac_duplication.get("enabled", True),
            hold_down_time_minutes=bd.mac_duplication.get("hold_down_time", 9),
            monitoring_window_minutes=bd.mac_duplication.get("monitoring_window", 3),
            action=bd.mac_duplication.get("action", "StopLearning"),
            num_moves=bd.mac_duplication.get("num_moves", 5),
        )

    mac_learning = BridgeDomainMacLearning(
        enabled=bd.mac_learning,
        aging_time_seconds=bd.mac_aging,
    )

    if bd.type == "SIMPLE":
        spec = BridgeDomainSpec(
            type="Simple",
            encap_options=None,
            mac_learning=mac_learning,
            mac_duplication_detection=mac_dup,
            export_target=bd.export_target,
            import_target=bd.import_target,
        )
    else:
        spec = BridgeDomainSpec(
            type="EVPNVXLAN",
            encap_options=BridgeDomainEncapsulationOptions(
                vxlan=BridgeDomainVxlanOptions(vni=bd.vni)
            ),
            evi=bd.evi,
            mac_learning=mac_learning,
            mac_duplication_detection=mac_dup,
            export_target=bd.export_target,
            import_target=bd.import_target,
        )
    return v1._wrap_cr(
        reg.BRIDGE_DOMAIN.api_version, reg.BRIDGE_DOMAIN.kind, bd.name, ns, spec,
        origin=bd.origin or design,
    )


def _cr_router(router: RouterIntent, ns: str, design: str, reg: Registry) -> dict:
    """Router CR (v2). ``vni`` → ``encapOptions.vxlan``; ``nodeSelector`` → ``nodeSelectors``."""
    spec = RouterSpec(
        type="EVPNVXLAN",
        encap_options=RouterEncapsulationOptions(
            vxlan=RouterVxlanOptions(vni=router.vni)
        ),
        evi=router.evi,
        node_selectors=router.node_selector or None,
        export_target=router.export_target,
        import_target=router.import_target,
    )
    return v1._wrap_cr(
        reg.ROUTER.api_version, reg.ROUTER.kind, router.name, ns, spec, origin=design
    )


def _host_route_population(value: dict | bool | None) -> IRBInterfaceHostRoutePopulation | None:
    """Map the intent's host_route_populate (bool/dict) to the v2 nested shape."""
    if value is None:
        return None
    if isinstance(value, bool):
        return IRBInterfaceHostRoutePopulation(
            dynamic=IRBInterfaceDynamic(populate=value),
            static=IRBInterfaceStatic(populate=value),
            evpn=IRBInterfaceEvpnLearned(populate=False),
        )
    if isinstance(value, dict):
        kwargs: dict = {}
        if "dynamic" in value:
            kwargs["dynamic"] = IRBInterfaceDynamic(populate=bool(value["dynamic"]))
        if "static" in value:
            kwargs["static"] = IRBInterfaceStatic(populate=bool(value["static"]))
        if "evpn" in value:
            kwargs["evpn"] = IRBInterfaceEvpnLearned(populate=bool(value["evpn"]))
        return IRBInterfaceHostRoutePopulation(**kwargs) if kwargs else None
    return None


def _cr_irb_interface(irb: IrbInterfaceIntent, ns: str, design: str, reg: Registry) -> dict:
    """IRBInterface CR (v2).

    ``arpTimeout``/``learnUnsolicited`` moved into the ``ipv4``/``ipv6`` blocks;
    per-address ``anycast`` flag replaces the implicit anycast handling.
    """
    ip_addrs: list[IRBInterfaceIpAddresses] = []
    if irb.ip_addresses:
        for addr in irb.ip_addresses:
            ipv4 = None
            ipv6 = None
            if addr.ipv4:
                ipv4 = IRBInterfaceIpv4Addresses(
                    ip_prefix=addr.ipv4.get("ip_prefix", ""),
                    primary=addr.ipv4.get("primary", True),
                    anycast=irb.anycast_gw,
                )
            if addr.ipv6:
                ipv6 = IRBInterfaceIpv6Addresses(
                    ip_prefix=addr.ipv6.get("ip_prefix", ""),
                    primary=addr.ipv6.get("primary", True),
                    anycast=irb.anycast_gw,
                )
            ip_addrs.append(IRBInterfaceIpAddresses(ipv4_address=ipv4, ipv6_address=ipv6))
    elif irb.ipv4:
        ip_addrs.append(
            IRBInterfaceIpAddresses(
                ipv4_address=IRBInterfaceIpv4Addresses(
                    ip_prefix=irb.ipv4, primary=True, anycast=irb.anycast_gw
                )
            )
        )

    evpn_adv = None
    if isinstance(irb.evpn_route_advertisement_type, dict):
        evpn_adv = IRBInterfaceEvpnRouteAdvertisementType(**irb.evpn_route_advertisement_type)

    ipv4_params = IRBInterfaceIpv4SpecificParameters(arp_timeout_seconds=irb.arp_timeout)

    spec = IRBInterfaceSpec(
        bridge_domain=irb.bridge_domain,
        router=irb.router,
        ip_mtu=irb.ip_mtu,
        ip_addresses=ip_addrs or None,
        ipv4=ipv4_params,
        l3_proxy_arpnd=IRBInterfaceL3ProxyArpNd(
            proxy_arp=irb.proxy_arp,
            proxy_nd=irb.proxy_nd,
        ),
        description=irb.description if irb.description else None,
        evpn_route_advertisement_type=evpn_adv,
        host_route_populate=_host_route_population(irb.host_route_populate),
    )
    return v1._wrap_cr(
        reg.IRB_INTERFACE.api_version, reg.IRB_INTERFACE.kind, irb.name, ns, spec,
        origin=irb.origin or design,
    )


def _cr_vlan(vlan: VlanIntent, ns: str, design: str, reg: Registry) -> dict:
    """VLAN CR (v2). ``interfaceSelector`` → ``interfaceSelectors`` (required)."""
    spec = VLANSpec(
        bridge_domain=vlan.bridge_domain,
        interface_selectors=vlan.interface_selector,
        vlan_id=vlan.vlan_id,
    )
    return v1._wrap_cr(reg.VLAN.api_version, reg.VLAN.kind, vlan.name, ns, spec, origin=design)


def _cr_routed_interface(
    ri: RoutedInterfaceIntent, ns: str, design: str, reg: Registry
) -> dict:
    """RoutedInterface CR (v2). ``arpTimeout``/``learnUnsolicited`` → ``ipv4`` block."""
    ipv4_addrs = [
        RoutedInterfaceIpv4Addresses(
            ip_prefix=a.get("ipPrefix", a.get("ip_prefix", "")),
            primary=a.get("primary"),
        )
        for a in ri.ipv4_addresses
    ]
    ipv6_addrs = [
        RoutedInterfaceIpv6Addresses(
            ip_prefix=a.get("ipPrefix", a.get("ip_prefix", "")),
            primary=a.get("primary"),
        )
        for a in ri.ipv6_addresses
    ]
    spec = RoutedInterfaceSpec(
        vlan_id=ri.vlan_id,
        ip_mtu=ri.ip_mtu,
        interface=ri.interface,
        router=ri.router,
        ipv4=RoutedInterfaceIpv4SpecificParameters(
            arp_timeout_seconds=ri.arp_timeout,
            learn_unsolicited_arp="Disabled",
        ),
        ipv4_addresses=ipv4_addrs or None,
        ipv6_addresses=ipv6_addrs or None,
    )
    return v1._wrap_cr(
        reg.ROUTED_INTERFACE.api_version, reg.ROUTED_INTERFACE.kind, ri.name, ns, spec,
        origin=design,
    )


def _cr_configlet(cfglet: ConfigletIntent, ns: str, design: str, reg: Registry) -> dict:
    """Configlet CR (v2). ``endpointSelector`` → ``endpointSelectors``."""
    spec = ConfigletSpec(
        endpoint_selectors=cfglet.endpoint_selector or None,
        endpoints=cfglet.endpoints or None,
        operating_system=cfglet.operating_system,
        priority=cfglet.priority,
        configs=[
            ConfigletConfigurations(
                path=c.path,
                operation=c.operation,
                config=c.config,
            )
            for c in cfglet.configs
        ],
    )
    return v1._wrap_cr(
        reg.CONFIGLET.api_version, reg.CONFIGLET.kind, cfglet.name, ns, spec,
        origin=cfglet.origin or design,
    )


def _cr_default_mtu(mtu: DefaultMtuIntent, ns: str, design: str, reg: Registry) -> dict:
    """DefaultMTU CR (v2). ``nodeSelector`` → ``nodeSelectors``; ``layer2SubifMTU`` → ``layer2SubinterfaceMTU``."""
    spec = DefaultMTUSpec(
        interface_mtu=mtu.interface_mtu,
        layer2_subinterface_mtu=mtu.layer2_subif_mtu,
        layer3_mtu=mtu.layer3_mtu,
        node_selectors=mtu.node_selector or None,
        nodes=mtu.nodes or None,
    )
    return v1._wrap_cr(
        reg.DEFAULT_MTU.api_version, reg.DEFAULT_MTU.kind, mtu.name, ns, spec, origin=design
    )


def _cr_banner(banner: BannerIntent, ns: str, design: str, reg: Registry) -> dict:
    """Banner CR (v2). ``nodeSelector`` → ``nodeSelectors``."""
    spec = BannerSpec(
        login_banner=banner.login_banner or None,
        motd=banner.motd or None,
        node_selectors=banner.node_selector or None,
        nodes=banner.nodes or None,
    )
    return v1._wrap_cr(
        reg.BANNER.api_version, reg.BANNER.kind, banner.name, ns, spec, origin=design
    )


# ---------------------------------------------------------------------------
# Routing-policy CR builders (v2 raw specs)
# ---------------------------------------------------------------------------

# Intent (lowercase) → v2 Policy match protocol enum (mixed case).
_PROTOCOL_TO_EDA_V2 = {
    "local": "Local",
    "bgp": "BGP",
    "aggregate": "Aggregate",
    "bgp_evpn": "BGP_EVPN",
    "static": "Static",
}

# Intent action result → v2 policyResult enum.
_RESULT_TO_EDA_V2 = {"accept": "Accept", "reject": "Reject"}


def _cr_prefix_set(ps: PrefixSetIntent, ns: str, design: str, reg: Registry) -> dict:
    """PrefixSet CR (v2). Top-level ``prefix`` → ``prefixes``."""
    spec = {"prefixes": v1._prefix_set_entries(ps)}
    return v1._wrap_cr_raw(
        reg.PREFIX_SET.api_version, reg.PREFIX_SET.kind, ps.name, ns, spec, origin=design
    )


def _render_policy_statement_v2(stmt: PolicyStatementIntent) -> dict:
    """Render one statement into a v2 Policy.statements[] dict."""
    match: dict = {}
    if stmt.match.prefix_set:
        match["prefixSet"] = stmt.match.prefix_set
    if stmt.match.protocol:
        match["protocol"] = _PROTOCOL_TO_EDA_V2.get(
            stmt.match.protocol, stmt.match.protocol
        )
    if stmt.match.bgp_evpn_route_types:
        match["bgp"] = {"evpnRouteTypes": list(stmt.match.bgp_evpn_route_types)}

    action: dict = {"policyResult": _RESULT_TO_EDA_V2.get(stmt.action.result, "Accept")}
    if stmt.action.set_local_preference is not None:
        action["bgp"] = {"setLocalPreference": stmt.action.set_local_preference}

    out: dict = {"name": stmt.name}
    if match:
        out["match"] = match
    out["action"] = action
    return out


def _cr_policy(rp: RoutingPolicyIntent, ns: str, design: str, reg: Registry) -> dict:
    """Policy CR (v2). ``statement`` → ``statements``; recased result/protocol enums."""
    spec: dict = {
        "defaultAction": {
            "policyResult": _RESULT_TO_EDA_V2.get(rp.default_action, "Reject")
        },
        "statements": [_render_policy_statement_v2(s) for s in rp.statements],
    }
    return v1._wrap_cr_raw(
        reg.POLICY.api_version, reg.POLICY.kind, rp.name, ns, spec, origin=design
    )


# ---------------------------------------------------------------------------
# Fabric CR builder (v2)
# ---------------------------------------------------------------------------


def _us_to_ms(value: int | None) -> int | None:
    """Convert a microsecond BFD interval (v1 intent) to milliseconds (v2)."""
    if value is None:
        return None
    return max(1, value // 1000)


def _bgp_timers_v2(timers) -> FabricTimers | None:
    if timers is None:
        return None
    payload: dict = {}
    if timers.connect_retry is not None:
        payload["connect_retry_seconds"] = timers.connect_retry
    if timers.hold_time is not None:
        payload["hold_time_seconds"] = timers.hold_time
    if timers.keep_alive is not None:
        payload["keep_alive_seconds"] = timers.keep_alive
    if timers.minimum_advertisement_interval is not None:
        payload["minimum_advertisement_interval_seconds"] = (
            timers.minimum_advertisement_interval
        )
    return FabricTimers(**payload) if payload else None


def _underlay_bfd_v2(bfd) -> FabricUnderlayProtocolBfd | None:
    if bfd is None:
        return None
    return FabricUnderlayProtocolBfd(
        enabled=bfd.enabled,
        desired_min_transmit_int_ms=_us_to_ms(bfd.desired_min_transmit_int),
        required_min_receive_int_ms=_us_to_ms(bfd.required_min_receive),
        detection_multiplier=bfd.detection_multiplier,
        required_min_echo_receive_int_ms=_us_to_ms(bfd.min_echo_receive_interval),
        ttl=bfd.ttl,
    )


def _overlay_bfd_v2(bfd) -> FabricOverlayProtocolBfd | None:
    if bfd is None:
        return None
    return FabricOverlayProtocolBfd(
        enabled=bfd.enabled,
        desired_min_transmit_int_ms=_us_to_ms(bfd.desired_min_transmit_int),
        required_min_receive_int_ms=_us_to_ms(bfd.required_min_receive),
        detection_multiplier=bfd.detection_multiplier,
        required_min_echo_receive_int_ms=_us_to_ms(bfd.min_echo_receive_interval),
        ttl=bfd.ttl,
    )


def _build_underlay_protocol_v2(
    cfg: FabricConfigInput | None,
    export_policy: list[str],
    import_policy: list[str],
) -> FabricUnderlayProtocol:
    if cfg is None:
        return FabricUnderlayProtocol(
            protocols=["EBGP"],
            bgp=FabricBgp(
                asn_pool="asn-pool",
                export_policies=export_policy or None,
                import_policies=import_policy or None,
            ),
            bfd=FabricUnderlayProtocolBfd(enabled=True),
        )

    up = cfg.underlay_protocol
    bgp_obj: FabricBgp | None = None
    if "EBGP" in up.protocol:
        bgp_in = up.bgp or None
        merged_export = list(export_policy or [])
        merged_import = list(import_policy or [])
        if bgp_in is not None:
            for n in bgp_in.export_policy:
                if n not in merged_export:
                    merged_export.append(n)
            for n in bgp_in.import_policy:
                if n not in merged_import:
                    merged_import.append(n)
        bgp_obj = FabricBgp(
            asn_pool=(bgp_in.asn_pool if bgp_in else None) or "asn-pool",
            export_policies=merged_export or None,
            import_policies=merged_import or None,
            keychain=bgp_in.keychain if bgp_in else None,
            timers=_bgp_timers_v2(bgp_in.timers if bgp_in else None),
        )

    ospf_obj: FabricOspf | None = None
    if up.ospf is not None and ("OSPFv2" in up.protocol or "OSPFv3" in up.protocol):
        ospf_obj = FabricOspf(address_families=up.ospf.address_family or None)

    return FabricUnderlayProtocol(
        protocols=list(up.protocol),
        bgp=bgp_obj,
        ospf=ospf_obj,
        bfd=_underlay_bfd_v2(up.bfd),
    )


def _build_overlay_protocol_v2(
    cfg: FabricConfigInput | None,
    leaf_selector: list[str],
    spine_selector: list[str],
) -> FabricOverlayProtocol:
    if cfg is None:
        return FabricOverlayProtocol(protocol="EBGP")

    op = cfg.overlay_protocol
    bgp_obj: FabricBgp | None = None
    if op.protocol == "IBGP":
        b = op.bgp
        bgp_obj = FabricBgp(
            autonomous_system=b.autonomous_system,
            cluster_id=b.cluster_id,
            export_policies=b.export_policy or None,
            import_policies=b.import_policy or None,
            keychain=b.keychain,
            rr_node_selectors=b.rr_node_selector or spine_selector or None,
            rr_client_node_selectors=b.rr_client_node_selector or leaf_selector or None,
            rr_ip_addresses=b.rr_ip_addresses or None,
            timers=_bgp_timers_v2(b.timers),
        )
    elif op.bgp is not None:
        b = op.bgp
        bgp_obj = FabricBgp(
            keychain=b.keychain,
            timers=_bgp_timers_v2(b.timers),
            export_policies=b.export_policy or None,
            import_policies=b.import_policy or None,
        )

    return FabricOverlayProtocol(
        protocol=op.protocol,
        bgp=bgp_obj,
        bfd=_overlay_bfd_v2(op.bfd),
    )


def _build_inter_switch_links_v2(cfg: FabricConfigInput | None) -> FabricInterswitchlinks:
    link_selectors = ["eda.nokia.com/role=interSwitch"]
    if cfg is None:
        return FabricInterswitchlinks(link_selectors=link_selectors, unnumbered="IPv6")
    isl = cfg.inter_switch_links
    # v1 intent uses the 'IPV6' enum literal; v2 expects 'IPv6'.
    unnumbered = "IPv6" if isl.unnumbered else None
    return FabricInterswitchlinks(
        link_selectors=link_selectors,
        unnumbered=unnumbered,
        pool_i_pv4=isl.pool_ipv4,
        pool_i_pv6=isl.pool_ipv6,
        ip_mtu=isl.ip_mtu,
        vlan_id=isl.vlan_id,
    )


def _cr_fabric(intent: FabricIntent, ns: str, design: str, reg: Registry) -> dict:
    """Fabric CR (v2)."""
    leaf_selector: list[str] = []
    spine_selector: list[str] = []
    for node in intent.nodes:
        if node.role == "tor":
            continue
        role_label = node.labels.get("eda.nokia.com/role", node.role)
        sel = f"eda.nokia.com/role={role_label}"
        if node.role == "leaf":
            if sel not in leaf_selector:
                leaf_selector.append(sel)
        elif node.role == "spine":
            if sel not in spine_selector:
                spine_selector.append(sel)

    internal_policy_names = {rp.name for rp in intent.routing_policies if rp.internal}
    export_policy = [
        n for n in intent.fabric_export_policies if n not in internal_policy_names
    ]
    import_policy = [
        n for n in intent.fabric_import_policies if n not in internal_policy_names
    ]

    collapsed = v1._is_collapsed_spine(intent)
    leaf_asn_pool = "collapsed-spine-asn" if collapsed else "leaf-asn"

    cfg = intent.fabric_config
    underlay = _build_underlay_protocol_v2(cfg, export_policy, import_policy)
    overlay = _build_overlay_protocol_v2(cfg, leaf_selector, spine_selector)
    isl = _build_inter_switch_links_v2(cfg)

    fabric_kwargs: dict = dict(
        underlay_protocol=underlay,
        overlay_protocol=overlay,
        system_pool_i_pv4="system0",
        inter_switch_links=isl,
        leafs=FabricLeafs(asn_pool=leaf_asn_pool, leaf_node_selectors=leaf_selector),
    )
    if spine_selector or not collapsed:
        fabric_kwargs["spines"] = FabricSpines(
            asn_pool="spine-asn", spine_node_selectors=spine_selector
        )

    spec = FabricSpec(**fabric_kwargs)
    return v1._wrap_cr(
        reg.FABRIC.api_version, reg.FABRIC.kind, intent.fabric_name, ns, spec, origin=design
    )
