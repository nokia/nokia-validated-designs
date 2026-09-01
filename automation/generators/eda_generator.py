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
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from automation.eda_models.registry import (
    INIT as CR_INIT,
    NODE_USER as CR_NODE_USER,
    NODE_PROFILE as CR_NODE_PROFILE,
    TOPO_NODE as CR_TOPO_NODE,
    TOPO_LINK as CR_TOPO_LINK,
    INDEX_ALLOCATION_POOL as CR_INDEX_ALLOCATION_POOL,
    IP_ALLOCATION_POOL as CR_IP_ALLOCATION_POOL,
    INTERFACE as CR_INTERFACE,
    FABRIC as CR_FABRIC,
    BRIDGE_DOMAIN as CR_BRIDGE_DOMAIN,
    ROUTER as CR_ROUTER,
    IRB_INTERFACE as CR_IRB_INTERFACE,
    VLAN as CR_VLAN,
    ROUTED_INTERFACE as CR_ROUTED_INTERFACE,
    STATIC_ROUTE as CR_STATIC_ROUTE,
    CONFIGLET as CR_CONFIGLET,
    DEFAULT_MTU as CR_DEFAULT_MTU,
    BANNER as CR_BANNER,
    POLICY as CR_POLICY,
    PREFIX_SET as CR_PREFIX_SET,
    NAMESPACE as CR_NAMESPACE,
    NODE_GROUP as CR_NODE_GROUP,
    TOPOLOGY_GROUPING as CR_TOPOLOGY_GROUPING,
    QUEUE as CR_QUEUE,
    FORWARDING_CLASS as CR_FORWARDING_CLASS,
    AI_BACKEND as CR_AI_BACKEND,
)
from automation.eda_models.profiles import Registry, get_default_registry
from automation.core.models import (
    AiBackendIntent,
    BannerIntent,
    BridgeDomainIntent,
    BreakoutIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    FabricBfdConfig,
    FabricBgpTimersConfig,
    FabricConfigInput,
    FabricDefinitionIntent,
    FabricInterSwitchLinksConfig,
    FabricIntent,
    FabricOverlayProtocolConfig,
    FabricUnderlayProtocolConfig,
    ForwardingClassIntent,
    IndexPoolIntent,
    IpPoolIntent,
    IrbInterfaceIntent,
    LagIntent,
    LinkIntent,
    NamespaceIntent,
    NodeGroupIntent,
    NodeIntent,
    PolicyStatementIntent,
    PrefixSetIntent,
    QueueIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
    StaticRouteIntent,
    TopologyGroupingIntent,
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
    RoutedInterfaceIpv6Addresses,
)
from automation.eda_models.protocols import StaticRouteSpec
from automation.eda_models.fabrics import (
    FabricBgp,
    FabricOspf,
    FabricSpec,
    FabricTimers,
    FabricUnderlayProtocol,
    FabricUnderlayProtocolBfd,
    FabricOverlayProtocol,
    FabricOverlayProtocolBfd,
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
    IndexAllocationPoolAllocations,
    IPAllocationPoolSpec,
    IPAllocationPoolSegments,
    NamespaceSpec,
)
from automation.eda_models.aifabrics import (
    BackendSpec,
    BackendStripes,
    BackendStripeConnector,
    BackendRocev2QoS,
    BackendGpuIsolationGroups,
)
from automation.eda_models.qos import QueueSpec
from automation.eda_models.aaa import NodeGroupSpec
from automation.eda_models.topologies import (
    TopologyGroupingSpec,
    TopologyGroupingGroupSelectors,
    TopologyGroupingTierSelectors,
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
from automation.eda_models.siteinfo import DefaultMTUSpec, BannerSpec

logger = logging.getLogger(__name__)

MANAGED_BY_LABEL = "eda.nokia.com/managed-by"
MANAGED_BY_VALUE = "nvd-automation"
NVD_DESIGN_LABEL = "eda.nokia.com/nvd-design"

# EDA marks resources implicitly created by another resource (e.g. the
# Policy/PrefixSet that a Fabric reconciler materialises for its eBGP ISL
# routing-policies) with ``eda.nokia.com/source: derived``. These resources
# are owned by their parent and must not be pruned or destroyed directly —
# EDA cleans them up when the parent goes away.
DERIVED_SOURCE_LABEL = "eda.nokia.com/source"
DERIVED_SOURCE_VALUE = "derived"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


# Attribute names on a Registry, keyed by the module-level CR_* global the
# CR builders read. Rebinding these globals lets a selected EDA profile drive
# the apiVersion strings without threading a registry into every builder.
_CR_GLOBALS: dict[str, str] = {
    "CR_INIT": "INIT",
    "CR_NODE_USER": "NODE_USER",
    "CR_NODE_PROFILE": "NODE_PROFILE",
    "CR_TOPO_NODE": "TOPO_NODE",
    "CR_TOPO_LINK": "TOPO_LINK",
    "CR_INDEX_ALLOCATION_POOL": "INDEX_ALLOCATION_POOL",
    "CR_IP_ALLOCATION_POOL": "IP_ALLOCATION_POOL",
    "CR_INTERFACE": "INTERFACE",
    "CR_FABRIC": "FABRIC",
    "CR_BRIDGE_DOMAIN": "BRIDGE_DOMAIN",
    "CR_ROUTER": "ROUTER",
    "CR_IRB_INTERFACE": "IRB_INTERFACE",
    "CR_VLAN": "VLAN",
    "CR_ROUTED_INTERFACE": "ROUTED_INTERFACE",
    "CR_STATIC_ROUTE": "STATIC_ROUTE",
    "CR_CONFIGLET": "CONFIGLET",
    "CR_DEFAULT_MTU": "DEFAULT_MTU",
    "CR_BANNER": "BANNER",
    "CR_POLICY": "POLICY",
    "CR_PREFIX_SET": "PREFIX_SET",
    "CR_NAMESPACE": "NAMESPACE",
    "CR_NODE_GROUP": "NODE_GROUP",
    "CR_TOPOLOGY_GROUPING": "TOPOLOGY_GROUPING",
    "CR_QUEUE": "QUEUE",
    "CR_FORWARDING_CLASS": "FORWARDING_CLASS",
    "CR_AI_BACKEND": "AI_BACKEND",
}


def _bind_registry(registry: Registry) -> None:
    """Rebind the module-level CR_* descriptors to *registry*'s CR types."""
    g = globals()
    for cr_global, attr in _CR_GLOBALS.items():
        g[cr_global] = getattr(registry, attr)


def generate(
    intent: FabricIntent,
    output_dir: Path | None = None,
    registry: Registry | None = None,
) -> list[dict]:
    """
    Generate all EDA CRs from a FabricIntent.

    Args:
        intent: The complete fabric intent
        output_dir: If set, write eda_transaction.json here
        registry: EDA registry profile to source apiVersions from
            (default profile when omitted).

    Returns:
        Ordered list of EDA CR dicts
    """
    registry = registry or get_default_registry()

    # A fresh 26.4.x install serves services/protocols at v2 with breaking spec
    # shape changes — delegate to the v2 backend, which reuses the unchanged
    # builders from this module.
    if getattr(registry, "generator_variant", "v1") == "v2":
        from automation.generators import eda_generator_v2
        return eda_generator_v2.generate(intent, output_dir=output_dir, registry=registry)

    _bind_registry(registry)

    design = intent.design
    resources: list[dict] = []

    # Namespace resolution. ``intent.ns_of()`` falls back to the intent default
    # for any resource that does not pin a namespace, so single-fabric designs
    # behave exactly as they did when this generator read one ``ns`` up front.
    ns = intent.eda.namespace
    _ns = intent.ns_of
    # Every namespace that needs the shared bootstrap resources (Init, NodeUser,
    # NodeGroup, NodeProfile). Derived from where the *nodes* live rather than
    # from namespaces_in_use(): a namespace holding only eda-system-scoped
    # resources has nothing to onboard.
    node_namespaces: list[str] = []
    for node in intent.nodes:
        node_ns = _ns(node)
        if node_ns not in node_namespaces:
            node_namespaces.append(node_ns)
    if not node_namespaces:
        node_namespaces = [ns]

    # 0. Namespaces themselves (created in eda-system, before anything in them)
    for nsi in intent.namespaces:
        resources.append(_cr_namespace(nsi, design))

    # 0b. TopologyGrouping (topology view; independent of fabric resources)
    for grouping in intent.topology_groupings:
        resources.append(_cr_topology_grouping(grouping, design))

    # 1. Init (commitSave) — one per namespace that owns nodes
    for node_ns in node_namespaces:
        resources.append(_cr_init(node_ns, design))

    # 1b. NodeGroups — must precede NodeUser, whose groupBindings reference them
    for group in intent.node_groups:
        resources.append(_cr_node_group(group, _ns(group), design))

    # 2. NodeUser — one per namespace that owns nodes
    creds = intent.credentials
    for node_ns in node_namespaces:
        resources.append(_cr_node_user(node_ns, design, creds.username, creds.password))

    # 3. NodeProfile — derived from node version, one per namespace
    node_version = intent.nodes[0].version if intent.nodes else ""
    profile_name = intent.eda.node_profile or f"clab-srlinux-{node_version}"
    if node_version:
        for node_ns in node_namespaces:
            resources.append(
                _cr_node_profile(
                    node_ns, profile_name, node_version, design,
                    creds.username, creds.password,
                )
            )

    # 4. TopoNodes (individual per-node CRs)
    for node in intent.nodes:
        resources.append(_cr_topo_node(node, profile_name, _ns(node), design))

    # 5. Interfaces — ISL interfaces (one per unique node/interface endpoint).
    # Keyed by namespace too: the same node name in two namespaces is two nodes.
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
            resources.append(_cr_interface_isl(node, iface, link_ns, design))

    # 6. Interfaces — Edge interfaces
    for ei in intent.edge_interfaces:
        resources.append(_cr_interface_edge(ei, _ns(ei), design))

    # 7. Interfaces — LAG member interfaces
    for lag in intent.lags:
        for member in lag.members:
            resources.append(
                _cr_interface_lag_member(member.node, member.interface, _ns(lag), design)
            )

    # 8. LAG Interfaces
    for lag in intent.lags:
        resources.append(_cr_interface_lag(lag, _ns(lag), design))

    # 9. Links
    for link in intent.links:
        resources.append(_cr_topo_link(link, _ns(link), design))

    # 10-11. Allocation pools. A design that declares its own pools (any
    # multi-fabric design must, since each fabric needs its own) owns them
    # outright; otherwise they are synthesized from the intent's ASN/prefix
    # scalars for the single fabric.
    if intent.index_pools or intent.ip_pools:
        for ipool in intent.index_pools:
            resources.append(_cr_index_pool(ipool, _ns(ipool), design))
        for ippool in intent.ip_pools:
            resources.append(_cr_ip_pool(ippool, _ns(ippool), design))
    else:
        # Collapsed-spine uses a single pool because there is no distinct
        # spine tier. 3-stage uses separate leaf-asn + spine-asn pools.
        if _is_collapsed_spine(intent):
            resources.append(
                _cr_index_allocation_pool(
                    "collapsed-spine-asn",
                    intent.leaf_asn_start,
                    20,
                    ns,
                    design,
                )
            )
        else:
            resources.append(
                _cr_index_allocation_pool(
                    "leaf-asn", intent.leaf_asn_start, 20, ns, design
                )
            )
            resources.append(
                _cr_index_allocation_pool(
                    "spine-asn", intent.spine_asn, 10, ns, design
                )
            )
        resources.append(
            _cr_ip_allocation_pool("system0", intent.system0_prefix, ns, design)
        )

    # 11a. Seed the pools EDA would have installed into any namespace the design
    # creates itself. A design's own declaration always wins.
    declared_pools = {(_ns(p), p.name) for p in intent.index_pools}
    for nsi in intent.namespaces:
        for pool_name, (start, size) in _EDA_DEFAULT_INDEX_POOLS.items():
            if (nsi.name, pool_name) in declared_pools:
                continue
            resources.append(
                _cr_index_allocation_pool(
                    pool_name, start, size, nsi.name, design
                )
            )

    # 11b. QoS scaffolding — Queues and ForwardingClasses that the AI Backend's
    # RoCEv2 policies bind to, so they must exist before it.
    for fc in intent.forwarding_classes:
        resources.append(_cr_forwarding_class(fc, _ns(fc), design))
    for queue in intent.queues:
        resources.append(_cr_queue(queue, _ns(queue), design))

    # 12. Routing policy — PrefixSets + Policies before Fabric (Fabric refs them).
    # Skip ``internal`` entries: they are fabric control-plane defaults handled
    # natively by EDA's Fabric reconciler and must not be materialized as
    # standalone Policy/PrefixSet CRs.
    for ps in intent.prefix_sets:
        if ps.internal:
            continue
        resources.append(_cr_prefix_set(ps, _ns(ps), design))
    for rp in intent.routing_policies:
        if rp.internal:
            continue
        resources.append(_cr_policy(rp, _ns(rp), design))

    # 13. Fabrics. Explicit definitions win; otherwise one Fabric is
    # synthesized from the intent's node roles (the single-fabric path).
    if intent.fabrics:
        for fabric in intent.fabrics:
            resources.append(_cr_fabric_definition(fabric, _ns(fabric), design))
    else:
        resources.append(_cr_fabric(intent, ns, design))

    # 13b. AI backend fabrics
    for backend in intent.ai_backends:
        resources.append(_cr_ai_backend(backend, _ns(backend), design))

    # 13. Bridge domains
    for bd in intent.bridge_domains:
        resources.append(_cr_bridge_domain(bd, _ns(bd), design))

    # 14. Routers
    for router in intent.routers:
        resources.append(_cr_router(router, _ns(router), design))

    # 15. IRB interfaces
    for irb in intent.irb_interfaces:
        resources.append(_cr_irb_interface(irb, _ns(irb), design))

    # 16. VLANs
    for vlan in intent.vlans:
        resources.append(_cr_vlan(vlan, _ns(vlan), design))

    # 17. Routed interfaces
    for ri in intent.routed_interfaces:
        resources.append(_cr_routed_interface(ri, _ns(ri), design))

    # 18. Static routes
    for sr in intent.static_routes:
        resources.append(_cr_static_route(sr, _ns(sr), design))

    # 19. Configlets — dynamic load balancing first, matching the order the
    #     design used to emit it in.
    for backend in intent.ai_backends:
        ns = _ns(backend)
        for cfglet in dlb_configlet_intents(backend, ns, design):
            resources.append(_cr_configlet(cfglet, ns, design))
    for cfglet in intent.configlets:
        resources.append(_cr_configlet(cfglet, _ns(cfglet), design))

    # 20. Default MTUs
    for mtu in intent.default_mtus:
        resources.append(_cr_default_mtu(mtu, _ns(mtu), design))

    # 21. Banners
    for banner in intent.banners:
        resources.append(_cr_banner(banner, _ns(banner), design))

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        tx_path = output_dir / "eda_transaction.json"
        with open(tx_path, "w") as f:
            json.dump(resources, f, indent=2)
        logger.info("Wrote %d CRs to %s", len(resources), tx_path)

    return resources


# ---------------------------------------------------------------------------
# Manifest export (inspection / GitOps review)
# ---------------------------------------------------------------------------


def _sanitize_filename(value: str) -> str:
    """Make a CR name safe to embed in a filename."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-")
    return safe or "unnamed"


def export_manifests(
    resources: list[dict],
    output_dir: Path,
    *,
    step: int = 10,
    sync_wave: bool = False,
) -> list[Path]:
    """Write each EDA CR to its own numbered YAML file for inspection.

    The ``resources`` list is already in dependency order, so the numeric
    filename prefix (zero-padded, ``step`` apart) reproduces that order under
    ``kubectl apply -f``/Argo, which read a directory in lexical filename order.

    Args:
        resources: Ordered list of EDA CR dicts (from :func:`generate`).
        output_dir: Directory to (re)create the manifest files in.
        step: Increment between numeric filename prefixes (default 10, so
            there's room to hand-insert resources between generated ones).
        sync_wave: When True, stamp each CR with an
            ``argocd.argoproj.io/sync-wave`` annotation matching its order
            so Argo CD applies them in the same sequence.

    Returns:
        The list of written file paths, in order.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clear any previously generated manifests so stale CRs don't linger.
    for stale in output_dir.glob("*.yaml"):
        stale.unlink()

    total = len(resources)
    width = max(3, len(str(total * step)))
    written: list[Path] = []

    for idx, cr in enumerate(resources, start=1):
        prefix = str(idx * step).zfill(width)
        kind = cr.get("kind", "Unknown")
        name = cr.get("metadata", {}).get("name", "unnamed")
        filename = f"{prefix}-{kind}-{_sanitize_filename(name)}.yaml"

        if sync_wave:
            cr = json.loads(json.dumps(cr))  # deep copy, don't mutate caller's dict
            meta = cr.setdefault("metadata", {})
            annotations = meta.setdefault("annotations", {})
            annotations["argocd.argoproj.io/sync-wave"] = str(idx)

        path = output_dir / filename
        with open(path, "w") as f:
            yaml.safe_dump(cr, f, sort_keys=False, default_flow_style=False)
        written.append(path)

    logger.info("Wrote %d manifest files to %s", len(written), output_dir)
    return written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _managed_labels(
    extra: dict[str, str] | None = None,
    origin: str = "",
) -> dict[str, str]:
    """Return labels with the managed-by marker and optional provenance."""
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE}
    if origin:
        labels[NVD_DESIGN_LABEL] = origin
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
    origin: str = "",
) -> dict:
    """Wrap a Pydantic spec model into a full EDA CR dict."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or _managed_labels(origin=origin),
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
    origin: str = "",
) -> dict:
    """Wrap a raw spec dict into a full EDA CR dict (for complex cases)."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels or _managed_labels(origin=origin),
        },
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# CR builders — using generated models
# ---------------------------------------------------------------------------


