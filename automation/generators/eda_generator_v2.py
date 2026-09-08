"""
EDA CR generator — 26.4.x / 26.8.x (services/protocols v2) backend.

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

26.8 shares this backend: its fabric-path spec shapes are a strict superset of
26.4's (additive fields and widened enums only, verified by diffing the 26.4
OpenAPI specs against a fresh 26.8.1 cluster's CRDs), so it reuses the same
``eda_models.eda_26_4`` models. Where 26.8 does diverge is the AI-fabric kinds:
all four of their groups graduated and ``Backend`` carries breaking renames, so
those builders work against ``eda_models.eda_26_8`` and are gated on the
profile's ``supports_ai_fabrics`` — 26.4 still refuses them.

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
    AiBackendIntent,
    BannerIntent,
    BridgeDomainIntent,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    FabricConfigInput,
    FabricDefinitionIntent,
    FabricIntent,
    ForwardingClassIntent,
    IrbInterfaceIntent,
    LagIntent,
    NamespaceIntent,
    NodeGroupIntent,
    PolicyStatementIntent,
    PrefixSetIntent,
    QueueIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
    TopologyGroupingIntent,
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

# AI-fabric models. These groups all graduated in 26.8 (aifabrics v1alpha1 -> v1,
# qos v1 -> v2, aaa v1alpha1 -> v1, topologies v1alpha1 -> v1) with breaking
# renames on Backend, so they live in their own package rather than being reused
# from eda_26_4 like the fabric-path models above. Only reachable from a profile
# whose ``supports_ai_fabrics`` is set, which today means 26.8.
from automation.eda_models.eda_26_8.aifabrics import (
    BackendSpec,
    BackendStripes,
    BackendStripeConnector,
    BackendRocev2Qos,
    BackendGpuIsolationGroups,
)
from automation.eda_models.eda_26_8.qos import QueueSpec
from automation.eda_models.eda_26_8.aaa import NodeGroupSpec
from automation.eda_models.eda_26_8.topologies import (
    TopologyGroupingSpec,
    TopologyGroupingGroupSelectors,
    TopologyGroupingTierSelectors,
)
from automation.eda_models.eda_26_8.core import NamespaceSpec

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

    # Multi-fabric / multi-namespace designs hang off the AI-fabric kinds, whose
    # spec shapes were never read off a live 26.4 cluster. Rather than silently
    # flatten such an intent into one namespace (which would deploy a
    # wrong-but-plausible fabric), refuse it on releases whose profile does not
    # claim AI-fabric support. 26.8 does claim it — its shapes are verified.
    if not reg.supports_ai_fabrics:
        unsupported: list[str] = []
        if intent.ai_backends:
            unsupported.append("ai_backends")
        if intent.fabrics:
            unsupported.append("fabrics")
        if len(intent.namespaces_in_use()) > 1:
            unsupported.append("multiple namespaces")
        if unsupported:
            raise ValueError(
                f"Design '{intent.design}' uses {', '.join(unsupported)}, which "
                f"the EDA {reg.eda_version} (v2) CR backend does not support. "
                f"Target a 25.12 or 26.8 cluster, or pass --eda-version 25.12 "
                f"to generate 25.12 CRs."
            )

    design = intent.design
    resources: list[dict] = []

    # Namespace resolution, mirroring the v1 generator: ``intent.ns_of()`` falls
    # back to the intent default for any resource that does not pin a namespace,
    # so single-fabric designs behave exactly as they did when this backend read
    # one ``ns`` up front.
    ns = intent.eda.namespace
    _ns = intent.ns_of
    # Every namespace that needs the shared bootstrap resources (Init, NodeUser,
    # NodeProfile). Derived from where the *nodes* live rather than from
    # namespaces_in_use(): a namespace holding only eda-system-scoped resources
    # has nothing to onboard.
    node_namespaces: list[str] = []
    for node in intent.nodes:
        node_ns = _ns(node)
        if node_ns not in node_namespaces:
            node_namespaces.append(node_ns)
    if not node_namespaces:
        node_namespaces = [ns]

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

    # 0. Namespaces themselves (created in eda-system, before anything in them)
    for nsi in intent.namespaces:
        resources.append(_cr_namespace(nsi, design, reg))

    # 0b. TopologyGrouping (topology view; independent of fabric resources)
    for grouping in intent.topology_groupings:
        resources.append(_cr_topology_grouping(grouping, design, reg))

    # 1. Init — one per namespace that owns nodes
    for node_ns in node_namespaces:
        resources.append(_cr_init(node_ns, design, reg))

    # 1b. NodeGroups — must precede NodeUser, whose groupBindings reference them
    for group in intent.node_groups:
        resources.append(_cr_node_group(group, _ns(group), design, reg))

    # 2. NodeUser — one per namespace that owns nodes
    creds = intent.credentials
    for node_ns in node_namespaces:
        resources.append(
            v1._cr_node_user(node_ns, design, creds.username, creds.password)
        )

    # 3. NodeProfile — derived from node version, one per namespace
    node_version = intent.nodes[0].version if intent.nodes else ""
    profile_name = intent.eda.node_profile or v1.default_node_profile_name(
        intent.environment, node_version
    )
    if node_version:
        for node_ns in node_namespaces:
            resources.append(
                v1._cr_node_profile(
                    node_ns,
                    profile_name,
                    node_version,
                    design,
                    intent.environment,
                    creds.username,
                    creds.password,
                )
            )

    # 4. TopoNodes (productionAddress.ipv4 zoned to a CIDR — see _mgmt_prefix above)
    for node in intent.nodes:
        cr = v1._cr_topo_node(node, profile_name, _ns(node), design)
        pa = cr.get("spec", {}).get("productionAddress") or {}
        ipv4 = pa.get("ipv4")
        if ipv4 and "/" not in ipv4:
            pa["ipv4"] = f"{ipv4}/{_mgmt_prefix}"
        resources.append(cr)

    # 5. ISL interfaces. Keyed by namespace too: the same node name in two
    # namespaces is two nodes.
    seen_isl: set[tuple[str, str, str]] = set()
    for link in intent.links:
        link_ns = _ns(link)
        for node, iface in (
            (link.local_node, link.local_interface),
            (link.remote_node, link.remote_interface),
        ):
            key = (link_ns, node, iface)
            if key in seen_isl:
                continue
            seen_isl.add(key)
            resources.append(_cr_interface_isl(node, iface, link_ns, design, reg))

    # 6. Edge interfaces
    for ei in intent.edge_interfaces:
        resources.append(_cr_interface_edge(ei, _ns(ei), design, reg))

    # 7. LAG member interfaces
    for lag in intent.lags:
        for member in lag.members:
            resources.append(
                _cr_interface_lag_member(
                    member.node, member.interface, _ns(lag), design, reg
                )
            )

    # 8. LAG interfaces
    for lag in intent.lags:
        resources.append(_cr_interface_lag(lag, _ns(lag), design, reg))

    # 9. Links
    for link in intent.links:
        resources.append(v1._cr_topo_link(link, _ns(link), design))

    # 10-11. Allocation pools. A design that declares its own pools (any
    # multi-fabric design must, since each fabric needs its own) owns them
    # outright; otherwise they are synthesized from the intent's ASN/prefix
    # scalars for the single fabric.
    if intent.index_pools or intent.ip_pools:
        for ipool in intent.index_pools:
            resources.append(v1._cr_index_pool(ipool, _ns(ipool), design))
        for ippool in intent.ip_pools:
            resources.append(v1._cr_ip_pool(ippool, _ns(ippool), design))
    else:
        if v1._is_collapsed_spine(intent):
            resources.append(
                v1._cr_index_allocation_pool(
                    "collapsed-spine-asn", intent.leaf_asn_start, 20, ns, design
                )
            )
        else:
            resources.append(
                v1._cr_index_allocation_pool(
                    "leaf-asn", intent.leaf_asn_start, 20, ns, design
                )
            )
            resources.append(
                v1._cr_index_allocation_pool("spine-asn", intent.spine_asn, 10, ns, design)
            )
        resources.append(
            v1._cr_ip_allocation_pool("system0", intent.system0_prefix, ns, design)
        )

    # 11a. Seed the pools EDA would have installed into any namespace the design
    # creates itself. A design's own declaration always wins.
    declared_pools = {(_ns(p), p.name) for p in intent.index_pools}
    for nsi in intent.namespaces:
        for pool_name, (start, size) in v1._EDA_DEFAULT_INDEX_POOLS.items():
            if (nsi.name, pool_name) in declared_pools:
                continue
            resources.append(
                v1._cr_index_allocation_pool(pool_name, start, size, nsi.name, design)
            )

    # 11b. QoS scaffolding — Queues and ForwardingClasses that the AI Backend's
    # RoCEv2 policies bind to, so they must exist before it.
    for fc in intent.forwarding_classes:
        resources.append(_cr_forwarding_class(fc, _ns(fc), design, reg))
    for queue in intent.queues:
        resources.append(_cr_queue(queue, _ns(queue), design, reg))

    # 12. Routing policy — PrefixSets + Policies before Fabric.
    for ps in intent.prefix_sets:
        if ps.internal:
            continue
        resources.append(_cr_prefix_set(ps, _ns(ps), design, reg))
    for rp in intent.routing_policies:
        if rp.internal:
            continue
        resources.append(_cr_policy(rp, _ns(rp), design, reg))

    # 13. Fabrics. Explicit definitions win; otherwise one Fabric is
    # synthesized from the intent's node roles (the single-fabric path).
    if intent.fabrics:
        for fabric in intent.fabrics:
            resources.append(_cr_fabric_definition(fabric, _ns(fabric), design, reg))
    else:
        resources.append(_cr_fabric(intent, ns, design, reg))

    # 13b. AI backend fabrics
    for backend in intent.ai_backends:
        resources.append(_cr_ai_backend(backend, _ns(backend), design, reg))

    # 13c. Bridge domains
    for bd in intent.bridge_domains:
        resources.append(_cr_bridge_domain(bd, _ns(bd), design, reg))

    # 14. Routers
    for router in intent.routers:
        resources.append(_cr_router(router, _ns(router), design, reg))

    # 15. IRB interfaces
    for irb in intent.irb_interfaces:
        resources.append(_cr_irb_interface(irb, _ns(irb), design, reg))

    # 16. VLANs
    for vlan in intent.vlans:
        resources.append(_cr_vlan(vlan, _ns(vlan), design, reg))

    # 17. Routed interfaces
    for ri in intent.routed_interfaces:
        resources.append(_cr_routed_interface(ri, _ns(ri), design, reg))

    # 18. Static routes (unchanged spec shape — reuse v1)
    for sr in intent.static_routes:
        resources.append(v1._cr_static_route(sr, _ns(sr), design))

    # 19. Configlets — dynamic load balancing first, matching the order the
    #     design used to emit it in.
    for backend in intent.ai_backends:
        ns = _ns(backend)
        for cfglet in v1.dlb_configlet_intents(backend, ns, design):
            resources.append(_cr_configlet(cfglet, ns, design, reg))
    for cfglet in intent.configlets:
        resources.append(_cr_configlet(cfglet, _ns(cfglet), design, reg))

    # 20. Default MTUs
    for mtu in intent.default_mtus:
        resources.append(_cr_default_mtu(mtu, _ns(mtu), design, reg))

    # 21. Banners
    for banner in intent.banners:
        resources.append(_cr_banner(banner, _ns(banner), design, reg))

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


def _cr_fabric_definition(
    fabric: FabricDefinitionIntent, ns: str, design: str, reg: Registry
) -> dict:
    """Fabric CR from an explicit fabric definition (v2).

    The counterpart to :func:`_cr_fabric`, which infers a single Fabric from the
    intent's node roles. Here the selectors and pools are stated outright,
    because a multi-fabric intent cannot infer which nodes belong to which
    fabric from roles alone — several fabrics each have leaves and spines.
    """
    cfg = fabric.fabric_config
    underlay = _build_underlay_protocol_v2(cfg, [], [])
    overlay = _build_overlay_protocol_v2(
        cfg, fabric.leaf_node_selector, fabric.spine_node_selector
    )
    isl = _build_inter_switch_links_v2(cfg)
    if fabric.inter_switch_link_selector:
        isl.link_selectors = list(fabric.inter_switch_link_selector)

    if underlay.bgp is not None and fabric.leaf_asn_pool:
        underlay.bgp.asn_pool = fabric.leaf_asn_pool

    spec = FabricSpec(
        system_pool_i_pv4=fabric.system_pool_ipv4 or None,
        leafs=FabricLeafs(
            leaf_node_selectors=list(fabric.leaf_node_selector) or None,
            asn_pool=fabric.leaf_asn_pool or None,
        ),
        spines=(
            FabricSpines(
                spine_node_selectors=list(fabric.spine_node_selector),
                asn_pool=fabric.spine_asn_pool or None,
            )
            if fabric.spine_node_selector
            else None
        ),
        inter_switch_links=isl,
        underlay_protocol=underlay,
        overlay_protocol=overlay,
    )
    return v1._wrap_cr(
        reg.FABRIC.api_version, reg.FABRIC.kind, fabric.name, ns, spec, origin=design
    )


# ---------------------------------------------------------------------------
# AI-fabric kinds (26.8: aifabrics v1, qos v2, aaa v1, topologies v1)
# ---------------------------------------------------------------------------


def _cr_namespace(nsi: NamespaceIntent, design: str, reg: Registry) -> dict:
    """Namespace CR. Spec shape is unchanged from 25.12.

    The CR itself lives in ``eda-system`` (or whatever ``parent_namespace``
    says); ``metadata.name`` is the namespace being created.
    """
    spec = NamespaceSpec(description=nsi.description or None)
    return v1._wrap_cr(
        reg.NAMESPACE.api_version, reg.NAMESPACE.kind, nsi.name,
        nsi.parent_namespace, spec, origin=design,
    )


def _cr_node_group(
    group: NodeGroupIntent, ns: str, design: str, reg: Registry
) -> dict:
    """AAA NodeGroup CR. Spec shape is unchanged from 25.12; only the group
    version moved (``aaa`` v1alpha1 -> v1)."""
    spec = NodeGroupSpec(
        services=list(group.services),
        superuser=group.superuser,
    )
    return v1._wrap_cr(
        reg.NODE_GROUP.api_version, reg.NODE_GROUP.kind, group.name, ns, spec,
        origin=design,
    )


def _cr_topology_grouping(
    grouping: TopologyGroupingIntent, design: str, reg: Registry
) -> dict:
    """TopologyGrouping CR (the fabric's topology view).

    ``groupSelectors[].groupUIName`` is new in 26.8 and *required*. The intent
    carries no per-group UI name, so it falls back to the group key — which is
    what the field's own documentation says the UI would display anyway.
    """
    spec = TopologyGroupingSpec(
        group_selectors=[
            TopologyGroupingGroupSelectors(
                group=gs.group,
                group_ui_name=gs.group,
                node_selector=list(gs.node_selector) or None,
            )
            for gs in grouping.group_selectors
        ] or None,
        tier_selectors=[
            TopologyGroupingTierSelectors(
                tier=ts.tier,
                node_selector=list(ts.node_selector) or None,
            )
            for ts in grouping.tier_selectors
        ] or None,
        ui_name=grouping.ui_name or grouping.name,
        ui_description=grouping.ui_description or None,
    )
    return v1._wrap_cr(
        reg.TOPOLOGY_GROUPING.api_version, reg.TOPOLOGY_GROUPING.kind,
        grouping.name, grouping.namespace, spec, origin=design,
    )


def _cr_queue(queue: QueueIntent, ns: str, design: str, reg: Registry) -> dict:
    """QoS Queue CR (v2). The ``queueType`` enum was recased ``Pfc`` -> ``PFC``."""
    spec = QueueSpec(
        queue_id=queue.queue_id,
        queue_type="PFC" if queue.queue_type == "Pfc" else queue.queue_type,
        traffic_type=queue.traffic_type,
    )
    return v1._wrap_cr(
        reg.QUEUE.api_version, reg.QUEUE.kind, queue.name, ns, spec, origin=design
    )


def _cr_forwarding_class(
    fc: ForwardingClassIntent, ns: str, design: str, reg: Registry
) -> dict:
    """QoS ForwardingClass CR (v2).

    The spec is still an empty object on qos v2 — the resource is purely its
    name — so this is built from a raw ``{}`` rather than a typed model.
    """
    return v1._wrap_cr_raw(
        reg.FORWARDING_CLASS.api_version, reg.FORWARDING_CLASS.kind, fc.name, ns,
        {}, origin=design,
    )


def _cr_ai_backend(
    backend: AiBackendIntent, ns: str, design: str, reg: Registry
) -> dict:
    """aifabrics Backend CR (v1) — a rail-optimized AI fabric.

    26.8 renamed a number of fields relative to the 25.12 v1alpha1 shape:
    ``systemPoolIPV4`` -> ``systemPoolIPv4`` (on the spec, stripes and the
    stripe connector), the singular ``nodeSelector``/``linkSelector``/
    ``interfaceSelector`` -> plural, ``gpuVlan`` -> ``gpuVLAN``, and the RoCEv2
    timers/burst size gained explicit units (``pfcDeadlock*Timer`` -> ``*TimerMs``,
    ``queueMaximumBurstSize`` -> ``queueMaximumBurstSizeBytes``). The intent
    field names are unchanged; only the aliases on the 26.8 models differ.

    ``dynamicLoadBalancing`` is new in 26.8 but deliberately left unset —
    see ``dlb_configlet_intents`` for why the configlet rendering is kept.
    ``addressAllocation`` is left unset so EDA applies its own defaults, and
    ``type`` is omitted because its ``overlay`` sub-field is mandatory once the
    block is present — the intent has nothing to say about either yet.
    """
    qos = backend.rocev2_qos
    connector = backend.stripe_connector
    spec = BackendSpec(
        system_pool_i_pv4=backend.system_pool_ipv4 or None,
        asn_pool=backend.asn_pool or None,
        ip_mtu=backend.ip_mtu,
        address_allocation=None,
        dynamic_load_balancing=None,
        type=None,
        stripes=[
            BackendStripes(
                name=s.name,
                stripe_id=s.stripe_id,
                gpu_vlan=s.gpu_vlan,
                node_selectors=list(s.node_selector),
                asn_pool=s.asn_pool or None,
                system_pool_i_pv4=s.system_pool_ipv4 or None,
            )
            for s in backend.stripes
        ],
        gpu_isolation_groups=[
            BackendGpuIsolationGroups(
                name=g.name,
                interface_selectors=list(g.interface_selector),
            )
            for g in backend.gpu_isolation_groups
        ],
        stripe_connector=(
            BackendStripeConnector(
                name=connector.name,
                node_selectors=list(connector.node_selector),
                link_selectors=list(connector.link_selector),
                asn_pool=connector.asn_pool or None,
                system_pool_i_pv4=connector.system_pool_ipv4 or None,
            )
            if connector is not None
            else None
        ),
        rocev2_qo_s=BackendRocev2Qos(
            ecn_max_drop_probability_percent=qos.ecn_max_drop_probability_percent,
            ecn_slope_max_threshold_percent=qos.ecn_slope_max_threshold_percent,
            ecn_slope_min_threshold_percent=qos.ecn_slope_min_threshold_percent,
            pfc_deadlock_detection_timer_ms=qos.pfc_deadlock_detection_timer,
            pfc_deadlock_recovery_timer_ms=qos.pfc_deadlock_recovery_timer,
            queue_maximum_burst_size_bytes=qos.queue_maximum_burst_size,
        ),
    )
    return v1._wrap_cr(
        reg.AI_BACKEND.api_version, reg.AI_BACKEND.kind, backend.name, ns, spec,
        origin=design,
    )
