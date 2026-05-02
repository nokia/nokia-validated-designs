"""
Schema constants shared by every NetBox-backed module (builder, setup,
seed, writer, reset).

Everything NVD-created in NetBox is scoped by two conventions:

- Every object is tagged ``NVD_MANAGED_TAG`` (slug ``nvd-managed``) so the
  reset script can find and remove exactly what we created without
  touching unrelated NetBox content.
- Every NVD-specific custom field name is prefixed ``nvd_`` so the reset
  script can identify and remove them in ``--scope all``.
"""

from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

NVD_MANAGED_TAG = "nvd-managed"
NVD_EDGE_TAG = "nvd-edge"
NVD_EXTRAS_TAG = "nvd-extras"
NVD_IRB_TAG = "nvd-irb"
NVD_ROUTED_TAG = "nvd-routed"

ALL_NVD_TAGS: list[tuple[str, str, str]] = [
    # (slug, name, description)
    (NVD_MANAGED_TAG, "NVD Managed", "Object created by Nokia Validated Design tooling"),
    (NVD_EDGE_TAG, "NVD Edge Interface", "Interface flagged as an edge (server-facing) port"),
    (NVD_EXTRAS_TAG, "NVD Extras", "Object belongs to the extras overlay"),
    (NVD_IRB_TAG, "NVD IRB Interface", "Virtual IRB interface binding a bridge domain to a VRF"),
    (NVD_ROUTED_TAG, "NVD Routed Subinterface", "L3 subinterface attached to a VRF on an edge port"),
]

# ---------------------------------------------------------------------------
# Custom field names
# ---------------------------------------------------------------------------

# Site
CF_SITE_DESIGN = "nvd_design"
CF_SITE_ENVIRONMENT = "nvd_environment"
CF_SITE_SPINE_ASN = "nvd_spine_asn"
CF_SITE_LEAF_ASN_START = "nvd_leaf_asn_start"
CF_SITE_SYSTEM0_PREFIX = "nvd_system0_prefix"
CF_SITE_MGMT_SUBNET = "nvd_mgmt_subnet"
CF_SITE_EDA_NAMESPACE = "nvd_eda_namespace"
CF_SITE_EDA_NODE_PROFILE = "nvd_eda_node_profile"
CF_SITE_CONFIG = "nvd_config"  # Complete FabricIntent-side data not mappable natively

CF_SITE_DEPLOYMENT_STATE = "nvd_deployment_state"
CF_SITE_LAST_DEPLOY_AT = "nvd_last_deploy_at"
CF_SITE_LAST_DEPLOY_TXID = "nvd_last_deploy_txid"
CF_SITE_LAST_DEPLOY_PHASE = "nvd_last_deploy_phase"
CF_SITE_LAST_DEPLOY_ERROR = "nvd_last_deploy_error"
CF_SITE_LAST_DEPLOY_SUMMARY = "nvd_last_deploy_summary"
CF_SITE_LAST_DEPLOY_DURATION = "nvd_last_deploy_duration"

# Device
CF_DEVICE_ASN = "nvd_asn"
CF_DEVICE_SYSTEM0_IPV4 = "nvd_system0_ipv4"
CF_DEVICE_SRL_VERSION = "nvd_srl_version"
CF_DEVICE_LABELS = "nvd_labels"
CF_DEVICE_UPLINK_INTERFACES = "nvd_uplink_interfaces"

