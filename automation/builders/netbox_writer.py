"""
Write a ``FabricIntent`` into NetBox. Shared by every seed script
(``netbox_seed_*``) and used by tests that populate a fake NetBox with
pynetbox-style mocks.

Conventions:

- Every object created by this module is tagged ``nvd-managed`` so the
  reset script can remove them safely.
- Every object is re-used if it already exists (``get`` by slug/name/id
  before ``create``) so seeds are safe to re-run.

Writing is ordered to satisfy foreign-key dependencies:
    site → device-types → device-roles → platform (already created by
    setup) → devices → interfaces → IP addresses → primary_ip → cables
    → VRFs → VLAN groups → VLANs.

The JSON-blob fields (``nvd_config`` on the site) are overwritten whole
with the current intent snapshot so re-running a seed produces a clean
state even if fields were removed between runs.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

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
    NVD_DEVICE_TYPES,
    NVD_EDGE_TAG,
    NVD_IRB_TAG,
    NVD_MANAGED_TAG,
    NVD_ROUTED_TAG,
    VLAN_UNTAGGED_SENTINEL_NAME,
    VLAN_UNTAGGED_SENTINEL_VID,
    group_vlan_suffix,
    labels_dict_to_list,
    nvd_config_context_name,
    parse_vlan_label,
)
from automation.core.models import (
    FabricIntent,
    IrbInterfaceIntent,
    NodeIntent,
    RoutedInterfaceIntent,
    RouterIntent,
)
from automation.core.selectors import node_matches_selector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write(nb: Any, intent: FabricIntent) -> dict[str, int]:
    """
    Write ``intent`` into the NetBox instance ``nb``. Returns a small
    dictionary of counts for logging.
    """
    counts: dict[str, int] = {}

    site = _ensure_site(nb, intent)
    counts["sites"] = 1

    counts["prefixes"] = _ensure_prefixes(nb, site, intent)

    device_by_name = _ensure_devices(nb, site, intent)
    counts["devices"] = len(device_by_name)

    # VLANs must exist before interfaces so edge / LAG interfaces can
    # reference them natively via ``untagged_vlan`` / ``tagged_vlans``.
    counts["vrfs"] = _ensure_vrfs(nb, site, intent)
    counts["vlan_groups"], counts["vlans"] = _ensure_vlans(nb, site, intent)

    counts["interfaces"] = _ensure_interfaces(nb, site, intent, device_by_name)
    counts["cables"] = _ensure_cables(nb, intent, device_by_name)

    # IRB interfaces live on every device where their VRF is deployed,
    # so they need ``device_by_name`` and must come after VRFs.
    counts["irbs"] = _ensure_irbs(nb, site, intent, device_by_name)
    # Routed subinterfaces sit underneath an existing edge interface via
    # the native ``Interface.parent`` FK; they must come after edge
    # interfaces and VRFs.
    counts["routed_interfaces"] = _ensure_routed_interfaces(
        nb, site, intent, device_by_name
    )

    # Credentials on the site CF, everything else in a Config Context.
    _write_site_config_blob(site, intent)
    counts["config_contexts"] = _ensure_config_context(nb, site, intent)
    return counts


# ---------------------------------------------------------------------------
# Helpers: tags, lookups
# ---------------------------------------------------------------------------


def _tag_slugs(*extra: str) -> list[dict[str, str]]:
    return [{"slug": NVD_MANAGED_TAG}] + [{"slug": s} for s in extra]


def _ensure_tag_refs(nb: Any, obj: Any, *slugs: str) -> None:
    """Make sure obj's tags include every slug in slugs."""
    wanted = set(slugs)
    current = {getattr(t, "slug", None) for t in getattr(obj, "tags", []) or []}
    if wanted.issubset(current):
        return
    merged = sorted(current | wanted)
    obj.update({"tags": [{"slug": s} for s in merged if s]})


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------


def _ensure_site(nb: Any, intent: FabricIntent) -> Any:
    slug = intent.fabric_name
    existing = nb.dcim.sites.get(slug=slug)
    payload: dict[str, Any] = {
        "name": slug,
        "slug": slug,
        "status": "active",
        "tags": _tag_slugs(),
        "custom_fields": {
            CF_SITE_DESIGN: intent.design,
            CF_SITE_ENVIRONMENT: intent.environment,
            CF_SITE_SPINE_ASN: intent.spine_asn,
            CF_SITE_LEAF_ASN_START: intent.leaf_asn_start,
            CF_SITE_SYSTEM0_PREFIX: intent.system0_prefix,
            CF_SITE_MGMT_SUBNET: intent.mgmt_subnet,
            CF_SITE_EDA_NAMESPACE: intent.eda.namespace,
            CF_SITE_EDA_NODE_PROFILE: intent.eda.node_profile,
        },
    }
    if existing is None:
        site = nb.dcim.sites.create(**payload)
        logger.info("  created site %s", slug)
        return site
    existing.update(payload)
    logger.debug("  updated site %s", slug)
    return existing