def _cr_init(ns: str, design: str) -> dict:
    spec = InitSpec(commit_save=True, mgmt=InitMgmt(ipv4_dhcp=True, ipv6_dhcp=True))
    return _wrap_cr(CR_INIT.api_version, CR_INIT.kind, "init-base", ns, spec, origin=design)


def _cr_node_user(ns: str, design: str, username: str = "admin", password: str = "NokiaSrl1!") -> dict:
    spec = NodeUserSpec(
        username=username,
        password=password,
        group_bindings=[NodeUserGroupBindings(groups=["sudo"], node_selector=[""])],
    )
    return _wrap_cr(CR_NODE_USER.api_version, CR_NODE_USER.kind, username, ns, spec, origin=design)


def _cr_node_profile(ns: str, profile_name: str, version: str, design: str, username: str = "admin", password: str = "NokiaSrl1!") -> dict:
    ver_escaped = version.replace(".", "\\.")
    spec = NodeProfileSpec(
        images=[
            NodeProfileImages(
                image=f"srlimages/srlinux-{version}-bin/srlinux.bin",
                image_md5=f"srlimages/srlinux-{version}-md5/srlinux.md5",
            )
        ],
        llm_db=f"https://eda-asvr.eda-system.svc/eda-system/llm-dbs/llm-db-srlinux-ghcr-{version}/llm-embeddings-srl-{version.replace('.', '-')}.tar.gz",
        node_user=username,
        onboarding_username=username,
        onboarding_password=password,
        operating_system="srl",
        port=57410,
        version=version,
        version_match=f"v{ver_escaped}.*",
        version_path=".system.information.version",
        yang=f"https://eda-asvr.eda-system.svc/eda-system/schemaprofiles/srlinux-ghcr-{version}/srlinux-{version}.zip",
        annotate=True,
    )
    return _wrap_cr(CR_NODE_PROFILE.api_version, CR_NODE_PROFILE.kind, profile_name, ns, spec, origin=design)