# Interface
CF_IFACE_ROLE = "nvd_role"  # "edge" | "isl" | "mgmt" | "system0" | "lag" | "lag-member" | "irb" | "routed"
CF_IFACE_ENCAP = "nvd_encap"  # edge: "dot1q" | "null"
CF_IFACE_LABELS = "nvd_labels"
CF_IFACE_LACP = "nvd_lacp"  # LAG interfaces only — JSON with LacpConfig fields
CF_IFACE_AGGREGATE_ID = "nvd_aggregate_id"
CF_IFACE_LAG_MODE = "nvd_lag_mode"  # "all-active" | "port-active"
CF_IFACE_LAG_MIN_LINKS = "nvd_lag_min_links"
CF_IFACE_LAG_RELOAD_DELAY = "nvd_lag_reload_delay"
CF_IFACE_LAG_REVERTIVE = "nvd_lag_revertive"
CF_IFACE_LAG_PREFERRED_ACTIVE = "nvd_lag_preferred_active"
CF_IFACE_LAG_STANDBY_SIGNALING = "nvd_lag_standby_signaling"
CF_IFACE_LACP_PORT_PRIORITY = "nvd_lacp_port_priority"
# IRB interfaces — bridge_domain ref reuses the same field name as on VLAN
# (shared by design), and nvd_irb_config carries the remaining IrbInterfaceIntent
# knobs (anycast_gw, proxy_*, arp_timeout, ip_mtu, learn_unsolicited,
# evpn_route_advertisement_type, host_route_populate, ip_addresses / ipv4, origin).
CF_IFACE_BRIDGE_DOMAIN = "nvd_bridge_domain_ref"
CF_IFACE_IRB_CONFIG = "nvd_irb_config"
# Routed subinterfaces — native dcim.Interface (type=virtual, parent=<edge>)
# with this JSON blob carrying the non-native settings (vlan_id,
# arp_timeout, ipv4_addresses / ipv6_addresses lists).
CF_IFACE_ROUTED_CONFIG = "nvd_routed_config"

# VLAN Group (bridge domain)
CF_VLANGROUP_VNI = "nvd_vni"
CF_VLANGROUP_EVI = "nvd_evi"
CF_VLANGROUP_BD_TYPE = "nvd_bd_type"
CF_VLANGROUP_MAC_LEARNING = "nvd_mac_learning"
CF_VLANGROUP_MAC_AGING = "nvd_mac_aging"
CF_VLANGROUP_MAC_DUPLICATION = "nvd_mac_duplication"
CF_VLANGROUP_ORIGIN = "nvd_origin"
# Optional BGP-EVPN route targets for the bridge domain. When unset, EDA
# and srl_builders fall back to ``target:1:<evi>``.
CF_VLANGROUP_EXPORT_TARGET = "nvd_export_target"
CF_VLANGROUP_IMPORT_TARGET = "nvd_import_target"

# VLAN
CF_VLAN_VLAN_ID_STR = "nvd_vlan_id_str"
CF_VLAN_INTERFACE_SELECTOR = "nvd_interface_selector"
CF_VLAN_BRIDGE_DOMAIN_REF = "nvd_bridge_domain_ref"

# Reserved untagged VLAN ("sentinel") — every nvd-managed VLAN Group has
# exactly one of these, created on demand by the reader pre-pass. It
# represents "no VLAN tag on the wire for this bridge domain"; the
# concrete VlanIntent.name is reconstructed on read from the parent
# VLAN Group's name suffix.
VLAN_UNTAGGED_SENTINEL_NAME = "untagged"
VLAN_UNTAGGED_SENTINEL_VID = 4094

# VRF (router)
CF_VRF_VNI = "nvd_vni"
CF_VRF_EVI = "nvd_evi"
CF_VRF_NODE_SELECTOR = "nvd_node_selector"
CF_VRF_SITE = "nvd_site"  # slug of the Site this VRF belongs to (explicit scoping)
# Static routes live on the VRF they belong to, not on the Site:
# list of dicts {name, nodes, prefixes, nexthop_group}. The ``router``
# ref from StaticRouteIntent is implicit (it's the owning VRF).
CF_VRF_STATIC_ROUTES = "nvd_static_routes"
# Optional BGP-EVPN route targets for the router. When unset, EDA
# and srl_builders fall back to ``target:1:<evi>``.
CF_VRF_EXPORT_TARGET = "nvd_export_target"
CF_VRF_IMPORT_TARGET = "nvd_import_target"

# Prefix
CF_PREFIX_ROLE = "nvd_prefix_role"  # "loopback" | "management"

# ---------------------------------------------------------------------------
# Selection-field choices
# ---------------------------------------------------------------------------

DESIGN_CHOICES = [
    "3-stage-evpn-vxlan",
    "collapsed-spine",
    "unconstrained-3-stage",
]
ENVIRONMENT_CHOICES = ["containerlab", "physical"]
DEPLOYMENT_STATE_CHOICES = ["draft", "staged", "deploying", "deployed", "failed"]
INTERFACE_ROLE_CHOICES = [
    "system0",
    "mgmt",
    "isl",
    "edge",
    "lag",
    "lag-member",
    "irb",
    "routed",
]