def _write_site_config_blob(site: Any, intent: FabricIntent) -> None:
    """Keep credentials on the site CF; everything else is a Config Context.

    Credentials stay on ``nvd_config`` because NetBox Config Contexts
    don't support row-level ACLs — they're readable by anyone with
    ``view_device`` on a scoped device. The rest of the non-native bundle
    moves to a per-site Config Context (see ``_ensure_config_context``).
    """
    dump = intent.model_dump(mode="json")
    blob: dict[str, Any] = {
        CFG_KEY_CREDENTIALS: dump.get("credentials", {}),
    }
    # Merge with whatever is currently on the site so we don't clobber
    # the identity fields (nvd_design, spine_asn, ...) that ``_ensure_site``
    # just set, or deployment-state fields set by the Custom Script.
    current = dict(getattr(site, "custom_fields", {}) or {})
    current[CF_SITE_CONFIG] = blob
    site.update({"custom_fields": current})
    logger.debug("  wrote nvd_config (credentials only) to site %s", intent.fabric_name)


# ---------------------------------------------------------------------------
# Config Context — non-native site-wide bundle
# ---------------------------------------------------------------------------


def _ensure_config_context(nb: Any, site: Any, intent: FabricIntent) -> int:
    """Upsert the single per-site Config Context ``nvd-<slug>-config``.

    Everything that previously lived in the big ``nvd_config`` blob and
    isn't credentials lands here. One context per site keeps merging
    deterministic (the reader doesn't rely on NetBox's weight-based
    render) and keeps the operator UI tidy (one row per fabric). Fans
    out to role/tag-scoped contexts is possible later without breaking
    the reader — it already shallow-merges whatever it finds.
    """
    dump = intent.model_dump(mode="json")
    data: dict[str, Any] = {
        CCTX_KEY_CONFIGLETS: dump.get("configlets", []),
        CCTX_KEY_DEFAULT_MTUS: dump.get("default_mtus", []),
        CCTX_KEY_BANNERS: dump.get("banners", []),
        CCTX_KEY_PREFIX_SETS: dump.get("prefix_sets", []),
        CCTX_KEY_ROUTING_POLICIES: dump.get("routing_policies", []),
        CCTX_KEY_FABRIC_EXPORT_POLICIES: dump.get("fabric_export_policies", []),
        CCTX_KEY_FABRIC_IMPORT_POLICIES: dump.get("fabric_import_policies", []),
        CCTX_KEY_BREAKOUTS: dump.get("breakouts", []),
        CCTX_KEY_EDA: dump.get("eda", {}),
    }

    name = nvd_config_context_name(site.slug)
    payload = {
        "name": name,
        "weight": 1000,
        "is_active": True,
        "description": f"NVD non-native intent bundle for site '{site.slug}'",
        "sites": [site.id],
        # ConfigContext serialises tags as bare slug strings, not
        # ``{"slug": ...}`` dicts like the rest of the API.
        "tags": [NVD_MANAGED_TAG],
        "data": data,
    }

    existing = nb.extras.config_contexts.get(name=name)
    if existing is None:
        nb.extras.config_contexts.create(**payload)
        logger.info("  created config context %s", name)
        return 1
    existing.update(payload)
    logger.debug("  updated config context %s", name)
    return 1


# ---------------------------------------------------------------------------
# Prefixes
# ---------------------------------------------------------------------------


def _ensure_prefixes(nb: Any, site: Any, intent: FabricIntent) -> int:
    created = 0
    if intent.system0_prefix:
        created += _ensure_prefix(nb, site, intent.system0_prefix, role="loopback")
    if intent.mgmt_subnet:
        created += _ensure_prefix(nb, site, intent.mgmt_subnet, role="management")
    return created


def _ensure_prefix(nb: Any, site: Any, cidr: str, *, role: str) -> int:
    # Lookup order: site-scoped → global (covers a prior partial run that
    # created the prefix without a site).
    existing = nb.ipam.prefixes.get(prefix=cidr, site_id=site.id)
    if existing is None:
        existing = nb.ipam.prefixes.get(prefix=cidr)
    if existing is not None:
        existing.update({
            "site": site.id,
            "custom_fields": {CF_PREFIX_ROLE: role},
            "tags": _tag_slugs(),
        })
        return 0
    nb.ipam.prefixes.create(
        prefix=cidr,
        site=site.id,
        status="active",
        tags=_tag_slugs(),
        custom_fields={CF_PREFIX_ROLE: role},
    )
    logger.info("  created prefix %s (%s) on %s", cidr, role, site.slug)
    return 1


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def _device_type_id(nb: Any, model: str) -> int:
    """Look up a device type by its model name (e.g. '7220 IXR-D3L')."""
    # NetBox allows looking up device types by ``model``.
    dt = nb.dcim.device_types.get(model=model)
    if dt is not None:
        return dt.id
    # fallback: iterate known seeds (slugify)
    for slug, m, _ in NVD_DEVICE_TYPES:
        if m == model:
            dt = nb.dcim.device_types.get(slug=slug)
            if dt is not None:
                return dt.id
    raise RuntimeError(
        f"Device type '{model}' not found in NetBox. Run "
        f"`python -m automation.builders.netbox_setup` first to create the "
        f"standard device types, or add the model to NVD_DEVICE_TYPES."
    )


def _role_id(nb: Any, slug: str) -> int:
    role = nb.dcim.device_roles.get(slug=slug)
    if role is None:
        raise RuntimeError(f"device role '{slug}' not found — run netbox_setup first")
    return role.id