def _cr_topo_node(node: NodeIntent, node_profile: str, ns: str, design: str) -> dict:
    """Generate a TopoNode CR for each node."""
    labels = _managed_labels(
        {"eda.nokia.com/name": node.name, **node.labels},
        origin=design,
    )
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
    return _wrap_cr(CR_TOPO_NODE.api_version, CR_TOPO_NODE.kind, node.name, ns, spec, labels)


def _cr_interface_isl(node: str, interface: str, ns: str, design: str) -> dict:
    """Generate an ISL Interface CR."""
    name = f"{node}-{interface.replace('/', '-')}"
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=interface, node=node)],
        type="interface",
    )
    return _wrap_cr(
        CR_INTERFACE.api_version, CR_INTERFACE.kind, name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "interSwitch"}, origin=design),
    )


def _cr_interface_edge(ei: EdgeInterfaceIntent, ns: str, design: str) -> dict:
    """Generate an edge Interface CR."""
    labels = _managed_labels({**ei.labels, "eda.nokia.com/role": "edge"}, origin=design)
    spec = InterfaceSpec(
        enabled=True,
        lldp=True,
        members=[InterfaceMembers(enabled=True, interface=ei.interface, node=ei.node)],
        type="interface",
        encap_type=ei.encap if ei.encap in ("dot1q", "null") else None,
    )
    return _wrap_cr(CR_INTERFACE.api_version, CR_INTERFACE.kind, ei.name, ns, spec, labels)