# ---------------------------------------------------------------------------
# Device Roles & Platform
# ---------------------------------------------------------------------------

NVD_DEVICE_ROLES: list[tuple[str, str, str]] = [
    # (slug, name, color)
    ("leaf", "Leaf", "2196f3"),  # blue
    ("spine", "Spine", "ff9800"),  # orange
    ("tor", "ToR", "9c27b0"),  # purple
    ("collapsed-spine", "Collapsed Spine", "4caf50"),  # green
]
NVD_MANUFACTURER = ("nokia", "Nokia")  # (slug, name)
NVD_PLATFORM = ("srlinux", "SR Linux", "srl")  # (slug, name, manufacturer_slug)

# Device Types seeded by setup (manufacturer slug == "nokia")
NVD_DEVICE_TYPES: list[tuple[str, str, str]] = [
    # (slug, model, part_number)
    ("7220-ixr-d3l", "7220 IXR-D3L", "7220 IXR-D3L"),
    ("7220-ixr-d2l", "7220 IXR-D2L", "7220 IXR-D2L"),
    ("7220-ixr-d4", "7220 IXR-D4", "7220 IXR-D4"),
    ("7220-ixr-d5", "7220 IXR-D5", "7220 IXR-D5"),
    ("7730-svr-lh10", "7730 SVR-LH10", "7730 SVR-LH10"),
    ("7730-svr-lh18", "7730 SVR-LH18", "7730 SVR-LH18"),
    ("7730-svr-lh10c", "7730 SVR-LH10C", "7730 SVR-LH10C"),
]

# ---------------------------------------------------------------------------
# Field specs used by setup.py (see NetBox DCIM/IPAM/EXTRAS custom-field API)
# ---------------------------------------------------------------------------

# object_types values are NetBox content-type slugs (app_label.model) in
# NetBox 4.x API.
OT_SITE = "dcim.site"
OT_DEVICE = "dcim.device"
OT_INTERFACE = "dcim.interface"
OT_VLANGROUP = "ipam.vlangroup"
OT_VLAN = "ipam.vlan"
OT_VRF = "ipam.vrf"
OT_PREFIX = "ipam.prefix"


CustomFieldSpec = dict[str, object]


def _cf(
    name: str,
    label: str,
    ftype: str,
    object_types: list[str],
    description: str = "",
    choices: list[str] | None = None,
    default: object = None,
    required: bool = False,
) -> CustomFieldSpec:
    spec: CustomFieldSpec = {
        "name": name,
        "label": label,
        "type": ftype,
        "object_types": object_types,
        "description": description,
        "required": required,
    }
    if choices is not None:
        spec["choices"] = choices
    if default is not None:
        spec["default"] = default
    return spec