def _platform_id(nb: Any, slug: str = "srlinux") -> int:
    plat = nb.dcim.platforms.get(slug=slug)
    if plat is None:
        raise RuntimeError(f"platform '{slug}' not found — run netbox_setup first")
    return plat.id


def _ensure_devices(
    nb: Any, site: Any, intent: FabricIntent
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for node in intent.nodes:
        dev = _ensure_device(nb, site, node)
        out[node.name] = dev
    return out


def _ensure_device(nb: Any, site: Any, node: Any) -> Any:
    existing = nb.dcim.devices.get(name=node.name, site_id=site.id)
    payload: dict[str, Any] = {
        "name": node.name,
        "site": site.id,
        "role": _role_id(nb, node.role),
        "device_type": _device_type_id(nb, node.platform),
        "platform": _platform_id(nb),
        "status": "active",
        "tags": _tag_slugs(),
        "custom_fields": {
            CF_DEVICE_ASN: node.asn,
            CF_DEVICE_SYSTEM0_IPV4: node.system0_ipv4,
            CF_DEVICE_SRL_VERSION: node.version,
            CF_DEVICE_LABELS: dict(node.labels or {}),
            CF_DEVICE_UPLINK_INTERFACES: list(node.uplink_interfaces or []),
        },
    }
    if existing is None:
        dev = nb.dcim.devices.create(**payload)
        logger.info("  created device %s", node.name)
    else:
        existing.update(payload)
        dev = existing
    return dev


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------


def _ensure_interfaces(
    nb: Any, site: Any, intent: FabricIntent, device_by_name: dict[str, Any]
) -> int:
    mgmt_prefix = intent.mgmt_subnet
    count = 0

    for node in intent.nodes:
        dev = device_by_name[node.name]
        # system0
        sys0 = _ensure_interface(
            nb,
            dev,
            name="system0",
            iface_type="virtual",
            role="system0",
        )
        if node.system0_ipv4:
            _ensure_ip(nb, sys0, node.system0_ipv4)
        count += 1

        # mgmt0
        if node.mgmt_ipv4:
            mgmt_if = _ensure_interface(
                nb,
                dev,
                name="mgmt0",
                iface_type="virtual",
                role="mgmt",
            )
            mgmt_cidr = _mgmt_cidr(node.mgmt_ipv4, mgmt_prefix)
            ip = _ensure_ip(nb, mgmt_if, mgmt_cidr)
            if dev.primary_ip4 is None or getattr(dev.primary_ip4, "id", None) != ip.id:
                dev.update({"primary_ip4": ip.id})
            count += 1

        # ISL uplink interfaces
        for uplink_name in node.uplink_interfaces:
            _ensure_interface(
                nb, dev, name=uplink_name, iface_type="25gbase-x-sfp28",
                role="isl",
            )
            count += 1

    # Edge interfaces
    vlan_lookup = _build_site_vlan_lookup(nb, site)
    for edge in intent.edge_interfaces:
        dev = device_by_name.get(edge.node)
        if dev is None:
            continue
        vlan_info, residual_labels = _split_vlan_labels(edge.labels or {}, vlan_lookup)
        mode = vlan_info["mode"] or _encap_to_mode(edge.encap)
        iface = _ensure_interface(
            nb,
            dev,
            name=edge.interface,
            iface_type="25gbase-x-sfp28",
            role="edge",
            custom_fields={
                CF_IFACE_LABELS: labels_dict_to_list(
                    residual_labels, iface_role="edge"
                ),
            },
            extra_tags=[NVD_EDGE_TAG],
            mode=mode,
            untagged_vlan=vlan_info["untagged_vlan_id"],
            tagged_vlans=vlan_info["tagged_vlan_ids"],
        )
        count += 1
        # Rename iface if NetBox key differs (store under edge.name for reconstruction)
        # The builder reconstructs the EdgeInterfaceIntent.name as "{node}-{interface}"
        # via the `-` check, so we don't need to store the full edge name separately.
        del iface  # silence unused-var warning

    # LAG interfaces + members
    for lag in intent.lags:
        # Derive parent node from the member list (LAG sits on a device in NetBox)
        if not lag.members:
            continue
        # In a multihoming LAG the same ``name`` is deployed on multiple
        # devices; NetBox needs a separate interface row per device.
        for member_device in {m.node for m in lag.members}:
            dev = device_by_name.get(member_device)
            if dev is None:
                continue
            vlan_info, residual_labels = _split_vlan_labels(
                lag.labels or {}, vlan_lookup
            )
            # LAGs are always trunks by design; default to tagged when no
            # VLAN labels pinned a mode.
            lag_mode = vlan_info["mode"] or "tagged"
            lag_if = _ensure_interface(
                nb,
                dev,
                name=lag.name,
                iface_type="lag",
                role="lag",
                custom_fields={
                    CF_IFACE_LACP: lag.lacp.model_dump(mode="json"),
                    CF_IFACE_LABELS: labels_dict_to_list(
                        residual_labels, iface_role="lag"
                    ),
                    CF_IFACE_LAG_MODE: lag.multihoming_mode,
                    CF_IFACE_LAG_MIN_LINKS: lag.min_links,
                    CF_IFACE_LAG_RELOAD_DELAY: lag.reload_delay_timer,
                    CF_IFACE_LAG_REVERTIVE: lag.revertive,
                    CF_IFACE_LAG_PREFERRED_ACTIVE: lag.preferred_active_node,
                    CF_IFACE_LAG_STANDBY_SIGNALING: lag.standby_signaling,
                },
                mode=lag_mode,
                untagged_vlan=vlan_info["untagged_vlan_id"],
                tagged_vlans=vlan_info["tagged_vlan_ids"],
            )
            count += 1
            # Members on this device
            for member in lag.members:
                if member.node != member_device:
                    continue
                member_if = _ensure_interface(
                    nb,
                    dev,
                    name=member.interface,
                    iface_type="25gbase-x-sfp28",
                    role="lag-member",
                    custom_fields={
                        CF_IFACE_AGGREGATE_ID: member.aggregate_id,
                        CF_IFACE_LACP_PORT_PRIORITY: member.lacp_port_priority,
                    },
                )
                # Parent LAG linkage
                if getattr(member_if, "lag", None) is None or getattr(member_if.lag, "id", None) != lag_if.id:
                    member_if.update({"lag": lag_if.id})
                count += 1

    return count


def _encap_to_mode(encap: str | None) -> str | None:
    """Map an ``EdgeInterfaceIntent.encap`` value to ``Interface.mode``.

    Inverse of ``iface_mode_to_encap``. Used when the edge has no VLAN
    labels to lean on for mode selection, so that the NetBox UI still
    shows the correct 802.1Q Mode after a seed/write.
    """
    if encap == "null":
        return "access"
    if encap == "dot1q":
        return "tagged"
    return None


def _build_site_vlan_lookup(nb: Any, site: Any) -> dict[str, int]:
    """Return a ``{label-name: vlan_id}`` map for every VLAN on the site.

    VLANs are referenced by labels using names of the form
    ``tagged-v10`` or ``untagged-v40``. For tagged VLANs the NetBox
    object is literally named that way. For untagged VLANs there is
    exactly one reserved VLAN named ``"untagged"`` per VLAN Group (see
    ``_ensure_vlans``), so we project it under the label-form derived
    from the group name (``untagged-<suffix>``). Built once per write
    so the edge / LAG loop doesn't hit NetBox for every label.
    """
    result: dict[str, int] = {}
    try:
        vlans = list(nb.ipam.vlans.filter(site_id=site.id, limit=0))
    except Exception:
        vlans = list(nb.ipam.vlans.filter(site=getattr(site, "slug", site.name)))
    for v in vlans:
        name = getattr(v, "name", None)
        vid = getattr(v, "id", None)
        if not name or vid is None:
            continue
        if name == VLAN_UNTAGGED_SENTINEL_NAME:
            group = getattr(v, "group", None)
            group_name = getattr(group, "name", None) if group is not None else None
            if group_name:
                result[f"untagged-{group_vlan_suffix(group_name)}"] = vid
            continue
        result[name] = vid
    return result


def _split_vlan_labels(
    labels: dict[str, str], vlan_lookup: dict[str, int]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split ``labels`` into (native VLAN attachment, residual labels).

    The returned attachment dict has shape::

        {"mode": "tagged" | "access" | None,
         "untagged_vlan_id": int | None,
         "tagged_vlan_ids": list[int]}

    Only labels that reference a VLAN present in ``vlan_lookup`` are
    consumed; everything else (including VLAN labels whose target isn't
    in the intent, e.g. an edge-selector tag with no bridge domain)
    stays in the residual dict so ``nvd_labels`` preserves them.
    """
    tagged_ids: list[int] = []
    untagged_id: int | None = None
    residual: dict[str, str] = {}
    # We track label-kind presence separately from resolved membership so
    # that an interface declaring a ``tagged-vN`` label for a VLAN that
    # doesn't exist as a NetBox object still ends up in ``tagged`` mode
    # (the native VLAN fields stay empty but the 802.1Q Mode is right).
    has_tagged_label = False
    has_untagged_label = False
    for key, value in labels.items():
        parsed = parse_vlan_label(key)
        if parsed is None:
            residual[key] = value
            continue
        kind, vlan_name = parsed
        if kind == "tagged":
            has_tagged_label = True
        else:
            has_untagged_label = True
        vid = vlan_lookup.get(vlan_name)
        if vid is None:
            residual[key] = value
            continue
        if kind == "tagged":
            tagged_ids.append(vid)
        else:
            untagged_id = vid

    # Any VLAN attachment — tagged or untagged — implies the parent must
    # have vlan-tagging enabled (NetBox ``mode=tagged``). An untagged VLAN
    # label does not mean ``access`` mode: it means a single sub-interface
    # carries untagged frames, while the parent stays tagged so it can also
    # host dot1q sub-interfaces alongside it. ``mode=access`` is only correct
    # for true L2-only ports with no sub-interface machinery, which we let
    # ``_encap_to_mode(edge.encap)`` decide downstream when no VLAN labels
    # are present at all.
    if has_tagged_label or has_untagged_label:
        mode: str | None = "tagged"
    else:
        mode = None

    return (
        {
            "mode": mode,
            "untagged_vlan_id": untagged_id,
            "tagged_vlan_ids": sorted(tagged_ids),
        },
        residual,
    )


def _ensure_interface(
    nb: Any,
    device: Any,
    *,
    name: str,
    iface_type: str,
    role: str,
    custom_fields: dict[str, Any] | None = None,
    extra_tags: list[str] | None = None,
    mode: str | None = None,
    untagged_vlan: int | None = None,
    tagged_vlans: list[int] | None = None,
) -> Any:
    iface = nb.dcim.interfaces.get(device_id=device.id, name=name)
    cf: dict[str, Any] = {CF_IFACE_ROLE: role}
    if custom_fields:
        cf.update(custom_fields)
    tag_slugs = [NVD_MANAGED_TAG]
    if extra_tags:
        tag_slugs.extend(extra_tags)
    payload: dict[str, Any] = {
        "device": device.id,
        "name": name,
        "type": iface_type,
        "enabled": True,
        "tags": [{"slug": s} for s in tag_slugs],
        "custom_fields": cf,
    }
    # Native VLAN fields. Always write them (possibly as None / []) so
    # re-runs can clear previously-set VLAN attachments when the intent
    # no longer references them.
    if mode is not None:
        payload["mode"] = mode
    else:
        payload["mode"] = None
    payload["untagged_vlan"] = untagged_vlan
    payload["tagged_vlans"] = list(tagged_vlans or [])
    if iface is None:
        return nb.dcim.interfaces.create(**payload)
    iface.update(payload)
    return iface


# ---------------------------------------------------------------------------
# IP addresses
# ---------------------------------------------------------------------------


def _ensure_ip(
    nb: Any,
    iface: Any,
    address: str,
    *,
    role: str | None = None,
    dedupe_globally: bool = True,
) -> Any:
    """Attach ``address`` to ``iface``.

    When ``dedupe_globally`` is True (legacy behaviour), the function also
    deduplicates across all interfaces by plain address. This is wrong for
    anycast IRB addresses — the same /24 gateway is configured on every
    participating leaf — so the IRB writer passes ``dedupe_globally=False``.
    """
    # Look up existing IP already bound to this interface with matching address
    existing = nb.ipam.ip_addresses.get(
        address=address,
        assigned_object_type="dcim.interface",
        assigned_object_id=iface.id,
    )
    payload: dict[str, Any] = {
        "address": address,
        "status": "active",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": iface.id,
        "tags": _tag_slugs(),
    }
    if role is not None:
        payload["role"] = role
    if existing is not None:
        existing.update(payload)
        return existing
    if dedupe_globally:
        # Also dedupe by address alone — NetBox doesn't forbid duplicate
        # addresses but we prefer to avoid them inside an NVD-managed fabric.
        ip = nb.ipam.ip_addresses.get(address=address)
        if ip is not None:
            ip.update(payload)
            return ip
    return nb.ipam.ip_addresses.create(**payload)


def _mgmt_cidr(ipv4: str, subnet: str) -> str:
    """Combine a bare ip with its /prefix."""
    if "/" in ipv4:
        return ipv4
    if subnet:
        try:
            net = ipaddress.IPv4Network(subnet, strict=False)
            return f"{ipv4}/{net.prefixlen}"
        except Exception:
            pass
    return f"{ipv4}/24"


# ---------------------------------------------------------------------------
# Cables
# ---------------------------------------------------------------------------


def _ensure_cables(
    nb: Any, intent: FabricIntent, device_by_name: dict[str, Any]
) -> int:
    count = 0
    for link in intent.links:
        local_dev = device_by_name.get(link.local_node)
        remote_dev = device_by_name.get(link.remote_node)
        if local_dev is None or remote_dev is None:
            continue
        local_if = nb.dcim.interfaces.get(device_id=local_dev.id, name=link.local_interface)
        remote_if = nb.dcim.interfaces.get(device_id=remote_dev.id, name=link.remote_interface)
        if local_if is None or remote_if is None:
            logger.warning(
                "  cable %s: missing interface %s.%s or %s.%s — skipping",
                link.name, link.local_node, link.local_interface,
                link.remote_node, link.remote_interface,
            )
            continue
        # Skip if either end already cabled to this peer
        if getattr(local_if, "cable", None) is not None:
            continue
        nb.dcim.cables.create(
            a_terminations=[{
                "object_type": "dcim.interface", "object_id": local_if.id,
            }],
            b_terminations=[{
                "object_type": "dcim.interface", "object_id": remote_if.id,
            }],
            status="connected",
            label=link.name,
            tags=_tag_slugs(),
        )
        logger.info(
            "  created cable %s.%s ↔ %s.%s",
            link.local_node, link.local_interface,
            link.remote_node, link.remote_interface,
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# VRFs
# ---------------------------------------------------------------------------


def _ensure_vrfs(nb: Any, site: Any, intent: FabricIntent) -> int:
    count = 0
    static_routes_by_router = _group_static_routes(intent)
    for router in intent.routers:
        existing = _lookup_vrf(nb, name=router.name, site_slug=site.slug)
        payload: dict[str, Any] = {
            "name": router.name,
            "tags": _tag_slugs(),
            "custom_fields": {
                CF_VRF_VNI: router.vni,
                CF_VRF_EVI: router.evi,
                CF_VRF_NODE_SELECTOR: list(router.node_selector or []),
                CF_VRF_SITE: site.slug,
                CF_VRF_STATIC_ROUTES: static_routes_by_router.get(router.name, []),
                CF_VRF_EXPORT_TARGET: router.export_target,
                CF_VRF_IMPORT_TARGET: router.import_target,
            },
        }
        if existing is None:
            nb.ipam.vrfs.create(**payload)
            count += 1
            logger.info("  created vrf %s (site=%s)", router.name, site.slug)
        else:
            existing.update(payload)
    return count


def _group_static_routes(intent: FabricIntent) -> dict[str, list[dict[str, Any]]]:
    """Group StaticRouteIntent dicts by owning VRF, stripping ``router``.

    The router reference becomes implicit — each entry lives on the VRF
    custom field of the router it belongs to, so storing the name twice
    would be redundant. Sorted by route name for deterministic output.
    """
    by_router: dict[str, list[dict[str, Any]]] = {}
    for sr in intent.static_routes:
        entry = sr.model_dump(mode="json")
        entry.pop("router", None)
        by_router.setdefault(sr.router, []).append(entry)
    for name in by_router:
        by_router[name].sort(key=lambda e: e.get("name", ""))
    return by_router


def _lookup_vrf(nb: Any, *, name: str, site_slug: str) -> Any:
    """Find the VRF belonging to ``site_slug`` with the given name.

    NetBox VRFs are globally unique by (name, rd) — not by site — so we
    layer an explicit site scope on top via the ``nvd_site`` custom field.
    This avoids cross-site collisions when two fabrics share a VRF name.
    """
    match = nb.ipam.vrfs.get(name=name, cf_nvd_site=site_slug)
    if match is not None:
        return match
    # Fallback: legacy VRFs created before CF_VRF_SITE existed (e.g. after
    # an upgrade mid-fabric). Find the one without any site tag.
    candidates = list(nb.ipam.vrfs.filter(name=name))
    for v in candidates:
        cf = getattr(v, "custom_fields", None) or {}
        if not cf.get(CF_VRF_SITE):
            return v
    return None


# ---------------------------------------------------------------------------
# IRB interfaces
# ---------------------------------------------------------------------------
#
# IRBs are native ``dcim.Interface`` rows (type=virtual) created on every
# device where the associated VRF is deployed. Per-IRB settings that don't
# fit any native NetBox field (anycast_gw, proxy_*, arp_timeout,
# learn_unsolicited, evpn_route_advertisement_type, host_route_populate,
# ip_addresses / ipv4, origin) are serialised into ``CF_IFACE_IRB_CONFIG``
# as JSON. ``interface.mtu``, ``interface.description`` and
# ``interface.vrf`` use the native NetBox fields.


def _ensure_irbs(
    nb: Any,
    site: Any,
    intent: FabricIntent,
    device_by_name: dict[str, Any],
) -> int:
    """Create one virtual Interface per (IRB × device where its VRF lives)."""
    count = 0
    router_by_name = {r.name: r for r in intent.routers}
    node_by_name = {n.name: n for n in intent.nodes}

    for irb in intent.irb_interfaces:
        router = router_by_name.get(irb.router)
        if router is None:
            logger.warning(
                "  irb %s references unknown router %s — skipping",
                irb.name, irb.router,
            )
            continue

        vrf_obj = _lookup_vrf(nb, name=irb.router, site_slug=site.slug)
        if vrf_obj is None:
            logger.warning(
                "  irb %s: VRF %s not found in NetBox — skipping",
                irb.name, irb.router,
            )
            continue

        devices = _devices_for_router(router, node_by_name, device_by_name)
        if not devices:
            logger.warning(
                "  irb %s: no devices match router %s node_selector=%s",
                irb.name, router.name, router.node_selector,
            )
            continue

        irb_blob = _irb_to_blob(irb)

        for device in devices:
            iface = _ensure_interface(
                nb,
                device,
                name=irb.name,
                iface_type="virtual",
                role="irb",
                extra_tags=[NVD_IRB_TAG],
                custom_fields={
                    CF_IFACE_BRIDGE_DOMAIN: irb.bridge_domain,
                    CF_IFACE_IRB_CONFIG: irb_blob,
                },
            )
            # Native NetBox fields that IRBs populate beyond the basic set.
            extras: dict[str, Any] = {}
            if irb.description and getattr(iface, "description", "") != irb.description:
                extras["description"] = irb.description
            if irb.ip_mtu and getattr(iface, "mtu", None) != irb.ip_mtu:
                extras["mtu"] = irb.ip_mtu
            current_vrf = getattr(iface, "vrf", None)
            if getattr(current_vrf, "id", None) != vrf_obj.id:
                extras["vrf"] = vrf_obj.id
            if extras:
                iface.update(extras)

            role = "anycast" if irb.anycast_gw else None
            for cidr in _irb_addresses(irb):
                _ensure_ip(nb, iface, cidr, role=role, dedupe_globally=False)

            count += 1

    return count


def _devices_for_router(
    router: RouterIntent,
    node_by_name: dict[str, NodeIntent],
    device_by_name: dict[str, Any],
) -> list[Any]:
    """Return the NetBox device objects where ``router`` should be present."""
    matched: list[Any] = []
    for node in node_by_name.values():
        if not node_matches_selector(node, router.node_selector):
            continue
        dev = device_by_name.get(node.name)
        if dev is not None:
            matched.append(dev)
    return matched


def _irb_to_blob(irb: IrbInterfaceIntent) -> dict[str, Any]:
    """Serialise all IrbInterfaceIntent fields that aren't native in NetBox."""
    dump = irb.model_dump(mode="json")
    # name, description, bridge_domain, router and ip_mtu are kept in
    # native NetBox fields / foreign keys, so we drop them from the blob
    # to keep it minimal. ip_addresses / ipv4 are kept so the reader can
    # distinguish legacy-shorthand vs dual-stack shape precisely.
    for key in ("name", "description", "bridge_domain", "router", "ip_mtu"):
        dump.pop(key, None)
    return dump


def _irb_addresses(irb: IrbInterfaceIntent) -> list[str]:
    """Return every IP CIDR that should be attached to an IRB interface."""
    out: list[str] = []
    if irb.ipv4:
        out.append(irb.ipv4)
    for entry in irb.ip_addresses:
        if entry.ipv4 and entry.ipv4.get("ip_prefix"):
            out.append(entry.ipv4["ip_prefix"])
        if entry.ipv6 and entry.ipv6.get("ip_prefix"):
            out.append(entry.ipv6["ip_prefix"])
    return out


# ---------------------------------------------------------------------------
# Routed subinterfaces
# ---------------------------------------------------------------------------
#
# A ``RoutedInterfaceIntent`` describes an L3 subinterface sitting on top
# of an existing edge port (e.g. ``ethernet-1-4`` with optional VLAN tag).
# We model it as a native virtual ``dcim.Interface`` row whose
# ``parent`` FK points at the edge port and whose ``vrf`` FK points at
# the router. Non-native fields (vlan_id, arp_timeout, v4/v6 shape) live
# in ``CF_IFACE_ROUTED_CONFIG``.


def _ensure_routed_interfaces(
    nb: Any,
    site: Any,
    intent: FabricIntent,
    device_by_name: dict[str, Any],
) -> int:
    count = 0
    edge_by_name = {e.name: e for e in intent.edge_interfaces}

    for ri in intent.routed_interfaces:
        edge = edge_by_name.get(ri.interface)
        if edge is None:
            logger.warning(
                "  routed_interface %s references unknown edge %s — skipping",
                ri.name, ri.interface,
            )
            continue

        device = device_by_name.get(edge.node)
        if device is None:
            logger.warning(
                "  routed_interface %s: device %s not found — skipping",
                ri.name, edge.node,
            )
            continue

        parent_if = nb.dcim.interfaces.get(device_id=device.id, name=edge.interface)
        if parent_if is None:
            logger.warning(
                "  routed_interface %s: parent port %s.%s not found — skipping",
                ri.name, edge.node, edge.interface,
            )
            continue

        vrf_obj = _lookup_vrf(nb, name=ri.router, site_slug=site.slug)
        if vrf_obj is None:
            logger.warning(
                "  routed_interface %s: VRF %s not found — skipping",
                ri.name, ri.router,
            )
            continue

        iface = _ensure_interface(
            nb,
            device,
            name=ri.name,
            iface_type="virtual",
            role="routed",
            extra_tags=[NVD_ROUTED_TAG],
            custom_fields={
                CF_IFACE_ROUTED_CONFIG: _routed_to_blob(ri),
            },
        )

        extras: dict[str, Any] = {}
        if getattr(getattr(iface, "parent", None), "id", None) != parent_if.id:
            extras["parent"] = parent_if.id
        if ri.ip_mtu and getattr(iface, "mtu", None) != ri.ip_mtu:
            extras["mtu"] = ri.ip_mtu
        current_vrf = getattr(iface, "vrf", None)
        if getattr(current_vrf, "id", None) != vrf_obj.id:
            extras["vrf"] = vrf_obj.id
        if extras:
            iface.update(extras)

        for cidr in _routed_addresses(ri):
            _ensure_ip(nb, iface, cidr, dedupe_globally=False)

        count += 1

    return count


def _routed_to_blob(ri: RoutedInterfaceIntent) -> dict[str, Any]:
    """Serialise non-native RoutedInterfaceIntent fields."""
    dump = ri.model_dump(mode="json")
    # name, interface, router and ip_mtu live on native fields / FKs.
    for key in ("name", "interface", "router", "ip_mtu"):
        dump.pop(key, None)
    return dump


def _routed_addresses(ri: RoutedInterfaceIntent) -> list[str]:
    """Return every IP CIDR that should be attached to a routed subinterface."""
    out: list[str] = []
    for entry in list(ri.ipv4_addresses) + list(ri.ipv6_addresses):
        prefix = entry.get("ipPrefix") or entry.get("ip_prefix")
        if prefix:
            out.append(prefix)
    return out


# ---------------------------------------------------------------------------
# VLAN groups / VLANs
# ---------------------------------------------------------------------------


def _ensure_vlans(
    nb: Any, site: Any, intent: FabricIntent
) -> tuple[int, int]:
    """Ensure every bridge-domain has a VLAN Group and the VLANs it needs.

    For each ``BridgeDomainIntent`` we create one VLAN Group. Inside each
    group we create **at most one tagged VLAN per VID** (``tagged-v10``
    with ``vid=10``) plus, if any ``VlanIntent`` for the BD has
    ``vlan_id=="untagged"``, exactly **one reserved untagged VLAN**
    named ``"untagged"`` with ``vid=4094`` (the sentinel). Multiple
    untagged VlanIntents pointing at the same BD collapse into this
    single NetBox object — the per-BD naming suffix only lives in the
    in-memory intent and is reconstructed on read from the group name.
    """
    vg_count = 0
    vg_by_name: dict[str, Any] = {}

    for bd in intent.bridge_domains:
        vg = _ensure_vlan_group(nb, site, bd)
        vg_by_name[bd.name] = vg
        vg_count += 1

    vlan_count = 0
    untagged_seen: set[str] = set()
    for vlan in intent.vlans:
        vg = vg_by_name.get(vlan.bridge_domain)
        if vg is None:
            logger.warning(
                "  vlan %s references unknown bridge_domain %s — skipping",
                vlan.name, vlan.bridge_domain,
            )
            continue

        is_untagged = vlan.vlan_id.lower() == "untagged"
        if is_untagged:
            if vlan.bridge_domain in untagged_seen:
                continue
            untagged_seen.add(vlan.bridge_domain)
            nb_name = VLAN_UNTAGGED_SENTINEL_NAME
            numeric = VLAN_UNTAGGED_SENTINEL_VID
            description = (
                f"Untagged binding for BD {vlan.bridge_domain}. "
                f"vid={numeric} is a SENTINEL (NetBox requires 1..4094); "
                f"carries no VLAN tag — see CF {CF_VLAN_VLAN_ID_STR}."
            )
            interface_selector: list[str] = []
        else:
            nb_name = vlan.name
            numeric = _vlan_numeric(vlan.vlan_id)
            description = (
                f"Tagged binding for BD {vlan.bridge_domain} "
                f"(VLAN id {vlan.vlan_id})."
            )
            interface_selector = list(vlan.interface_selector or [])

        existing = nb.ipam.vlans.get(name=nb_name, group_id=vg.id)
        payload: dict[str, Any] = {
            "name": nb_name,
            "vid": numeric,
            "group": vg.id,
            "site": site.id,
            "status": "active",
            "description": description,
            "tags": _tag_slugs(),
            "custom_fields": {
                CF_VLAN_VLAN_ID_STR: "untagged" if is_untagged else vlan.vlan_id,
                CF_VLAN_INTERFACE_SELECTOR: interface_selector,
                CF_VLAN_BRIDGE_DOMAIN_REF: vlan.bridge_domain,
            },
        }
        if existing is None:
            nb.ipam.vlans.create(**payload)
            vlan_count += 1
        else:
            existing.update(payload)
    return vg_count, vlan_count


def _ensure_vlan_group(nb: Any, site: Any, bd: Any) -> Any:
    existing = nb.ipam.vlan_groups.get(slug=bd.name)
    # NetBox 4.x VLAN groups use a scope (object_type + object_id) to
    # associate with a site.
    payload: dict[str, Any] = {
        "name": bd.name,
        "slug": bd.name,
        "scope_type": "dcim.site",
        "scope_id": site.id,
        "tags": _tag_slugs(),
        "custom_fields": {
            CF_VLANGROUP_VNI: bd.vni,
            CF_VLANGROUP_EVI: bd.evi,
            CF_VLANGROUP_BD_TYPE: bd.type,
            CF_VLANGROUP_MAC_LEARNING: bd.mac_learning,
            CF_VLANGROUP_MAC_AGING: bd.mac_aging,
            CF_VLANGROUP_MAC_DUPLICATION: bd.mac_duplication,
            CF_VLANGROUP_ORIGIN: bd.origin,
            CF_VLANGROUP_EXPORT_TARGET: bd.export_target,
            CF_VLANGROUP_IMPORT_TARGET: bd.import_target,
        },
    }
    if existing is None:
        return nb.ipam.vlan_groups.create(**payload)
    existing.update(payload)
    return existing


def _vlan_numeric(vlan_id: str) -> int:
    """Map our ``VlanIntent.vlan_id`` string to a NetBox-legal vid.

    SR Linux sub-interfaces may carry either a numeric tag or be
    explicitly untagged. NetBox on the other hand requires
    ``ipam.VLAN.vid`` to be a real 802.1Q id in the range 1..4094, so
    there is no legal way to represent "no tag" on the native field.

    We work around this with a **documented sentinel**: untagged
    VlanIntents are stored with ``vid=4094``, and the authoritative
    string value (``"untagged"`` or ``"10"`` etc.) lives in the
    ``CF_VLAN_VLAN_ID_STR`` custom field. The VLAN's
    ``description`` also carries an inline note so the sentinel is
    self-documenting in the NetBox UI. Readers MUST consult
    ``CF_VLAN_VLAN_ID_STR`` — never ``vid`` — when reconstructing the
    VlanIntent. See ``CF_VLAN_VLAN_ID_STR`` help text for details.
    """
    if vlan_id.lower() == "untagged":
        return 4094
    try:
        v = int(vlan_id)
        return v if 1 <= v <= 4094 else 4094
    except ValueError:
        return 4094