def _cr_interface_lag_member(node: str, interface: str, ns: str, design: str) -> dict:
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
        CR_INTERFACE.api_version, CR_INTERFACE.kind, name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "edge"}, origin=design),
    )


def _cr_interface_lag(lag: LagIntent, ns: str, design: str) -> dict:
    """Generate a LAG Interface CR."""
    labels = _managed_labels({**lag.labels, "eda.nokia.com/role": "edge"}, origin=design)

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
        mode="active",
        interval=lag.lacp.interval,
        system_priority=lag.lacp.system_priority,
        system_id_mac=lag.lacp.system_id_mac,
        admin_key=admin_key,
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
    return _wrap_cr(CR_INTERFACE.api_version, CR_INTERFACE.kind, lag.name, ns, spec, labels)


def _cr_topo_link(link: LinkIntent, ns: str, design: str) -> dict:
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
        CR_TOPO_LINK.api_version, CR_TOPO_LINK.kind, link.name, ns, spec,
        _managed_labels({"eda.nokia.com/role": "interSwitch"}, origin=design),
    )


# EDA seeds these index pools into its own namespace when it is installed, and
# its service intents resolve them by well-known name *inside the namespace of
# the resource being deployed*. A design that brings up its own namespaces gets
# none of them, so the first BridgeDomain or LAG deployed there dies on
# "pool template Index:tunnel-index-pool does not exist in namespace ...".
# Ranges mirror the EDA install defaults. asn-pool and leafindex-pool are
# deliberately absent: those carry design-specific ranges and pinned
# allocations, so a design that needs them declares them itself.
_EDA_DEFAULT_INDEX_POOLS: dict[str, tuple[int, int]] = {
    "es-index-pool": (1, 16777216),
    "evi-pool": (100, 4000),
    "irb-subif-pool": (0, 4000),
    "lag-admin-key-pool": (1, 65535),
    "lagid-pool": (1, 128),
    "loopback-id-pool": (0, 255),
    "mirror-sdp-pool": (7000, 1000),
    "subif-pool": (0, 4000),
    "tunnel-index-pool": (500, 4000),
    "vlan-pool": (1, 4000),
    "vni-pool": (200, 4000),
}


def _cr_index_allocation_pool(
    name: str, start: int, size: int, ns: str, design: str
) -> dict:
    spec = IndexAllocationPoolSpec(
        segments=[IndexAllocationPoolSegments(start=start, size=size)]
    )
    return _wrap_cr(CR_INDEX_ALLOCATION_POOL.api_version, CR_INDEX_ALLOCATION_POOL.kind, name, ns, spec, origin=design)


def _cr_ip_allocation_pool(name: str, subnet: str, ns: str, design: str) -> dict:
    spec = IPAllocationPoolSpec(
        segments=[IPAllocationPoolSegments(subnet=subnet)]
    )
    return _wrap_cr(CR_IP_ALLOCATION_POOL.api_version, CR_IP_ALLOCATION_POOL.kind, name, ns, spec, origin=design)


def _cr_index_pool(pool: IndexPoolIntent, ns: str, design: str) -> dict:
    """Generate an IndexAllocationPool CR from an explicit pool intent.

    Unlike :func:`_cr_index_allocation_pool` (which synthesizes a pool from the
    intent's ASN scalars) this carries optional pinned ``allocations`` — the AI
    backend needs a stable leaf index per rail leaf, since the index selects
    which spine port a leaf's uplinks land on.
    """
    spec = IndexAllocationPoolSpec(
        segments=[
            IndexAllocationPoolSegments(
                start=pool.start,
                size=pool.size,
                allocations=[
                    IndexAllocationPoolAllocations(name=a.name, value=a.value)
                    for a in pool.allocations
                ] or None,
            )
        ]
    )
    return _wrap_cr(
        CR_INDEX_ALLOCATION_POOL.api_version, CR_INDEX_ALLOCATION_POOL.kind,
        pool.name, ns, spec, origin=design,
    )