CUSTOM_FIELDS: list[CustomFieldSpec] = [
    # ---------------- Site ----------------
    _cf(CF_SITE_DESIGN, "NVD Design", "select", [OT_SITE],
        description="Which NVD design this fabric implements",
        choices=DESIGN_CHOICES),
    _cf(CF_SITE_ENVIRONMENT, "NVD Environment", "select", [OT_SITE],
        description="Deployment environment", choices=ENVIRONMENT_CHOICES,
        default="containerlab"),
    _cf(CF_SITE_SPINE_ASN, "Spine ASN", "integer", [OT_SITE]),
    _cf(CF_SITE_LEAF_ASN_START, "Leaf ASN start", "integer", [OT_SITE]),
    _cf(CF_SITE_SYSTEM0_PREFIX, "System0 prefix", "text", [OT_SITE]),
    _cf(CF_SITE_MGMT_SUBNET, "Management subnet", "text", [OT_SITE]),
    _cf(CF_SITE_EDA_NAMESPACE, "EDA namespace", "text", [OT_SITE], default="eda"),
    _cf(CF_SITE_EDA_NODE_PROFILE, "EDA NodeProfile", "text", [OT_SITE]),
    _cf(CF_SITE_CONFIG, "NVD FabricIntent JSON", "json", [OT_SITE],
        description="Serialised FabricIntent fields not mapped natively"),
    # Lifecycle
    _cf(CF_SITE_DEPLOYMENT_STATE, "Deployment state", "select", [OT_SITE],
        choices=DEPLOYMENT_STATE_CHOICES, default="draft"),
    _cf(CF_SITE_LAST_DEPLOY_AT, "Last deploy at", "datetime", [OT_SITE]),
    _cf(CF_SITE_LAST_DEPLOY_TXID, "Last deploy txid", "text", [OT_SITE]),
    _cf(CF_SITE_LAST_DEPLOY_PHASE, "Last deploy phase", "text", [OT_SITE]),
    _cf(CF_SITE_LAST_DEPLOY_ERROR, "Last deploy error", "longtext", [OT_SITE]),
    _cf(CF_SITE_LAST_DEPLOY_SUMMARY, "Last deploy summary", "text", [OT_SITE]),
    _cf(CF_SITE_LAST_DEPLOY_DURATION, "Last deploy duration (s)", "integer", [OT_SITE]),
    # ---------------- Device ----------------
    _cf(CF_DEVICE_ASN, "BGP ASN", "integer", [OT_DEVICE]),
    _cf(CF_DEVICE_SYSTEM0_IPV4, "System0 IPv4", "text", [OT_DEVICE]),
    _cf(CF_DEVICE_SRL_VERSION, "SR Linux version", "text", [OT_DEVICE]),
    _cf(CF_DEVICE_LABELS, "NVD labels", "json", [OT_DEVICE]),
    _cf(CF_DEVICE_UPLINK_INTERFACES, "NVD uplink interfaces", "json", [OT_DEVICE]),
    # ---------------- Interface ----------------
    _cf(CF_IFACE_ROLE, "NVD interface role", "select", [OT_INTERFACE],
        choices=INTERFACE_ROLE_CHOICES),
    _cf(CF_IFACE_ENCAP, "Encap", "text", [OT_INTERFACE],
        description="[DEPRECATED] Use the native 802.1Q Mode field "
        "(access -> null, tagged -> dot1q). Read only as fallback when "
        "Interface.mode is unset."),
    _cf(CF_IFACE_LABELS, "NVD labels", "json", [OT_INTERFACE],
        description="Tag-like list of labels. ``k=v`` for values, bare "
        "``k`` when the value is ``enabled``. ``eda.nokia.com/role=<X>`` "
        "is elided when X equals the interface's native ``nvd_role``."),
    _cf(CF_IFACE_LACP, "LACP config", "json", [OT_INTERFACE]),
    _cf(CF_IFACE_AGGREGATE_ID, "LAG aggregate id", "text", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_MODE, "LAG multihoming mode", "text", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_MIN_LINKS, "LAG min links", "integer", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_RELOAD_DELAY, "LAG reload delay timer", "integer", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_REVERTIVE, "LAG revertive", "boolean", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_PREFERRED_ACTIVE, "LAG preferred active node", "text", [OT_INTERFACE]),
    _cf(CF_IFACE_LAG_STANDBY_SIGNALING, "LAG standby signaling", "text", [OT_INTERFACE]),
    _cf(CF_IFACE_LACP_PORT_PRIORITY, "LACP port priority", "integer", [OT_INTERFACE]),
    # IRB-specific interface fields
    _cf(CF_IFACE_IRB_CONFIG, "IRB config", "json", [OT_INTERFACE],
        description="IRB settings: anycast_gw, proxy_*, arp_timeout, ip_mtu, "
        "learn_unsolicited, evpn_route_advertisement_type, host_route_populate, "
        "ip_addresses / ipv4, origin"),
    _cf(CF_IFACE_ROUTED_CONFIG, "Routed subinterface config", "json", [OT_INTERFACE],
        description="Routed subinterface settings: vlan_id, arp_timeout, "
        "ipv4_addresses, ipv6_addresses"),
    # ---------------- VLAN Group ----------------
    _cf(CF_VLANGROUP_VNI, "VXLAN VNI", "integer", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_EVI, "EVPN EVI", "integer", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_BD_TYPE, "Bridge domain type", "text", [OT_VLANGROUP],
        default="EVPNVXLAN"),
    _cf(CF_VLANGROUP_MAC_LEARNING, "MAC learning", "boolean", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_MAC_AGING, "MAC aging (s)", "integer", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_MAC_DUPLICATION, "MAC duplication settings", "json", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_ORIGIN, "Bridge domain origin", "text", [OT_VLANGROUP]),
    _cf(CF_VLANGROUP_EXPORT_TARGET, "Export RT", "text", [OT_VLANGROUP],
        description="Optional BGP-EVPN export route target in 'target:N:N' "
        "format; overrides the default 'target:1:<evi>'."),
    _cf(CF_VLANGROUP_IMPORT_TARGET, "Import RT", "text", [OT_VLANGROUP],
        description="Optional BGP-EVPN import route target in 'target:N:N' "
        "format; overrides the default 'target:1:<evi>'."),
    # ---------------- VLAN ----------------
    _cf(CF_VLAN_VLAN_ID_STR, "VLAN id (string)", "text", [OT_VLAN],
        description=(
            "Authoritative encap id: a numeric VLAN id or 'untagged'. "
            "For 'untagged', the native vid is stored as the sentinel "
            "4094 (NetBox requires vid in 1..4094) — use THIS field, "
            "not vid, when reading."
        )),
    _cf(CF_VLAN_INTERFACE_SELECTOR, "Interface selector", "json", [OT_VLAN]),
    _cf(CF_VLAN_BRIDGE_DOMAIN_REF, "Bridge domain name", "text", [OT_VLAN, OT_INTERFACE],
        description="Name of the BridgeDomainIntent this VLAN or IRB belongs to"),
    # ---------------- VRF ----------------
    _cf(CF_VRF_VNI, "VXLAN VNI", "integer", [OT_VRF]),
    _cf(CF_VRF_EVI, "EVPN EVI", "integer", [OT_VRF]),
    _cf(CF_VRF_NODE_SELECTOR, "Node selector", "json", [OT_VRF]),
    _cf(CF_VRF_SITE, "NVD site slug", "text", [OT_VRF],
        description="Slug of the Site this VRF belongs to — scopes a globally-"
        "namespaced NetBox VRF back to a specific NVD fabric"),
    _cf(CF_VRF_STATIC_ROUTES, "Static routes", "json", [OT_VRF],
        description="List of StaticRouteIntent dicts (name, nodes, prefixes, "
        "nexthop_group) — the owning VRF is implicit"),
    _cf(CF_VRF_EXPORT_TARGET, "Export RT", "text", [OT_VRF],
        description="Optional BGP-EVPN export route target in 'target:N:N' "
        "format; overrides the default 'target:1:<evi>'."),
    _cf(CF_VRF_IMPORT_TARGET, "Import RT", "text", [OT_VRF],
        description="Optional BGP-EVPN import route target in 'target:N:N' "
        "format; overrides the default 'target:1:<evi>'."),
    # ---------------- Prefix ----------------
    _cf(CF_PREFIX_ROLE, "NVD prefix role", "text", [OT_PREFIX],
        description="'loopback' for system0_prefix, 'management' for mgmt_subnet"),
]


