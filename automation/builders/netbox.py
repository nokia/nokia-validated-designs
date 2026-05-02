"""
NetBox builder — produces a ``FabricIntent`` by reading a NetBox Site and
its related objects.

The builder is design-agnostic: it behaves as a passthrough (similar to
``automation/designs/unconstrained_3_stage.py``). All design-specific
expansion (ASN allocation, ISL generation, default routing policies,
auto-generated configlets) happens upstream when the fabric is seeded
into NetBox — the seed invokes the appropriate per-design builder and
writes the already-expanded ``FabricIntent`` into NetBox. This module is
the inverse: read those NetBox objects and rebuild an identical
``FabricIntent``.

Primary conventions (see ``netbox_schema.py`` for the exact custom-field
names):

- Site custom fields carry the identity fields (``nvd_design``,
  ``nvd_environment``, ASN ranges, prefix strings, EDA settings) plus
  the lifecycle state. The ``nvd_config`` JSON custom field holds
  whatever cannot be mapped to native objects cleanly (IRBs, configlets,
  routing policies, credentials, routed interfaces, static routes,
  default MTUs, banners, breakouts).
- Devices carry ASN / system0 IP / SR Linux version / labels / uplinks
  in custom fields.
- Interfaces carry role / encap / labels / LAG + LACP parameters in
  custom fields.
- NetBox VLAN Groups model bridge domains (one per ``macvrf-*``); VNI /
  EVI / MAC settings in custom fields. VLANs inside a group carry the
  ``vlan_id_str`` and ``interface_selector``. VRFs model routers.
- NetBox Cables between two Device Interfaces become ISL links.
- Prefixes tagged ``nvd-managed`` on the site carry the loopback /
  management ranges.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from automation.builders.netbox_client import make_client
from automation.builders.netbox_schema import (
    CCTX_KEY_BANNERS,
    CCTX_KEY_BREAKOUTS,
    CCTX_KEY_CONFIGLETS,
    CCTX_KEY_DEFAULT_MTUS,
    CCTX_KEY_EDA,
    CCTX_KEY_FABRIC_EXPORT_POLICIES,
    CCTX_KEY_FABRIC_IMPORT_POLICIES,
    CCTX_KEY_PREFIX_SETS,
    CCTX_KEY_ROUTING_POLICIES,
    CFG_KEY_CREDENTIALS,
    CF_DEVICE_ASN,
    CF_DEVICE_LABELS,
    CF_DEVICE_SRL_VERSION,
    CF_DEVICE_SYSTEM0_IPV4,
    CF_DEVICE_UPLINK_INTERFACES,
    CF_IFACE_AGGREGATE_ID,
    CF_IFACE_BRIDGE_DOMAIN,
    CF_IFACE_ENCAP,
    CF_IFACE_IRB_CONFIG,
    CF_IFACE_LABELS,
    CF_IFACE_LACP,
    CF_IFACE_LACP_PORT_PRIORITY,
    CF_IFACE_LAG_MIN_LINKS,
    CF_IFACE_LAG_MODE,
    CF_IFACE_LAG_PREFERRED_ACTIVE,
    CF_IFACE_LAG_RELOAD_DELAY,
    CF_IFACE_LAG_REVERTIVE,
    CF_IFACE_LAG_STANDBY_SIGNALING,
    CF_IFACE_ROLE,
    CF_IFACE_ROUTED_CONFIG,
    CF_PREFIX_ROLE,
    CF_SITE_CONFIG,
    CF_SITE_DESIGN,
    CF_SITE_EDA_NAMESPACE,
    CF_SITE_EDA_NODE_PROFILE,
    CF_SITE_ENVIRONMENT,
    CF_SITE_LEAF_ASN_START,
    CF_SITE_MGMT_SUBNET,
    CF_SITE_SPINE_ASN,
    CF_SITE_SYSTEM0_PREFIX,
    CF_VLANGROUP_BD_TYPE,
    CF_VLANGROUP_EVI,
    CF_VLANGROUP_EXPORT_TARGET,
    CF_VLANGROUP_IMPORT_TARGET,
    CF_VLANGROUP_MAC_AGING,
    CF_VLANGROUP_MAC_DUPLICATION,
    CF_VLANGROUP_MAC_LEARNING,
    CF_VLANGROUP_ORIGIN,
    CF_VLANGROUP_VNI,
    CF_VLAN_BRIDGE_DOMAIN_REF,
    CF_VLAN_INTERFACE_SELECTOR,
    CF_VLAN_VLAN_ID_STR,
    CF_VRF_EVI,
    CF_VRF_EXPORT_TARGET,
    CF_VRF_IMPORT_TARGET,
    CF_VRF_NODE_SELECTOR,
    CF_VRF_SITE,
    CF_VRF_STATIC_ROUTES,
    CF_VRF_VNI,
    NVD_MANAGED_TAG,
    VLAN_UNTAGGED_SENTINEL_NAME,
    VLAN_UNTAGGED_SENTINEL_VID,
    group_vlan_suffix,
    iface_mode_to_encap,
    labels_list_to_dict,
    untagged_vlan_intent_name,
    vlan_label_key,
)
from automation.core.models import (
    BannerIntent,
    BreakoutIntent,
    BridgeDomainIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    Credentials,
    DefaultMtuIntent,
    EdaSettings,
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    IrbIpAddress,
    LacpConfig,
    LagIntent,
    LagMember,
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build(
    site_slug: str,
    netbox_url: str | None = None,
    netbox_token: str | None = None,
    *,
    verify_tls: bool = False,
) -> FabricIntent:
    """Build a ``FabricIntent`` from NetBox data for the given Site slug."""
    nb = make_client(url=netbox_url, token=netbox_token, verify_tls=verify_tls)
    return build_from_client(nb, site_slug)


def build_from_client(nb: Any, site_slug: str) -> FabricIntent:
    """Build a ``FabricIntent`` using an already-authenticated pynetbox client."""
    site = nb.dcim.sites.get(slug=site_slug)
    if site is None:
        raise ValueError(f"Site '{site_slug}' not found in NetBox")
    logger.info("Building FabricIntent for site %s", site_slug)

    site_cf = dict(getattr(site, "custom_fields", {}) or {})
    nvd_config = site_cf.get(CF_SITE_CONFIG) or {}
    if isinstance(nvd_config, str):
        nvd_config = json.loads(nvd_config) if nvd_config else {}

    # The non-native intent bundle (routing policies, configlets, MTUs,
    # banners, EDA settings, breakouts, …) lives in one or more per-site
    # ``extras.ConfigContext`` rows tagged ``nvd-managed``. We merge
    # them in ascending weight order so later contexts override earlier
    # ones key-by-key — mirroring how NetBox itself composes the
    # rendered config context on a device.
    ccx_data = _load_site_config_contexts(nb, site.id)

    # ---- Identity ----
    design = site_cf.get(CF_SITE_DESIGN) or "unconstrained-3-stage"
    environment = site_cf.get(CF_SITE_ENVIRONMENT) or "containerlab"
    spine_asn = site_cf.get(CF_SITE_SPINE_ASN) or 0
    leaf_asn_start = site_cf.get(CF_SITE_LEAF_ASN_START) or 0
    system0_prefix = site_cf.get(CF_SITE_SYSTEM0_PREFIX) or ""
    mgmt_subnet = site_cf.get(CF_SITE_MGMT_SUBNET) or ""

    # ---- Devices → NodeIntent ----
    devices = list(nb.dcim.devices.filter(site_id=site.id, limit=0))
    nodes = [_device_to_node(d) for d in devices]
    nodes.sort(key=lambda n: (_role_sort_key(n.role), n.name))

    # ---- Cables → LinkIntent ----
    links = _build_links(nb, site, devices)

    # ---- Interfaces on devices ----
    all_interfaces = list(
        nb.dcim.interfaces.filter(device_id=[d.id for d in devices], limit=0)
    ) if devices else []

    edge_interfaces = _build_edge_interfaces(all_interfaces)
    lags = _build_lags(all_interfaces)

    # ---- VLAN Groups → BridgeDomainIntent ----
    vlan_groups = list(nb.ipam.vlan_groups.filter(tag=NVD_MANAGED_TAG, limit=0))
    site_vlan_groups = [
        vg for vg in vlan_groups
        if _vlan_group_scope_site_id(vg) == site.id
    ]
    bridge_domains = [_vlan_group_to_bd(vg) for vg in site_vlan_groups]
    bridge_domains.sort(key=lambda bd: bd.name)

    # Every nvd-managed VLAN Group gets a reserved untagged VLAN
    # (``name="untagged"`` / ``vid=4094``). This auto-creates any
    # missing sentinels so humans who create a VLAN Group via the
    # NetBox GUI don't need to remember to also add the untagged row.
    _ensure_untagged_sentinels(nb, site, site_vlan_groups)

    # ---- VLANs → VlanIntent ----
    # NetBox 4.x: VLANs with site filter. We scope by tag instead.
    vlans_raw = list(nb.ipam.vlans.filter(tag=NVD_MANAGED_TAG, limit=0))
    # Collect sentinel VLAN ids actually referenced by an interface so
    # we don't emit a spurious VlanIntent for empty groups (e.g. IRB-only
    # bridge domains or fresh groups created via the GUI).
    referenced_untagged_vlan_ids = {
        getattr(u, "id", None)
        for u in (getattr(i, "untagged_vlan", None) for i in all_interfaces)
        if u is not None
    }
    vlans: list[VlanIntent] = []
    for v in vlans_raw:
        if _vlan_site_id(v) != site.id:
            continue
        if _is_sentinel_vlan(v) and not _sentinel_has_members(
            v, referenced_untagged_vlan_ids
        ):
            continue
        vlans.append(_vlan_to_intent(v))
    vlans.sort(key=lambda v: v.name)

    # ---- VRFs → RouterIntent (site-scoped via nvd_site custom field) ----
    # NetBox VRFs are global; we scope by the CF_VRF_SITE custom field
    # (the writer sets ``nvd_site = site.slug``). Two fabrics can share a
    # VRF name without colliding as long as each keeps its own VRF row
    # tagged with its site slug.
    vrfs_raw = [
        v for v in nb.ipam.vrfs.filter(tag=NVD_MANAGED_TAG, limit=0)
        if (getattr(v, "custom_fields", None) or {}).get(CF_VRF_SITE) == site_slug
    ]
    routers = [_vrf_to_router(v) for v in vrfs_raw]
    routers.sort(key=lambda r: r.name)

    # ---- Static routes (stashed on each VRF's nvd_static_routes CF) ----
    static_routes = _build_static_routes(vrfs_raw)

    # ---- IRB interfaces (native virtual interfaces) ----
    irb_interfaces = _build_irbs(nb, all_interfaces)

    # ---- Routed subinterfaces (native virtual interfaces, parent=edge) ----
    routed_interfaces = _build_routed_interfaces(all_interfaces)

    # ---- Configlets (Config Context) ----
    configlets = [
        ConfigletIntent(
            **{
                **c,
                "configs": [ConfigletConfigEntry(**e) for e in c.get("configs", [])],
            }
        )
        for c in ccx_data.get(CCTX_KEY_CONFIGLETS, [])
    ]

    # ---- Default MTUs ----
    default_mtus = [
        DefaultMtuIntent(**m) for m in ccx_data.get(CCTX_KEY_DEFAULT_MTUS, [])
    ]

    # ---- Banners ----
    banners = [BannerIntent(**b) for b in ccx_data.get(CCTX_KEY_BANNERS, [])]

    # ---- Routing policy ----
    prefix_sets = [
        PrefixSetIntent(
            **{
                **ps,
                "prefixes": [PrefixEntry(**p) for p in ps.get("prefixes", [])],
            }
        )
        for ps in ccx_data.get(CCTX_KEY_PREFIX_SETS, [])
    ]
    routing_policies = [
        RoutingPolicyIntent(
            **{
                **rp,
                "statements": [
                    PolicyStatementIntent(
                        name=s["name"],
                        match=PolicyMatch(**s.get("match", {})),
                        action=PolicyAction(**s.get("action", {})),
                    )
                    for s in rp.get("statements", [])
                ],
            }
        )
        for rp in ccx_data.get(CCTX_KEY_ROUTING_POLICIES, [])
    ]
    fabric_export_policies = list(
        ccx_data.get(CCTX_KEY_FABRIC_EXPORT_POLICIES, []) or []
    )
    fabric_import_policies = list(
        ccx_data.get(CCTX_KEY_FABRIC_IMPORT_POLICIES, []) or []
    )

    # ---- Breakouts (Config Context) ----
    breakouts = [BreakoutIntent(**b) for b in ccx_data.get(CCTX_KEY_BREAKOUTS, [])]

    # ---- Credentials (Site CF) / EDA (Config Context) ----
    creds_data = nvd_config.get(CFG_KEY_CREDENTIALS) or {}
    credentials = Credentials(**creds_data) if creds_data else Credentials()

    eda_data = ccx_data.get(CCTX_KEY_EDA) or {}
    eda_settings = EdaSettings(
        node_profile=eda_data.get(
            "node_profile", site_cf.get(CF_SITE_EDA_NODE_PROFILE) or ""
        ),
        namespace=eda_data.get(
            "namespace", site_cf.get(CF_SITE_EDA_NAMESPACE) or "eda"
        ),
    )

    return FabricIntent(
        design=design,
        fabric_name=site_slug,
        environment=environment,
        spine_asn=spine_asn,
        leaf_asn_start=leaf_asn_start,
        system0_prefix=system0_prefix,
        mgmt_subnet=mgmt_subnet,
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
# Helpers
# ---------------------------------------------------------------------------


_ROLE_SORT_PREFERENCE = {"leaf": 0, "collapsed-spine": 1, "spine": 2, "tor": 3}


def _role_sort_key(role: str) -> int:
    return _ROLE_SORT_PREFERENCE.get(role, 99)


def _cf(obj: Any) -> dict[str, Any]:
    cf = getattr(obj, "custom_fields", None)
    if cf is None:
        return {}
    return dict(cf)


def _role_slug(device: Any) -> str:
    role = getattr(device, "role", None) or getattr(device, "device_role", None)
    if role is None:
        return ""
    return getattr(role, "slug", "") or ""


def _platform_name(device: Any) -> str:
    """Return the device model name (e.g. '7220 IXR-D3L')."""
    dt = getattr(device, "device_type", None)
    if dt is None:
        return ""
    return getattr(dt, "model", "") or ""


def _primary_ip4_address(device: Any) -> str:
    """Return only the ``a.b.c.d`` part of the primary IPv4."""
    pip = getattr(device, "primary_ip4", None)
    if pip is None:
        return ""
    addr = getattr(pip, "address", "") or ""
    return addr.split("/", 1)[0]


def _device_to_node(device: Any) -> NodeIntent:
    cf = _cf(device)
    labels = cf.get(CF_DEVICE_LABELS) or {}
    if isinstance(labels, str):
        labels = json.loads(labels) if labels else {}
    uplinks = cf.get(CF_DEVICE_UPLINK_INTERFACES) or []
    if isinstance(uplinks, str):
        uplinks = json.loads(uplinks) if uplinks else []

    return NodeIntent(
        name=device.name,
        role=_role_slug(device),
        platform=_platform_name(device),
        version=cf.get(CF_DEVICE_SRL_VERSION) or "",
        system0_ipv4=cf.get(CF_DEVICE_SYSTEM0_IPV4) or "",
        asn=cf.get(CF_DEVICE_ASN) or 0,
        mgmt_ipv4=_primary_ip4_address(device),
        labels=dict(labels),
        uplink_interfaces=list(uplinks),
    )


def _build_links(nb: Any, site: Any, devices: list[Any]) -> list[LinkIntent]:
    """Reconstruct LinkIntents from NetBox Cables between two devices."""
    if not devices:
        return []
    device_names = {d.id: d.name for d in devices}

    # Cable → termination interfaces. In NetBox 4.x, each cable has
    # ``a_terminations`` and ``b_terminations`` lists. Filter by device
    # IDs in our site.
    cables = list(
        nb.dcim.cables.filter(
            tag=NVD_MANAGED_TAG, device_id=list(device_names.keys()), limit=0
        )
    )

    links: list[LinkIntent] = []
    for cable in cables:
        a_term = (cable.a_terminations or [None])[0]
        b_term = (cable.b_terminations or [None])[0]
        if a_term is None or b_term is None:
            continue

        a_iface = a_term.object if hasattr(a_term, "object") else a_term
        b_iface = b_term.object if hasattr(b_term, "object") else b_term

        a_dev_id = _interface_device_id(a_iface)
        b_dev_id = _interface_device_id(b_iface)

        if a_dev_id not in device_names or b_dev_id not in device_names:
            continue

        a_name = device_names[a_dev_id]
        b_name = device_names[b_dev_id]
        a_int = getattr(a_iface, "name", "")
        b_int = getattr(b_iface, "name", "")

        # Deterministic ordering: leaf/collapsed-spine side first (local).
        # If both same role, fall back to alphabetical name.
        a_role = _role_slug_by_device_id(nb, a_dev_id)
        b_role = _role_slug_by_device_id(nb, b_dev_id)
        if _role_sort_key(a_role) > _role_sort_key(b_role):
            a_name, b_name = b_name, a_name
            a_int, b_int = b_int, a_int

        link_name = f"{a_name}-{b_name}"
        links.append(
            LinkIntent(
                name=link_name,
                local_node=a_name,
                local_interface=a_int,
                remote_node=b_name,
                remote_interface=b_int,
            )
        )
    # Sort deterministically: by (local_node, local_interface) so repeated
    # builds produce identical output regardless of NetBox row order.
    links.sort(key=lambda lnk: (lnk.local_node, lnk.local_interface))
    return links


_role_cache: dict[int, str] = {}


def _role_slug_by_device_id(nb: Any, device_id: int) -> str:
    if device_id in _role_cache:
        return _role_cache[device_id]
    d = nb.dcim.devices.get(device_id)
    slug = _role_slug(d) if d else ""
    _role_cache[device_id] = slug
    return slug


def _interface_device_id(iface: Any) -> int | None:
    dev = getattr(iface, "device", None)
    if dev is None:
        return None
    return getattr(dev, "id", None)


def _build_edge_interfaces(interfaces: list[Any]) -> list[EdgeInterfaceIntent]:
    result: list[EdgeInterfaceIntent] = []
    for iface in interfaces:
        cf = _cf(iface)
        if cf.get(CF_IFACE_ROLE) != "edge":
            continue
        labels_raw = cf.get(CF_IFACE_LABELS)
        if isinstance(labels_raw, str):
            labels_raw = json.loads(labels_raw) if labels_raw else []
        labels = labels_list_to_dict(labels_raw or [], iface_role="edge")
        _merge_native_vlan_labels(iface, labels)
        device_name = getattr(getattr(iface, "device", None), "name", "")
        # 802.1Q Mode is the authoritative source of truth for encap;
        # fall back to the legacy CF_IFACE_ENCAP for pre-migration data.
        mode_value = _iface_mode_value(iface)
        if mode_value:
            encap = iface_mode_to_encap(mode_value)
        else:
            encap = cf.get(CF_IFACE_ENCAP) or "dot1q"
        result.append(
            EdgeInterfaceIntent(
                name=f"{device_name}-{iface.name}",
                node=device_name,
                interface=_iface_short_name(iface),
                encap=encap,
                labels=labels,
            )
        )
    result.sort(key=lambda e: e.name)
    return result


def _iface_short_name(iface: Any) -> str:
    """Return the SR Linux interface name (``ethernet-1-X``)."""
    return iface.name


def _iface_mode_value(iface: Any) -> str | None:
    """Extract the 802.1Q Mode as a plain string.

    pynetbox may expose ``Interface.mode`` as a ``Record`` with a
    ``value`` attribute (e.g. ``"tagged"``) and a ``label`` (e.g.
    ``"Tagged"``); our ``FakeStore`` uses plain strings. Normalise
    both shapes to the underlying slug.
    """
    mode = getattr(iface, "mode", None)
    if mode is None:
        return None
    value = getattr(mode, "value", None)
    if value is not None:
        return str(value)
    if isinstance(mode, str):
        return mode
    return None


def _merge_native_vlan_labels(iface: Any, labels: dict[str, str]) -> None:
    """Back-synthesise ``eda.nokia.com/{tagged|untagged}-v*`` labels.

    When the writer promoted a VLAN-membership label to the native
    ``Interface.untagged_vlan`` / ``Interface.tagged_vlans`` fields, the
    label is absent from ``nvd_labels``. Reconstruct it here from the
    native fields so the reader returns the same labels dict the
    design builder produced.

    The reserved untagged VLAN per group is literally named
    ``"untagged"``; its original ``untagged-v<N>`` label form is
    reconstructed from the VLAN's parent group name suffix.
    """
    untagged = getattr(iface, "untagged_vlan", None)
    if untagged is not None:
        name = getattr(untagged, "name", None)
        if name == VLAN_UNTAGGED_SENTINEL_NAME:
            group = getattr(untagged, "group", None)
            group_name = getattr(group, "name", None) if group is not None else None
            intent_name = untagged_vlan_intent_name(group_name or "")
            if intent_name:
                labels[vlan_label_key(intent_name)] = "enabled"
        elif name:
            labels[vlan_label_key(name)] = "enabled"
    for v in getattr(iface, "tagged_vlans", None) or []:
        name = getattr(v, "name", None)
        if name:
            labels[vlan_label_key(name)] = "enabled"


def _build_lags(interfaces: list[Any]) -> list[LagIntent]:
    """Reconstruct LAG intents from NetBox LAG interfaces.

    A fabric-level LAG (e.g. a multi-homed LAG that appears on four
    leaves) is modelled as one NetBox LAG interface per participating
    device. All instances share the same ``name`` and carry identical
    top-level LAG config in custom fields; only the member lists differ
    per device. We group by ``name`` and merge members.
    """
    lag_ifs = [i for i in interfaces if _cf(i).get(CF_IFACE_ROLE) == "lag"]
    member_ifs = [i for i in interfaces if _cf(i).get(CF_IFACE_ROLE) == "lag-member"]

    # Group members by the NetBox LAG interface id they point at.
    members_by_lag_id: dict[int, list[Any]] = {}
    for m in member_ifs:
        lag_ref = getattr(m, "lag", None)
        if lag_ref is None:
            continue
        lag_id = getattr(lag_ref, "id", None)
        if lag_id is None:
            continue
        members_by_lag_id.setdefault(lag_id, []).append(m)

    # Group the per-device LAG interfaces by their logical LAG ``name``.
    lag_ifs_by_name: dict[str, list[Any]] = {}
    for lag_if in lag_ifs:
        lag_ifs_by_name.setdefault(lag_if.name, []).append(lag_if)

    result: list[LagIntent] = []
    for lag_name, instances in lag_ifs_by_name.items():
        # All instances of the same logical LAG share top-level config;
        # read it off the first one.
        first = instances[0]
        cf = _cf(first)
        lacp_cf = cf.get(CF_IFACE_LACP) or {}
        if isinstance(lacp_cf, str):
            lacp_cf = json.loads(lacp_cf) if lacp_cf else {}
        labels_raw = cf.get(CF_IFACE_LABELS)
        if isinstance(labels_raw, str):
            labels_raw = json.loads(labels_raw) if labels_raw else []
        labels = labels_list_to_dict(labels_raw or [], iface_role="lag")
        _merge_native_vlan_labels(first, labels)

        # Merge all member interfaces across every instance.
        members: list[LagMember] = []
        for inst in instances:
            for m in members_by_lag_id.get(inst.id, []):
                mcf = _cf(m)
                members.append(
                    LagMember(
                        node=getattr(getattr(m, "device", None), "name", ""),
                        interface=_iface_short_name(m),
                        aggregate_id=str(mcf.get(CF_IFACE_AGGREGATE_ID) or ""),
                        lacp_port_priority=int(
                            mcf.get(CF_IFACE_LACP_PORT_PRIORITY) or 32768
                        ),
                    )
                )
        members.sort(key=lambda x: (x.node, x.interface))

        result.append(
            LagIntent(
                name=lag_name,
                type="lacp",
                multihoming_mode=cf.get(CF_IFACE_LAG_MODE) or "all-active",
                min_links=int(cf.get(CF_IFACE_LAG_MIN_LINKS) or 1),
                lacp=LacpConfig(
                    interval=lacp_cf.get("interval", "fast"),
                    system_id_mac=lacp_cf.get("system_id_mac", ""),
                    system_priority=int(lacp_cf.get("system_priority", 32768)),
                    admin_key=lacp_cf.get("admin_key"),
                    fallback=lacp_cf.get("fallback"),
                ),
                members=members,
                labels=labels,
                revertive=bool(cf.get(CF_IFACE_LAG_REVERTIVE) or False),
                preferred_active_node=cf.get(CF_IFACE_LAG_PREFERRED_ACTIVE) or "",
                standby_signaling=cf.get(CF_IFACE_LAG_STANDBY_SIGNALING) or "",
                reload_delay_timer=int(cf.get(CF_IFACE_LAG_RELOAD_DELAY) or 100),
            )
        )
    result.sort(key=lambda x: x.name)
    return result


def _ensure_untagged_sentinels(
    nb: Any, site: Any, site_vlan_groups: list[Any]
) -> None:
    """Ensure every nvd-managed VLAN Group at ``site`` has its reserved
    untagged VLAN (``name="untagged"`` / ``vid=4094``).

    Runs as an idempotent pre-pass at the start of
    :func:`build_from_client` so humans can create VLAN Groups in the
    NetBox UI without also having to remember to add the untagged
    sentinel — the next deploy materialises it on the fly.
    """
    if not site_vlan_groups:
        return
    for vg in site_vlan_groups:
        group_id = getattr(vg, "id", None)
        group_name = getattr(vg, "name", None)
        if group_id is None or not group_name:
            continue
        try:
            existing = nb.ipam.vlans.get(
                name=VLAN_UNTAGGED_SENTINEL_NAME, group_id=group_id
            )
        except TypeError:
            existing = nb.ipam.vlans.get(
                name=VLAN_UNTAGGED_SENTINEL_NAME, group=group_id
            )
        if existing is not None:
            continue
        description = (
            f"Untagged binding for BD {group_name}. "
            f"vid={VLAN_UNTAGGED_SENTINEL_VID} is a SENTINEL (NetBox requires 1..4094); "
            f"carries no VLAN tag — see CF {CF_VLAN_VLAN_ID_STR}."
        )
        payload: dict[str, Any] = {
            "name": VLAN_UNTAGGED_SENTINEL_NAME,
            "vid": VLAN_UNTAGGED_SENTINEL_VID,
            "group": group_id,
            "site": site.id,
            "status": "active",
            "description": description,
            "tags": [{"slug": NVD_MANAGED_TAG}],
            "custom_fields": {
                CF_VLAN_VLAN_ID_STR: "untagged",
                CF_VLAN_INTERFACE_SELECTOR: [],
                CF_VLAN_BRIDGE_DOMAIN_REF: group_name,
            },
        }
        logger.info(
            "  auto-creating reserved untagged VLAN for group %s", group_name
        )
        nb.ipam.vlans.create(**payload)


def _vlan_group_scope_site_id(vg: Any) -> int | None:
    """Return the site id a VLAN group is scoped to, if any."""
    scope = getattr(vg, "scope", None) or getattr(vg, "scope_object", None)
    scope_type = getattr(vg, "scope_type", None)
    # In NetBox 4.x this is spelled differently; try multiple paths.
    if scope is not None:
        return getattr(scope, "id", None)
    # Fallback: look at ``scope_id`` + ``scope_type`` pair
    if scope_type and "site" in str(scope_type):
        return getattr(vg, "scope_id", None)
    return None


def _vlan_site_id(v: Any) -> int | None:
    site = getattr(v, "site", None)
    if site is None:
        return None
    return getattr(site, "id", None)


def _vlan_group_to_bd(vg: Any) -> BridgeDomainIntent:
    cf = _cf(vg)
    mac_dup = cf.get(CF_VLANGROUP_MAC_DUPLICATION)
    if isinstance(mac_dup, str):
        mac_dup = json.loads(mac_dup) if mac_dup else None
    return BridgeDomainIntent(
        name=vg.name,
        type=cf.get(CF_VLANGROUP_BD_TYPE) or "EVPNVXLAN",
        vni=cf.get(CF_VLANGROUP_VNI),
        evi=cf.get(CF_VLANGROUP_EVI),
        mac_learning=bool(cf.get(CF_VLANGROUP_MAC_LEARNING, True)),
        mac_aging=int(cf.get(CF_VLANGROUP_MAC_AGING) or 300),
        mac_duplication=mac_dup if mac_dup is not None else {
            "enabled": True,
            "hold_down_time": 9,
            "monitoring_window": 3,
            "action": "StopLearning",
            "num_moves": 5,
        },
        origin=cf.get(CF_VLANGROUP_ORIGIN) or "",
        export_target=cf.get(CF_VLANGROUP_EXPORT_TARGET) or None,
        import_target=cf.get(CF_VLANGROUP_IMPORT_TARGET) or None,
    )


def _is_sentinel_vlan(v: Any) -> bool:
    """Return True when the given VLAN is the reserved untagged sentinel."""
    if getattr(v, "name", None) == VLAN_UNTAGGED_SENTINEL_NAME:
        return True
    cf = _cf(v)
    return (cf.get(CF_VLAN_VLAN_ID_STR) or "").lower() == "untagged"


def _sentinel_has_members(v: Any, referenced_vlan_ids: set[int | None]) -> bool:
    """Return True if the sentinel carries an explicit selector or is
    referenced by some interface's ``untagged_vlan``.

    Auto-created sentinels that nothing uses yet are silently dropped
    from the resulting intent, so empty / IRB-only VLAN Groups don't
    pollute the intent with placeholder VlanIntents. The sentinel row
    itself stays in NetBox so a human can wire it up later from the UI.
    """
    cf = _cf(v)
    selector = cf.get(CF_VLAN_INTERFACE_SELECTOR) or []
    if isinstance(selector, str):
        selector = json.loads(selector) if selector else []
    if selector:
        return True
    return getattr(v, "id", None) in referenced_vlan_ids


def _vlan_to_intent(v: Any) -> VlanIntent:
    cf = _cf(v)
    sel = cf.get(CF_VLAN_INTERFACE_SELECTOR) or []
    if isinstance(sel, str):
        sel = json.loads(sel) if sel else []

    vlan_id_str = cf.get(CF_VLAN_VLAN_ID_STR) or str(v.vid)
    is_untagged = (
        getattr(v, "name", "") == VLAN_UNTAGGED_SENTINEL_NAME
        or vlan_id_str.lower() == "untagged"
    )
    if is_untagged:
        group = getattr(v, "group", None)
        group_name = getattr(group, "name", None) if group is not None else None
        intent_name = untagged_vlan_intent_name(group_name or "")
        bridge_domain = (
            cf.get(CF_VLAN_BRIDGE_DOMAIN_REF) or group_name or ""
        )
        interface_selector = list(sel) or [f"eda.nokia.com/{intent_name}=enabled"]
        return VlanIntent(
            name=intent_name,
            bridge_domain=bridge_domain,
            vlan_id="untagged",
            interface_selector=interface_selector,
        )

    return VlanIntent(
        name=v.name,
        bridge_domain=cf.get(CF_VLAN_BRIDGE_DOMAIN_REF) or "",
        vlan_id=vlan_id_str,
        interface_selector=list(sel),
    )


def _vrf_to_router(v: Any) -> RouterIntent:
    cf = _cf(v)
    sel = cf.get(CF_VRF_NODE_SELECTOR) or []
    if isinstance(sel, str):
        sel = json.loads(sel) if sel else []
    return RouterIntent(
        name=v.name,
        vni=int(cf.get(CF_VRF_VNI) or 0),
        evi=int(cf.get(CF_VRF_EVI) or 0),
        node_selector=list(sel),
        export_target=cf.get(CF_VRF_EXPORT_TARGET) or None,
        import_target=cf.get(CF_VRF_IMPORT_TARGET) or None,
    )


def _load_site_config_contexts(nb: Any, site_id: int) -> dict[str, Any]:
    """Return a merged dict of every ``nvd-managed`` context for this site.

    Shallow merge in ascending weight order: later contexts override
    earlier keys wholesale. We don't try to deep-merge lists — that
    matches the way NetBox itself composes rendered contexts and keeps
    round-trips predictable for the current single-context layout.
    """
    try:
        contexts = list(
            nb.extras.config_contexts.filter(
                site_id=site_id, tag=NVD_MANAGED_TAG, limit=0,
            )
        )
    except Exception:
        return {}

    contexts.sort(key=lambda c: getattr(c, "weight", 0))
    merged: dict[str, Any] = {}
    for ctx in contexts:
        data = getattr(ctx, "data", None) or {}
        if isinstance(data, str):
            data = json.loads(data) if data else {}
        merged.update(data)
    return merged


def _build_static_routes(vrfs: list[Any]) -> list[StaticRouteIntent]:
    """Reconstruct StaticRouteIntent entries stashed on each VRF's CF.

    The writer strips the redundant ``router`` ref before storing, so we
    stamp it back on from the owning VRF's name. Sorted by name for a
    stable intent (the model validator relies on name uniqueness across
    the whole list — not per-VRF — which we also preserve).
    """
    out: list[StaticRouteIntent] = []
    for vrf in vrfs:
        cf = _cf(vrf)
        entries = cf.get(CF_VRF_STATIC_ROUTES) or []
        if isinstance(entries, str):
            entries = json.loads(entries) if entries else []
        for entry in entries:
            data = dict(entry)
            data["router"] = vrf.name
            out.append(StaticRouteIntent(**data))
    out.sort(key=lambda s: s.name)
    return out


def _build_irbs(nb: Any, interfaces: list[Any]) -> list[IrbInterfaceIntent]:
    """Reconstruct IrbInterfaceIntent objects from native NetBox interfaces.

    A single logical IRB (e.g. ``irb-v10``) appears once per leaf where
    its VRF is deployed. We group by ``name`` and pick one representative
    row — every row shares the same IRB config — then read attached IPs
    for shape detection (legacy ``ipv4`` string vs dual-stack list).
    """
    irb_ifs = [i for i in interfaces if _cf(i).get(CF_IFACE_ROLE) == "irb"]
    by_name: dict[str, list[Any]] = {}
    for iface in irb_ifs:
        by_name.setdefault(iface.name, []).append(iface)

    result: list[IrbInterfaceIntent] = []
    for name, instances in by_name.items():
        first = instances[0]
        cf = _cf(first)
        blob = cf.get(CF_IFACE_IRB_CONFIG) or {}
        if isinstance(blob, str):
            blob = json.loads(blob) if blob else {}

        vrf = getattr(first, "vrf", None)
        router_name = getattr(vrf, "name", None) or blob.get("router") or ""

        kwargs: dict[str, Any] = {
            "name": name,
            "description": getattr(first, "description", "") or "",
            "bridge_domain": cf.get(CF_IFACE_BRIDGE_DOMAIN) or "",
            "router": router_name,
            "ip_mtu": getattr(first, "mtu", None) or 1500,
        }
        # Hoist the blob-carried fields back onto the intent.
        for key in (
            "anycast_gw",
            "proxy_arp",
            "proxy_nd",
            "arp_timeout",
            "learn_unsolicited",
            "evpn_route_advertisement_type",
            "host_route_populate",
            "origin",
            "ipv4",
        ):
            if key in blob:
                kwargs[key] = blob[key]

        # Preserve the original shape of the IP addressing so round-trips
        # remain lossless: the writer stored whichever was populated.
        ip_addresses_raw = blob.get("ip_addresses") or []
        kwargs["ip_addresses"] = [
            IrbIpAddress(ipv4=a.get("ipv4"), ipv6=a.get("ipv6"))
            for a in ip_addresses_raw
        ]

        result.append(IrbInterfaceIntent(**kwargs))

    result.sort(key=lambda i: i.name)
    return result


def _build_routed_interfaces(interfaces: list[Any]) -> list[RoutedInterfaceIntent]:
    """Reconstruct RoutedInterfaceIntent from native virtual subinterfaces.

    Each routed subinterface is a single ``dcim.Interface`` row whose
    ``parent`` FK points back at the physical edge port. ``Interface.vrf``
    carries the router reference; ``Interface.mtu`` the IP MTU; the rest
    (vlan_id, arp_timeout, v4/v6 address shapes) lives in the
    ``CF_IFACE_ROUTED_CONFIG`` JSON custom field.
    """
    result: list[RoutedInterfaceIntent] = []
    for iface in interfaces:
        cf = _cf(iface)
        if cf.get(CF_IFACE_ROLE) != "routed":
            continue

        blob = cf.get(CF_IFACE_ROUTED_CONFIG) or {}
        if isinstance(blob, str):
            blob = json.loads(blob) if blob else {}

        parent = getattr(iface, "parent", None)
        device_name = getattr(getattr(iface, "device", None), "name", "")
        parent_name = getattr(parent, "name", None) or ""
        interface_ref = f"{device_name}-{parent_name}" if parent_name else ""

        vrf = getattr(iface, "vrf", None)
        router_name = getattr(vrf, "name", None) or ""

        kwargs: dict[str, Any] = {
            "name": iface.name,
            "interface": interface_ref,
            "router": router_name,
            "ip_mtu": getattr(iface, "mtu", None) or 1500,
        }
        for key in ("vlan_id", "arp_timeout", "ipv4_addresses", "ipv6_addresses"):
            if key in blob:
                kwargs[key] = blob[key]

        result.append(RoutedInterfaceIntent(**kwargs))

    result.sort(key=lambda r: r.name)
    return result