def _cr_ip_pool(pool: IpPoolIntent, ns: str, design: str) -> dict:
    """Generate an IPAllocationPool CR from an explicit pool intent."""
    spec = IPAllocationPoolSpec(
        segments=[IPAllocationPoolSegments(subnet=pool.subnet)]
    )
    return _wrap_cr(
        CR_IP_ALLOCATION_POOL.api_version, CR_IP_ALLOCATION_POOL.kind,
        pool.name, ns, spec, origin=design,
    )


def _cr_namespace(nsi: NamespaceIntent, design: str) -> dict:
    """Generate a Namespace CR.

    The CR itself lives in ``eda-system`` (or whatever ``parent_namespace``
    says); ``metadata.name`` is the namespace being created.
    """
    spec = NamespaceSpec(description=nsi.description or None)
    return _wrap_cr(
        CR_NAMESPACE.api_version, CR_NAMESPACE.kind, nsi.name,
        nsi.parent_namespace, spec, origin=design,
    )


def _cr_node_group(group: NodeGroupIntent, ns: str, design: str) -> dict:
    """Generate an AAA NodeGroup CR."""
    spec = NodeGroupSpec(
        services=list(group.services),
        superuser=group.superuser,
    )
    return _wrap_cr(
        CR_NODE_GROUP.api_version, CR_NODE_GROUP.kind, group.name, ns, spec,
        origin=design,
    )