# ---------------------------------------------------------------------------
# nvd_config (site JSON) keys
# ---------------------------------------------------------------------------

# Site nvd_config is now only a holding pen for ``credentials`` — every
# other non-native bundle has moved to a tagged Config Context scoped to
# the site. Credentials stay on the Site custom field because Config
# Contexts lack row-level ACLs and are readable by anyone who can view
# the device.
#
# Note: ``irb_interfaces``, ``routed_interfaces`` and ``static_routes``
# are not site-blob keys — they live on native rows / VRF CFs.
CFG_KEY_CREDENTIALS = "credentials"

# Keys that live inside the per-site Config Context ``data`` blob.
CCTX_KEY_CONFIGLETS = "configlets"
CCTX_KEY_DEFAULT_MTUS = "default_mtus"
CCTX_KEY_BANNERS = "banners"
CCTX_KEY_PREFIX_SETS = "prefix_sets"
CCTX_KEY_ROUTING_POLICIES = "routing_policies"
CCTX_KEY_FABRIC_EXPORT_POLICIES = "fabric_export_policies"
CCTX_KEY_FABRIC_IMPORT_POLICIES = "fabric_import_policies"
CCTX_KEY_EDA = "eda"
CCTX_KEY_BREAKOUTS = "breakouts"


def nvd_config_context_name(site_slug: str) -> str:
    """Canonical name for the single per-site NVD Config Context."""
    return f"nvd-{site_slug}-config"


