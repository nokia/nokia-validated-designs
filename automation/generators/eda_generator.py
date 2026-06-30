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
)
from automation.eda_models.profiles import Registry, get_default_registry
from automation.core.models import (
    BannerIntent,
    BridgeDomainIntent,
    BreakoutIntent,
    ConfigletIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    FabricBfdConfig,
    FabricBgpTimersConfig,
    FabricConfigInput,
    FabricInterSwitchLinksConfig,
    FabricIntent,
    FabricOverlayProtocolConfig,
    FabricUnderlayProtocolConfig,
    IrbInterfaceIntent,
    LagIntent,
    LinkIntent,
    NodeIntent,
    PolicyStatementIntent,
    PrefixSetIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
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

    ns = intent.eda.namespace
    design = intent.design
    resources: list[dict] = []

    # 1. Init (commitSave)
    resources.append(_cr_init(ns, design))

    # 2. NodeUser
    creds = intent.credentials
    resources.append(_cr_node_user(ns, design, creds.username, creds.password))

    # 3. NodeProfile — derived from node version
    node_version = intent.nodes[0].version if intent.nodes else ""
    profile_name = intent.eda.node_profile or f"clab-srlinux-{node_version}"
    if node_version:
        resources.append(_cr_node_profile(ns, profile_name, node_version, design, creds.username, creds.password))

    # 4. TopoNodes (individual per-node CRs)
    for node in intent.nodes:
        resources.append(_cr_topo_node(node, profile_name, ns, design))

    # 5. Interfaces — ISL interfaces (one per unique node/interface endpoint)
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
            resources.append(_cr_interface_isl(node, iface, ns, design))

    # 6. Interfaces — Edge interfaces
    for ei in intent.edge_interfaces:
        resources.append(_cr_interface_edge(ei, ns, design))

    # 7. Interfaces — LAG member interfaces
    for lag in intent.lags:
        for member in lag.members:
            resources.append(
                _cr_interface_lag_member(member.node, member.interface, ns, design)
            )

    # 8. LAG Interfaces
    for lag in intent.lags:
        resources.append(_cr_interface_lag(lag, ns, design))

    # 9. Links
    for link in intent.links:
        resources.append(_cr_topo_link(link, ns, design))

    # 10. ASN allocation pools
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

    # 11. IP allocation pool (system0)
    resources.append(
        _cr_ip_allocation_pool("system0", intent.system0_prefix, ns, design)
    )

    # 12. Routing policy — PrefixSets + Policies before Fabric (Fabric refs them).
    # Skip ``internal`` entries: they are fabric control-plane defaults handled
    # natively by EDA's Fabric reconciler and must not be materialized as
    # standalone Policy/PrefixSet CRs.
    for ps in intent.prefix_sets:
        if ps.internal:
            continue
        resources.append(_cr_prefix_set(ps, ns, design))
    for rp in intent.routing_policies:
        if rp.internal:
            continue
        resources.append(_cr_policy(rp, ns, design))

    # 13. Fabric
    resources.append(_cr_fabric(intent, ns, design))

    # 13. Bridge domains
    for bd in intent.bridge_domains:
        resources.append(_cr_bridge_domain(bd, ns, design))

    # 14. Routers
    for router in intent.routers:
        resources.append(_cr_router(router, ns, design))

    # 15. IRB interfaces
    for irb in intent.irb_interfaces:
        resources.append(_cr_irb_interface(irb, ns, design))

    # 16. VLANs
    for vlan in intent.vlans:
        resources.append(_cr_vlan(vlan, ns, design))

    # 17. Routed interfaces
    for ri in intent.routed_interfaces:
        resources.append(_cr_routed_interface(ri, ns, design))

    # 18. Static routes
    for sr in intent.static_routes:
        resources.append(_cr_static_route(sr, ns, design))

    # 19. Configlets
    for cfglet in intent.configlets:
        resources.append(_cr_configlet(cfglet, ns, design))

    # 20. Default MTUs
    for mtu in intent.default_mtus:
        resources.append(_cr_default_mtu(mtu, ns, design))

    # 21. Banners
    for banner in intent.banners:
        resources.append(_cr_banner(banner, ns, design))

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