def _cr_topology_grouping(grouping: TopologyGroupingIntent, design: str) -> dict:
    """Generate a TopologyGrouping CR (the fabric's topology view)."""
    spec = TopologyGroupingSpec(
        group_selectors=[
            TopologyGroupingGroupSelectors(
                group=gs.group,
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
    return _wrap_cr(
        CR_TOPOLOGY_GROUPING.api_version, CR_TOPOLOGY_GROUPING.kind,
        grouping.name, grouping.namespace, spec, origin=design,
    )


def _cr_queue(queue: QueueIntent, ns: str, design: str) -> dict:
    """Generate a QoS Queue CR."""
    spec = QueueSpec(
        queue_id=queue.queue_id,
        queue_type=queue.queue_type,
        traffic_type=queue.traffic_type,
    )
    return _wrap_cr(
        CR_QUEUE.api_version, CR_QUEUE.kind, queue.name, ns, spec, origin=design
    )


def _cr_forwarding_class(fc: ForwardingClassIntent, ns: str, design: str) -> dict:
    """Generate a QoS ForwardingClass CR.

    The EDA spec is an empty object — the resource is purely its name — so this
    is the one CR built from a raw ``{}`` rather than a typed model.
    """
    return _wrap_cr_raw(
        CR_FORWARDING_CLASS.api_version, CR_FORWARDING_CLASS.kind, fc.name, ns,
        {}, origin=design,
    )


def _cr_ai_backend(backend: AiBackendIntent, ns: str, design: str) -> dict:
    """Generate an aifabrics Backend CR (a rail-optimized AI fabric)."""
    qos = backend.rocev2_qos
    connector = backend.stripe_connector
    spec = BackendSpec(
        system_pool_ipv4=backend.system_pool_ipv4 or None,
        asn_pool=backend.asn_pool or None,
        ip_mtu=backend.ip_mtu,
        stripes=[
            BackendStripes(
                name=s.name,
                stripe_id=s.stripe_id,
                gpu_vlan=s.gpu_vlan,
                node_selector=list(s.node_selector),
                asn_pool=s.asn_pool or None,
                system_pool_ipv4=s.system_pool_ipv4 or None,
            )
            for s in backend.stripes
        ],
        gpu_isolation_groups=[
            BackendGpuIsolationGroups(
                name=g.name,
                interface_selector=list(g.interface_selector),
            )
            for g in backend.gpu_isolation_groups
        ],
        stripe_connector=(
            BackendStripeConnector(
                name=connector.name,
                node_selector=list(connector.node_selector),
                link_selector=list(connector.link_selector),
                asn_pool=connector.asn_pool or None,
                system_pool_ipv4=connector.system_pool_ipv4 or None,
            )
            if connector is not None
            else None
        ),
        rocev2_qo_s=BackendRocev2QoS(
            ecn_max_drop_probability_percent=qos.ecn_max_drop_probability_percent,
            ecn_slope_max_threshold_percent=qos.ecn_slope_max_threshold_percent,
            ecn_slope_min_threshold_percent=qos.ecn_slope_min_threshold_percent,
            pfc_deadlock_detection_timer=qos.pfc_deadlock_detection_timer,
            pfc_deadlock_recovery_timer=qos.pfc_deadlock_recovery_timer,
            queue_maximum_burst_size=qos.queue_maximum_burst_size,
        ),
    )
    return _wrap_cr(
        CR_AI_BACKEND.api_version, CR_AI_BACKEND.kind, backend.name, ns, spec,
        origin=design,
    )


def _cr_fabric_definition(
    fabric: FabricDefinitionIntent, ns: str, design: str
) -> dict:
    """Generate a Fabric CR from an explicit fabric definition.

    The counterpart to :func:`_cr_fabric`, which infers a single Fabric from the
    intent's node roles. Here the selectors and pools are stated outright,
    because a multi-fabric intent cannot infer which nodes belong to which
    fabric from roles alone — several fabrics each have leaves and spines.
    """
    cfg = fabric.fabric_config
    underlay = _build_underlay_protocol(cfg, [], [])
    overlay = _build_overlay_protocol(
        cfg, fabric.leaf_node_selector, fabric.spine_node_selector
    )
    isl = _build_inter_switch_links(cfg)
    if fabric.inter_switch_link_selector:
        isl.link_selector = list(fabric.inter_switch_link_selector)

    if underlay.bgp is not None and fabric.leaf_asn_pool:
        underlay.bgp.asn_pool = fabric.leaf_asn_pool

    spec = FabricSpec(
        system_pool_ipv4=fabric.system_pool_ipv4 or None,
        leafs=FabricLeafs(
            leaf_node_selector=list(fabric.leaf_node_selector) or None,
            asn_pool=fabric.leaf_asn_pool or None,
        ),
        spines=(
            FabricSpines(
                spine_node_selector=list(fabric.spine_node_selector),
                asn_pool=fabric.spine_asn_pool or None,
            )
            if fabric.spine_node_selector
            else None
        ),
        inter_switch_links=isl,
        underlay_protocol=underlay,
        overlay_protocol=overlay,
    )
    return _wrap_cr(
        CR_FABRIC.api_version, CR_FABRIC.kind, fabric.name, ns, spec, origin=design
    )


def _is_collapsed_spine(intent: FabricIntent) -> bool:
    """Return True for the collapsed-spine design.

    Detected by either the explicit ``design`` identifier or the absence
    of any node with role ``spine`` combined with the presence of the
    ``eda.nokia.com/role=collapsed-spine`` label.
    """
    if intent.design == "collapsed-spine":
        return True
    has_spine = any(n.role == "spine" for n in intent.nodes)
    has_cs_label = any(
        n.labels.get("eda.nokia.com/role") == "collapsed-spine"
        for n in intent.nodes
    )
    return (not has_spine) and has_cs_label


def _cr_fabric(intent: FabricIntent, ns: str, design: str) -> dict:
    """Generate the Fabric CR.

    For the 3-stage design every ``leaf``/``spine`` node feeds the
    Fabric selector. For the collapsed-spine design ToRs (role=="tor")
    are excluded from both selectors and the ``spines`` block is
    omitted entirely (the Fabric has no spine tier).

    Underlay/overlay protocol selection, BFD, BGP timers, and ISL wiring
    come from ``intent.fabric_config`` when supplied. When it's absent
    (3-stage-evpn-vxlan, collapsed-spine) the legacy hardcoded EBGP
    underlay + EBGP overlay + IPv6 unnumbered ISL defaults are used so
    those designs keep producing the same Fabric CR they always did.
    """
    leaf_selector: list[str] = []
    spine_selector: list[str] = []
    for node in intent.nodes:
        if node.role == "tor":
            # ToRs are onboarded but not part of the Fabric CR.
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

    collapsed = _is_collapsed_spine(intent)
    leaf_asn_pool = "collapsed-spine-asn" if collapsed else "leaf-asn"

    cfg = intent.fabric_config
    underlay = _build_underlay_protocol(cfg, export_policy, import_policy)
    overlay = _build_overlay_protocol(cfg, leaf_selector, spine_selector)
    isl = _build_inter_switch_links(cfg)

    fabric_kwargs: dict = dict(
        underlay_protocol=underlay,
        overlay_protocol=overlay,
        system_pool_ipv4="system0",
        inter_switch_links=isl,
        leafs=FabricLeafs(asn_pool=leaf_asn_pool, leaf_node_selector=leaf_selector),
    )
    # For non-collapsed designs (or if the topology does contain spines),
    # include the Fabric.spines block. For collapsed-spine, omit it.
    if spine_selector or not collapsed:
        fabric_kwargs["spines"] = FabricSpines(
            asn_pool="spine-asn", spine_node_selector=spine_selector
        )

    spec = FabricSpec(**fabric_kwargs)
    return _wrap_cr(CR_FABRIC.api_version, CR_FABRIC.kind, intent.fabric_name, ns, spec, origin=design)


# ---------------------------------------------------------------------------
# Fabric sub-spec builders
#
# Translate the design-agnostic FabricConfigInput (mirrors the EDA Fabric
# spec) into the auto-generated EDA Pydantic models. ``cfg=None`` reproduces
# the legacy behaviour expected by 3-stage-evpn-vxlan / collapsed-spine.
# ---------------------------------------------------------------------------


def _bgp_timers(timers: FabricBgpTimersConfig | None) -> FabricTimers | None:
    if timers is None:
        return None
    payload = timers.model_dump(exclude_none=True)
    return FabricTimers(**payload) if payload else None


def _underlay_bfd(bfd: FabricBfdConfig | None) -> FabricUnderlayProtocolBfd | None:
    if bfd is None:
        return None
    return FabricUnderlayProtocolBfd(**bfd.model_dump(exclude_none=True))


def _overlay_bfd(bfd: FabricBfdConfig | None) -> FabricOverlayProtocolBfd | None:
    if bfd is None:
        return None
    return FabricOverlayProtocolBfd(**bfd.model_dump(exclude_none=True))


def _build_underlay_protocol(
    cfg: FabricConfigInput | None,
    export_policy: list[str],
    import_policy: list[str],
) -> FabricUnderlayProtocol:
    """Build ``Fabric.spec.underlayProtocol`` from FabricConfigInput.

    When ``cfg`` is None the legacy EBGP defaults are emitted (BFD on, asn
    pool ``asn-pool``).
    """
    if cfg is None:
        return FabricUnderlayProtocol(
            protocol=["EBGP"],
            bgp=FabricBgp(
                asn_pool="asn-pool",
                export_policy=export_policy or None,
                import_policy=import_policy or None,
            ),
            bfd=FabricUnderlayProtocolBfd(
                enabled=True,
                desired_min_transmit_int=1000000,
                required_min_receive=1000000,
                detection_multiplier=3,
                min_echo_receive_interval=1000000,
            ),
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
            export_policy=merged_export or None,
            import_policy=merged_import or None,
            keychain=bgp_in.keychain if bgp_in else None,
            timers=_bgp_timers(bgp_in.timers if bgp_in else None),
        )

    ospf_obj: FabricOspf | None = None
    if up.ospf is not None and ("OSPFv2" in up.protocol or "OSPFv3" in up.protocol):
        ospf_obj = FabricOspf(address_family=up.ospf.address_family or None)

    return FabricUnderlayProtocol(
        protocol=up.protocol,
        bgp=bgp_obj,
        ospf=ospf_obj,
        bfd=_underlay_bfd(up.bfd),
    )


def _build_overlay_protocol(
    cfg: FabricConfigInput | None,
    leaf_selector: list[str],
    spine_selector: list[str],
) -> FabricOverlayProtocol:
    """Build ``Fabric.spec.overlayProtocol`` from FabricConfigInput.

    For IBGP overlays, ``rrNodeSelector`` and ``rrClientNodeSelector``
    default to the spine and leaf selectors respectively when the user
    didn't pin them explicitly.
    """
    if cfg is None:
        return FabricOverlayProtocol(protocol="EBGP")

    op = cfg.overlay_protocol
    bgp_obj: FabricBgp | None = None
    if op.protocol == "IBGP":
        b = op.bgp  # required (validated upstream)
        bgp_obj = FabricBgp(
            autonomous_system=b.autonomous_system,
            cluster_id=b.cluster_id,
            export_policy=b.export_policy or None,
            import_policy=b.import_policy or None,
            keychain=b.keychain,
            rr_node_selector=b.rr_node_selector or spine_selector or None,
            rr_client_node_selector=b.rr_client_node_selector or leaf_selector or None,
            rr_ip_addresses=b.rr_ip_addresses or None,
            timers=_bgp_timers(b.timers),
        )
    elif op.bgp is not None:
        # EBGP overlay: forward only the sub-fields that EDA actually honours
        # for an EBGP overlay (keychain + timers); RR-related options are
        # IBGP-only and are silently dropped to avoid confusing EDA.
        b = op.bgp
        bgp_obj = FabricBgp(
            keychain=b.keychain,
            timers=_bgp_timers(b.timers),
            export_policy=b.export_policy or None,
            import_policy=b.import_policy or None,
        )

    return FabricOverlayProtocol(
        protocol=op.protocol,
        bgp=bgp_obj,
        bfd=_overlay_bfd(op.bfd),
    )


def _build_inter_switch_links(cfg: FabricConfigInput | None) -> FabricInterswitchlinks:
    """Build ``Fabric.spec.interSwitchLinks`` from FabricConfigInput.

    The link selector is fixed (``eda.nokia.com/role=interSwitch``) — that's
    the label the generator stamps on every ISL Interface CR.
    """
    link_selector = ["eda.nokia.com/role=interSwitch"]
    if cfg is None:
        return FabricInterswitchlinks(
            link_selector=link_selector,
            unnumbered="IPV6",
        )
    isl = cfg.inter_switch_links
    return FabricInterswitchlinks(
        link_selector=link_selector,
        unnumbered=isl.unnumbered,
        pool_ipv4=isl.pool_ipv4,
        pool_ipv6=isl.pool_ipv6,
        ip_mtu=isl.ip_mtu,
        vlan_id=isl.vlan_id,
    )


# ---------------------------------------------------------------------------
# Routing-policy CR builders
# ---------------------------------------------------------------------------

_PROTOCOL_TO_EDA = {
    "local": "LOCAL",
    "bgp": "BGP",
    "aggregate": "AGGREGATE",
    "bgp_evpn": "BGP_EVPN",
    "static": "STATIC",
}


def _prefix_set_entries(ps: PrefixSetIntent) -> list[dict]:
    """Convert PrefixEntry list to EDA prefix-set spec entries."""
    entries: list[dict] = []
    for p in ps.prefixes:
        entry: dict = {"prefix": p.ip_prefix}
        mlr = (p.mask_length_range or "").strip()
        if not mlr or mlr == "exact":
            entry["exact"] = True
        elif ".." in mlr:
            lo, hi = mlr.split("..", 1)
            entry["startRange"] = int(lo)
            entry["endRange"] = int(hi)
        else:
            # Single length value — treat as a fixed range of that length
            length = int(mlr)
            entry["startRange"] = length
            entry["endRange"] = length
        entries.append(entry)
    return entries


def _cr_prefix_set(ps: PrefixSetIntent, ns: str, design: str) -> dict:
    """Generate a PrefixSet CR."""
    spec = {"prefix": _prefix_set_entries(ps)}
    return _wrap_cr_raw(
        CR_PREFIX_SET.api_version,
        CR_PREFIX_SET.kind,
        ps.name,
        ns,
        spec,
        origin=design,
    )


def _render_policy_statement(stmt: PolicyStatementIntent) -> dict:
    """Render one PolicyStatementIntent into an EDA Policy.statement[] dict.

    Protocol values are mapped from intent lowercase (``local``, ``bgp``,
    ``aggregate``, ``bgp_evpn``, ``static``) to EDA's uppercase enum.
    ``bgp_evpn_route_types`` are emitted under ``match.bgp.evpnRouteType``.
    """
    match: dict = {}
    if stmt.match.prefix_set:
        match["prefixSet"] = stmt.match.prefix_set
    if stmt.match.protocol:
        match["protocol"] = _PROTOCOL_TO_EDA.get(
            stmt.match.protocol, stmt.match.protocol.upper()
        )
    if stmt.match.bgp_evpn_route_types:
        match["bgp"] = {"evpnRouteType": list(stmt.match.bgp_evpn_route_types)}

    action: dict = {"policyResult": stmt.action.result}
    if stmt.action.set_local_preference is not None:
        action["bgp"] = {"localPreference": stmt.action.set_local_preference}

    out: dict = {"name": stmt.name}
    if match:
        out["match"] = match
    out["action"] = action
    return out


def _cr_policy(rp: RoutingPolicyIntent, ns: str, design: str) -> dict:
    """Generate a routing-policy Policy CR."""
    spec: dict = {
        "defaultAction": {"policyResult": rp.default_action},
        "statement": [_render_policy_statement(s) for s in rp.statements],
    }
    return _wrap_cr_raw(
        CR_POLICY.api_version,
        CR_POLICY.kind,
        rp.name,
        ns,
        spec,
        origin=design,
    )


def _cr_bridge_domain(bd: BridgeDomainIntent, ns: str, design: str) -> dict:
    """Generate a BridgeDomain CR.

    Honors the ``type`` field on the intent. SIMPLE bridge domains omit
    the VXLAN envelope (no vni/evi) and MAC duplication detection; the
    full EVPNVXLAN path is unchanged from the original behavior.
    """
    mac_dup = None
    if bd.mac_duplication:
        mac_dup = BridgeDomainMacDuplicationDetection(
            enabled=bd.mac_duplication.get("enabled", True),
            hold_down_time=bd.mac_duplication.get("hold_down_time", 9),
            monitoring_window=bd.mac_duplication.get("monitoring_window", 3),
            action=bd.mac_duplication.get("action", "StopLearning"),
            num_moves=bd.mac_duplication.get("num_moves", 5),
        )
    if bd.type == "SIMPLE":
        # Pure L2 bridge domain (no VXLAN envelope): omit the pool
        # references and vni/evi while preserving MAC-learning and
        # duplication-detection semantics.
        spec = BridgeDomainSpec(
            type="SIMPLE",
            mac_learning=bd.mac_learning,
            mac_aging=bd.mac_aging,
            mac_duplication_detection=mac_dup,
            vni_pool=None,
            evi_pool=None,
            tunnel_index_pool=None,
            export_target=bd.export_target,
            import_target=bd.import_target,
        )
    else:
        spec = BridgeDomainSpec(
            type="EVPNVXLAN",
            vni=bd.vni,
            evi=bd.evi,
            mac_learning=bd.mac_learning,
            mac_aging=bd.mac_aging,
            mac_duplication_detection=mac_dup,
            export_target=bd.export_target,
            import_target=bd.import_target,
        )
    return _wrap_cr(
        CR_BRIDGE_DOMAIN.api_version, CR_BRIDGE_DOMAIN.kind, bd.name, ns, spec,
        origin=bd.origin or design,
    )


def _cr_router(router: RouterIntent, ns: str, design: str) -> dict:
    """Generate a Router CR."""
    spec = RouterSpec(
        vni=router.vni,
        evi=router.evi,
        node_selector=router.node_selector,
        export_target=router.export_target,
        import_target=router.import_target,
    )
    return _wrap_cr(CR_ROUTER.api_version, CR_ROUTER.kind, router.name, ns, spec, origin=design)


def _cr_irb_interface(irb: IrbInterfaceIntent, ns: str, design: str) -> dict:
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
    return _wrap_cr(
        CR_IRB_INTERFACE.api_version, CR_IRB_INTERFACE.kind, irb.name, ns, spec,
        origin=irb.origin or design,
    )


def _cr_vlan(vlan: VlanIntent, ns: str, design: str) -> dict:
    """Generate a VLAN CR."""
    spec = VLANSpec(
        bridge_domain=vlan.bridge_domain,
        interface_selector=vlan.interface_selector,
        vlan_id=vlan.vlan_id,
    )
    return _wrap_cr(CR_VLAN.api_version, CR_VLAN.kind, vlan.name, ns, spec, origin=design)


def _cr_routed_interface(ri: RoutedInterfaceIntent, ns: str, design: str) -> dict:
    """Generate a RoutedInterface CR."""
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
        learn_unsolicited="NONE",
        arp_timeout=ri.arp_timeout,
        interface=ri.interface,
        router=ri.router,
        ipv4_addresses=ipv4_addrs or None,
        ipv6_addresses=ipv6_addrs or None,
    )
    return _wrap_cr(CR_ROUTED_INTERFACE.api_version, CR_ROUTED_INTERFACE.kind, ri.name, ns, spec, origin=design)


def _cr_static_route(sr: StaticRouteIntent, ns: str, design: str) -> dict:
    """Generate a StaticRoute CR."""
    spec = StaticRouteSpec(
        nexthop_group=sr.nexthop_group,
        prefixes=sr.prefixes,
        router=sr.router,
        nodes=sr.nodes,
    )
    return _wrap_cr(CR_STATIC_ROUTE.api_version, CR_STATIC_ROUTE.kind, sr.name, ns, spec, origin=design)


def _cr_configlet(cfglet: ConfigletIntent, ns: str, design: str) -> dict:
    """Generate a Configlet CR from a ConfigletIntent."""
    spec = ConfigletSpec(
        endpoint_selector=cfglet.endpoint_selector or None,
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
    return _wrap_cr(
        CR_CONFIGLET.api_version, CR_CONFIGLET.kind, cfglet.name, ns, spec,
        origin=cfglet.origin or design,
    )


def dlb_configlet_intents(
    backend: AiBackendIntent, ns: str, design: str
) -> list[ConfigletIntent]:
    """Render Backend dynamic load balancing as raw config.

    Two configlets: the system-level balancer on every rail leaf, and the
    prefix binding that sends IPv6 traffic in the *default* network-instance
    through it. Callers wrap these with their own ``_cr_configlet``, since the
    Configlet spec renamed ``endpointSelector`` to plural in 26.8.

    26.8's Backend CR has a native ``dynamicLoadBalancing`` block, but it is
    deliberately not used: EDA renders the prefix binding into *every* network
    instance the Backend owns, including the GPU isolation-group VRF. That VRF
    carries routes leaked from ``default``, and binding the balancer there
    drops their next-hop resolution — verified on 26.8.1, where it cost every
    rail its ECMP paths and left GPUs unable to reach their rail gateway.

    The weighting factors are SR Linux platform defaults, restated so the
    rendered config is explicit about what the fabric relies on.
    """
    dlb = backend.dynamic_load_balancing
    if dlb is None:
        return []

    system_dlb = {
        "load-balancing": {
            "dynamic": {
                "flowset-size": str(dlb.flowset_size),
                "inactivity-timer": dlb.inactivity_timer_us,
                "mode": "flow-dynamic" if dlb.mode == "Dynamic" else "packet-based",
                "link-quality-sampling-interval": dlb.sampling_interval_us,
                "weighting-factor": {
                    "port-utilization": 70,
                    "queue-utilization": 20,
                    "itm-utilization": 10,
                },
            }
        }
    }
    prefix_binding = {
        "ip-load-balancing": {
            "dynamic-load-balancing": {"prefix": [{"ip-prefix": "::/0"}]}
        }
    }

    return [
        ConfigletIntent(
            name=name,
            namespace=ns,
            endpoint_selector=list(dlb.endpoint_selector),
            priority=100,
            origin=design,
            configs=[
                ConfigletConfigEntry(
                    path=path,
                    operation="Create",
                    config=json.dumps(config, indent=2),
                )
            ],
        )
        for name, path, config in (
            ("dlb", ".system", system_dlb),
            (
                "ip-load-balance-network-instance",
                '.network-instance{.name=="default"}',
                prefix_binding,
            ),
        )
    ]


def _cr_default_mtu(mtu: DefaultMtuIntent, ns: str, design: str) -> dict:
    """Generate a DefaultMTU CR."""
    spec = DefaultMTUSpec(
        interface_mtu=mtu.interface_mtu,
        layer2_subif_mtu=mtu.layer2_subif_mtu,
        layer3_mtu=mtu.layer3_mtu,
        node_selector=mtu.node_selector or None,
        nodes=mtu.nodes or None,
    )
    return _wrap_cr(CR_DEFAULT_MTU.api_version, CR_DEFAULT_MTU.kind, mtu.name, ns, spec, origin=design)


def _cr_banner(banner: BannerIntent, ns: str, design: str) -> dict:
    """Generate a Banner CR."""
    spec = BannerSpec(
        login_banner=banner.login_banner or None,
        motd=banner.motd or None,
        node_selector=banner.node_selector or None,
        nodes=banner.nodes or None,
    )
    return _wrap_cr(CR_BANNER.api_version, CR_BANNER.kind, banner.name, ns, spec, origin=design)