# ---------------------------------------------------------------------------
# ``nvd_labels`` encoding
# ---------------------------------------------------------------------------
#
# Interface labels are stored as a flat JSON **list of strings** in the
# ``nvd_labels`` custom field — not a key/value dict — so they read like
# a native tag list in the NetBox UI.
#
# Encoding rules:
# - ``{k: "enabled"}`` collapses to the bare key ``"k"``. Most labels
#   from the design builder are of the form ``tagged-vXX=enabled`` /
#   ``untagged-vXX=enabled``, so this covers the common case.
# - ``{k: v}`` with ``v != "enabled"`` becomes ``"k=v"``.
# - ``{"eda.nokia.com/role": X}`` is dropped *only* when ``X`` equals
#   the interface's native role (``nvd_role`` custom field). That
#   removes the duplication between ``nvd_role=edge`` and the label
#   ``role=edge``. Meaningful role overrides — e.g. a LAG whose
#   functional role is ``edge`` even though ``nvd_role=lag``, or a
#   routed sub-interface carrying a specific policy name — are kept.
#
# The reader inverts the encoding and re-injects
# ``eda.nokia.com/role=<nvd_role>`` when the label is absent, so every
# intent round-trips losslessly.

_LABEL_TRUE_VALUE = "enabled"
_LABEL_ROLE_KEY = "eda.nokia.com/role"


def labels_dict_to_list(labels: dict[str, str], *, iface_role: str | None = None) -> list[str]:
    """Encode a labels dict as a tag-like list of strings.

    ``iface_role`` is the interface's native ``nvd_role`` — pass it so
    a redundant ``role=<iface_role>`` label can be stripped out.
    """
    out: list[str] = []
    for key in sorted(labels):
        value = labels[key]
        if iface_role is not None and key == _LABEL_ROLE_KEY and value == iface_role:
            continue
        if value == _LABEL_TRUE_VALUE:
            out.append(key)
        else:
            out.append(f"{key}={value}")
    return out


# ---------------------------------------------------------------------------
# VLAN-membership labels
# ---------------------------------------------------------------------------
#
# A subset of interface labels encode VLAN membership:
#
#     eda.nokia.com/tagged-v10      -> interface carries VLAN "tagged-v10" tagged
#     eda.nokia.com/untagged-v40    -> interface carries VLAN "untagged-v40" untagged
#
# These are promoted to the native NetBox interface VLAN fields
# (``mode``, ``tagged_vlans``, ``untagged_vlan``) when the referenced
# VLAN exists on the site; the underlying VLAN's ``name`` is exactly
# the tag suffix (``tagged-v10`` / ``untagged-v40``), so the mapping
# is direct. Labels whose VLAN isn't defined in the intent (e.g. a
# pure edge-port selector tag with no bridge domain) stay in
# ``nvd_labels`` as a fallback, so nothing is lost.

_VLAN_LABEL_PREFIX_TAGGED = "eda.nokia.com/tagged-"
_VLAN_LABEL_PREFIX_UNTAGGED = "eda.nokia.com/untagged-"


def vlan_label_key(vlan_name: str) -> str:
    """Return the labels-dict key for a given VLAN ``name``.

    ``name`` is expected to already be of the form ``tagged-vXX`` or
    ``untagged-vXX``; the helper just prefixes the EDA namespace.
    """
    return f"eda.nokia.com/{vlan_name}"


def parse_vlan_label(key: str) -> tuple[str, str] | None:
    """Parse a label key into ``(kind, vlan_name)`` or ``None``.

    ``kind`` is either ``"tagged"`` or ``"untagged"``; ``vlan_name`` is
    the VLAN name to look up in NetBox (e.g. ``"tagged-v10"``).
    Returns ``None`` for labels that aren't VLAN-membership labels.
    """
    if key.startswith(_VLAN_LABEL_PREFIX_TAGGED):
        suffix = key[len(_VLAN_LABEL_PREFIX_TAGGED):]
        return ("tagged", f"tagged-{suffix}")
    if key.startswith(_VLAN_LABEL_PREFIX_UNTAGGED):
        suffix = key[len(_VLAN_LABEL_PREFIX_UNTAGGED):]
        return ("untagged", f"untagged-{suffix}")
    return None


def labels_list_to_dict(labels: list[str] | dict[str, str], *, iface_role: str | None = None) -> dict[str, str]:
    """Decode a labels field — accepts both the new list encoding and
    the legacy dict encoding for backward compatibility.

    If the decoded dict has no ``eda.nokia.com/role`` entry and
    ``iface_role`` is provided, the role label is re-injected so the
    intent round-trip stays lossless (the writer strips it when it
    equals ``nvd_role``).
    """
    if isinstance(labels, dict):
        result = dict(labels)
    else:
        result = {}
        for entry in labels or []:
            if not isinstance(entry, str) or not entry:
                continue
            if "=" in entry:
                k, _, v = entry.partition("=")
                result[k] = v
            else:
                result[entry] = _LABEL_TRUE_VALUE

    if iface_role is not None and _LABEL_ROLE_KEY not in result:
        result[_LABEL_ROLE_KEY] = iface_role
    return result


# ---------------------------------------------------------------------------
# VLAN Group <-> VlanIntent name helpers
# ---------------------------------------------------------------------------
#
# A VLAN Group like ``macvrf-v10`` acts as the bridge-domain anchor; its
# reserved untagged VLAN (``name="untagged"`` / ``vid=4094``) is
# projected back to a VlanIntent of the form ``untagged-<suffix>`` where
# the suffix is the tail of the group name after the last ``-``. The
# same suffix is used by the reader to rebuild the EDA interface-
# selector label (``eda.nokia.com/untagged-v10=enabled``).


def group_vlan_suffix(group_name: str) -> str:
    """Return the VLAN-ID suffix of a VLAN Group name.

    ``macvrf-v10`` -> ``v10``. If the group name contains no ``-`` (or
    is empty) the full name is returned as a sensible fallback.
    """
    if not group_name:
        return ""
    if "-" not in group_name:
        return group_name
    return group_name.rsplit("-", 1)[1]


def untagged_vlan_intent_name(group_name: str) -> str:
    """Reconstruct the VlanIntent name for a group's reserved untagged VLAN.

    ``macvrf-v10`` -> ``untagged-v10``. Mirrors the naming convention
    the design builders produce for ``VlanIntent.name``.
    """
    return f"untagged-{group_vlan_suffix(group_name)}"


# ---------------------------------------------------------------------------
# 802.1Q Mode -> EDA encap
# ---------------------------------------------------------------------------
#
# NetBox's native ``dcim.Interface.mode`` field is the authoritative
# source for an edge interface's encapsulation. The EDA generator reads
# ``EdgeInterfaceIntent.encap`` (``"dot1q"`` or ``"null"``) verbatim
# into ``InterfaceSpec.encap_type``; this helper is the one-way bridge
# from the NetBox UI's mode dropdown to that field.

_MODE_TO_ENCAP = {
    "access": "null",
    "tagged": "dot1q",
    "tagged-all": "dot1q",
}


def iface_mode_to_encap(mode: str | None) -> str:
    """Translate NetBox ``Interface.mode`` to an EDA encap string.

    ``access`` -> ``null`` (no tag on the wire).
    ``tagged`` / ``tagged-all`` -> ``dot1q``.
    Anything else (``None``, unknown) -> ``dot1q`` (the historical
    default; matches routed / unset interfaces in existing designs).
    """
    if mode is None:
        return "dot1q"
    return _MODE_TO_ENCAP.get(mode, "dot1q")


# ---------------------------------------------------------------------------
# Source marker printed by deploy.py and parsed by the Custom Script
# ---------------------------------------------------------------------------

DEPLOY_SUMMARY_MARKER = "[NVD-DEPLOY-SUMMARY]"


InterfaceRole = Literal[
    "system0",
    "mgmt",
    "isl",
    "edge",
    "lag",
    "lag-member",
    "irb",
    "routed",
]
