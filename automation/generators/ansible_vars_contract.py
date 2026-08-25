"""
The variable contract for generated Ansible projects.

Until this module existed, the only description of what may appear in a
generated project's ``group_vars/`` and ``host_vars/`` was the set of
``hv.get(...)`` calls scattered through ``ansible_filter_plugins/srl_builders/``.
Operators doing Day-2 edits had no reference and no validation: because every
lookup is a ``.get()`` with a default, a misspelled key is silently ignored,
and with ``purge: true`` a silently-ignored service is not merely skipped — the
prune phase then deletes the corresponding config from the device.

The contract is declared once here, as :data:`VARS`, and rendered into two
complementary artifacts:

``ansible_vars_schema()``
    A JSON Schema written to ``<project>/schemas/ansible_vars_schema.json``.
    Each generated vars file carries a ``# yaml-language-server: $schema=``
    directive pointing at it, so editors flag unknown *top-level* keys and
    wrong types while the file is being edited. Also usable in CI.

``role_argument_specs()``
    Per-role ``meta/argument_specs.yml`` payloads. ansible-core validates
    these automatically when a role runs, which catches what the schema
    cannot: misspelled *nested* keys, missing required keys and wrong types,
    on the merged variable scope actually handed to the builders.

Neither mechanism alone is sufficient. Role argument specs ignore in-scope
variables a role does not declare, so a top-level typo (``irb_interfacs:``)
passes runtime validation untouched; the JSON Schema catches that but never
sees the merged group_vars + host_vars scope. Together they cover both.

Keeping one declaration for both artifacts is the point: a key added to the
builders needs a single edit here to become documented, editor-validated and
runtime-validated at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_ID = "https://nvd.nokia.com/schemas/ansible/vars"
SCHEMA_FILENAME = "ansible_vars_schema.json"

# Namespaces that may appear in a vars file without being declared below.
# ``ansible_*`` is Ansible's own connection/behavioural namespace; ``x_*`` is
# reserved for operator-defined helper variables, so that turning on
# ``additionalProperties: false`` does not make local conveniences unwritable.
PASSTHROUGH_PREFIXES = ("ansible_", "x_")

_INERT_NOTE = (
    "Accepted for round-tripping but NOT read by the SR Linux builders — the "
    "emitted device config hardcodes this. Editing it has no effect."
)


@dataclass(frozen=True)
class V:
    """One variable (or sub-key) in the Ansible vars contract.

    ``type`` uses Ansible's argument-spec vocabulary (``str``, ``int``,
    ``bool``, ``list``, ``dict``, ``raw``) so the argument-spec rendering is a
    direct copy and the JSON Schema rendering is the translated one.
    """

    name: str
    type: str
    doc: str
    required: bool = False
    elements: str | None = None
    options: tuple[V, ...] = ()
    choices: tuple[str, ...] = ()
    # JSON Schema type override, for keys that legitimately accept more than
    # one scalar type (``vlan_id`` is "10", 10 or "untagged").
    json_type: tuple[str, ...] | None = None
    # A dict whose keys are free-form (label maps, raw JSON-RPC payloads).
    open_dict: bool = False
    # False for keys the generator emits but no builder consumes.
    consumed: bool = True

    @property
    def description(self) -> str:
        return self.doc if self.consumed else f"{self.doc} {_INERT_NOTE}"


_JSON_TYPES = {
    "str": "string",
    "int": "integer",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def _labels(what: str) -> V:
    return V(
        "labels",
        "dict",
        f"Kubernetes-style label map on the {what}, matched at playbook time "
        "by the selectors in 'vlans' and 'routers'.",
        open_dict=True,
    )


# ---------------------------------------------------------------------------
# Reusable sub-structures
# ---------------------------------------------------------------------------

_LACP = V(
    "lacp",
    "dict",
    "LACP parameters for the aggregate.",
    options=(
        V("interval", "str", "LACP transmit interval.", choices=("fast", "slow")),
        V(
            "system_id_mac",
            "str",
            "LACP system ID. Also seeds the EVPN ethernet-segment ESI, so a "
            "LAG without this key gets no ethernet-segment at all.",
        ),
        V("system_priority", "int", "LACP system priority. Defaults to 32768."),
        V("admin_key", "int", "LACP admin key. Defaults to the aggregate id."),
    ),
)

_ACCESS = V(
    "access",
    "list",
    "Access interfaces bound into this bridge domain. Normally resolved at "
    "playbook time from 'vlans' + interface labels; set it explicitly only to "
    "bypass selector resolution.",
    elements="dict",
    options=(
        V("interface", "str", "Interface or LAG name, e.g. ethernet-1/3 or lag1.", required=True),
        V(
            "vlan",
            "raw",
            "VLAN id, or 'untagged'. Defaults to 'untagged'.",
            json_type=("string", "integer"),
        ),
    ),
)

_IRB_INLINE = V(
    "irb",
    "dict",
    "IRB attached to this bridge domain. Normally resolved at playbook time "
    "from 'irb_interfaces' plus the owning router's node_selector.",
    open_dict=True,
)

_MAC_DUPLICATION = V(
    "mac_duplication",
    "dict",
    "MAC duplication detection. Omit for the default (enabled, 5 moves in a "
    "3-minute window, hold-down 9, action StopLearning).",
    options=(
        V("enabled", "bool", "Enable duplicate-MAC detection."),
        V("hold_down_time", "int", "Hold-down time in minutes."),
        V("monitoring_window", "int", "Detection window in minutes."),
        V("num_moves", "int", "Moves within the window that trigger the action."),
        V(
            "action",
            "str",
            "Action on detection.",
            choices=("StopLearning", "Blackhole", "UseNetInstanceAction"),
        ),
    ),
)

_PREFIX_ENTRY = V(
    "prefixes",
    "list",
    "Prefixes in the set.",
    elements="dict",
    options=(
        V("ip_prefix", "str", "Prefix in CIDR notation.", required=True),
        V(
            "mask_length_range",
            "str",
            "Mask length range, e.g. '32..32'. Defaults to 'exact'.",
        ),
    ),
)

_STATEMENTS = V(
    "statements",
    "list",
    "Ordered policy statements.",
    elements="dict",
    options=(
        V("name", "raw", "Statement name. Rendered as a string.", required=True),
        V(
            "match",
            "dict",
            "Match conditions. An empty match matches everything.",
            options=(
                V("prefix_set", "str", "Name of a prefix-set defined in 'prefix_sets'."),
                V(
                    "protocol",
                    "str",
                    "Source protocol to match.",
                    choices=("local", "bgp", "aggregate", "bgp_evpn", "static"),
                ),
                V(
                    "bgp_evpn_route_types",
                    "list",
                    "EVPN route types to match. Listing several here expands "
                    "into one SR Linux statement per route type.",
                    elements="int",
                ),
            ),
        ),
        V(
            "action",
            "dict",
            "Action applied on match.",
            options=(
                V("result", "str", "Policy result. Defaults to accept.", choices=("accept", "reject")),
                V("set_local_preference", "int", "BGP local preference to set."),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

# Grouped only so the README and the role mapping can talk about coherent
# sections; the merged variable scope is flat.
GROUPS: dict[str, tuple[V, ...]] = {
    "fabric": (
        V(
            "fabric_name",
            "str",
            "Fabric name. Used to derive default BGP peer-group and "
            "routing-policy names when those are not set explicitly.",
        ),
        V(
            "system0_prefix",
            "str",
            "Loopback supernet. Used as the routing-policy prefix when "
            "'routing_policy.prefix_sets' is empty.",
        ),
        V(
            "purge",
            "bool",
            "Delete device config that is no longer in intent. Defaults to "
            "true. Note that a service dropped by a misspelled key counts as "
            "'no longer in intent' and will be removed from the device.",
        ),
        V(
            "confirm_timeout",
            "int",
            "Confirmed-commit window in seconds. 0 disables two-phase commit.",
        ),
    ),
    "node": (
        V(
            "node",
            "dict",
            "Identity of this device.",
            required=True,
            options=(
                V("hostname", "str", "Device hostname.", required=True),
                V(
                    "role",
                    "str",
                    "Fabric role, e.g. leaf or spine. Selects role-dependent "
                    "underlay and overlay behaviour.",
                    required=True,
                ),
                V("router_id", "str", "Router ID / system0 IPv4 address, no prefix length.", required=True),
                V("asn", "int", "Local BGP autonomous system number.", required=True),
                _labels("node"),
            ),
        ),
        V(
            "underlay_interfaces",
            "list",
            "Fabric-facing (ISL) interfaces. Each gets a .0 routed "
            "subinterface, a BFD session and a dynamic BGP neighbour.",
            elements="dict",
            options=(
                V(
                    "name",
                    "str",
                    "Interface name in SR Linux slash notation, e.g. "
                    "ethernet-1/49. Note this differs from the dash notation "
                    "used in the design inputs.",
                    required=True,
                ),
                V("peer_asn", "int", "ASN accepted from this link's dynamic neighbour.", required=True),
            ),
        ),
        V(
            "default_mtu",
            "dict",
            "System-wide default MTUs.",
            options=(
                V("interface_mtu", "int", "Default port MTU."),
                V("layer2_subif_mtu", "int", "Default L2 subinterface MTU."),
                V("layer3_mtu", "int", "Default IP MTU."),
            ),
        ),
    ),
    "protocols": (
        V(
            "bgp",
            "dict",
            "Shared BGP settings for the default network-instance.",
            options=(
                V("group_name", "str", "BGP peer-group name for fabric ISLs."),
                V(
                    "afi_safi",
                    "dict",
                    "Per-AFI/SAFI settings.",
                    options=(
                        V(
                            "evpn",
                            "dict",
                            "EVPN address family.",
                            options=(
                                V("multipath_max_paths", "int", "Maximum EVPN multipaths."),
                                V("inter_as_vpn", "bool", "Allow inter-AS VPN routes.", consumed=False),
                                V("rapid_update", "bool", "Enable rapid update.", consumed=False),
                            ),
                        ),
                        V(
                            "ipv4_unicast",
                            "dict",
                            "IPv4 unicast address family.",
                            options=(
                                V("multipath_max_paths", "int", "Maximum IPv4 multipaths."),
                                V("advertise_ipv6_next_hops", "bool", "Advertise IPv6 next hops.", consumed=False),
                                V("receive_ipv6_next_hops", "bool", "Accept IPv6 next hops.", consumed=False),
                                V("rapid_update", "bool", "Enable rapid update.", consumed=False),
                            ),
                        ),
                        V(
                            "ipv6_unicast",
                            "dict",
                            "IPv6 unicast address family.",
                            options=(
                                V("multipath_max_paths", "int", "Maximum IPv6 multipaths."),
                                V("rapid_update", "bool", "Enable rapid update.", consumed=False),
                            ),
                        ),
                    ),
                ),
                V(
                    "preference",
                    "dict",
                    "Route preference per peer type.",
                    consumed=False,
                    options=(
                        V("ebgp", "int", "eBGP preference.", consumed=False),
                        V("ibgp", "int", "iBGP preference.", consumed=False),
                    ),
                ),
                V(
                    "route_advertisement",
                    "dict",
                    "Route advertisement behaviour.",
                    consumed=False,
                    options=(
                        V("rapid_withdrawal", "bool", "Enable rapid withdrawal.", consumed=False),
                        V("wait_for_fib_install", "bool", "Wait for FIB install.", consumed=False),
                    ),
                ),
                V(
                    "ebgp_default_policy",
                    "dict",
                    "Default eBGP import/export posture.",
                    consumed=False,
                    options=(
                        V("import_reject_all", "bool", "Reject all on import by default.", consumed=False),
                        V("export_reject_all", "bool", "Reject all on export by default.", consumed=False),
                    ),
                ),
            ),
        ),
        V(
            "bfd",
            "dict",
            "BFD timers applied to every underlay .0 subinterface.",
            options=(
                V("desired_min_transmit_interval", "int", "Desired minimum transmit interval, microseconds."),
                V("required_min_receive", "int", "Required minimum receive interval, microseconds."),
                V("detection_multiplier", "int", "Detection multiplier."),
                V("min_echo_receive_interval", "int", "Minimum echo receive interval, microseconds."),
            ),
        ),
        V(
            "routing_policy",
            "dict",
            "Routing policy. When 'prefix_sets' and 'policies' are both empty "
            "the builders fall back to a hardcoded 3-stage EVPN policy built "
            "from 'prefix_set', 'prefix' and 'mask_length_range'.",
            options=(
                V("prefix_set", "str", "Name of the fabric prefix-set."),
                V("export_policy", "str", "Name of the ISL export policy."),
                V("import_policy", "str", "Name of the ISL import policy."),
                V("prefix", "str", "Fallback-only: prefix for the synthesized prefix-set."),
                V("mask_length_range", "str", "Fallback-only: mask length range, e.g. '32..32'."),
                V(
                    "prefix_sets",
                    "list",
                    "Prefix-set definitions.",
                    elements="dict",
                    options=(
                        V("name", "str", "Prefix-set name.", required=True),
                        _PREFIX_ENTRY,
                        V("namespace", "str", "EDA namespace.", consumed=False),
                    ),
                ),
                V(
                    "policies",
                    "list",
                    "Routing-policy definitions.",
                    elements="dict",
                    options=(
                        V("name", "str", "Policy name.", required=True),
                        V(
                            "default_action",
                            "str",
                            "Result for traffic matching no statement. Defaults to reject.",
                            choices=("accept", "reject"),
                        ),
                        _STATEMENTS,
                        V("namespace", "str", "EDA namespace.", consumed=False),
                    ),
                ),
            ),
        ),
    ),
    "services": (
        V(
            "bridge_domains",
            "list",
            "MAC-VRF definitions shared by every node in the group. A bridge "
            "domain only reaches a device if a VLAN selector binds one of its "
            "interfaces or an IRB places it there.",
            elements="dict",
            options=(
                V("name", "str", "Bridge domain (mac-vrf) name.", required=True),
                # Null for a SIMPLE (L2-only, node-local) bridge domain, which
                # gets no VXLAN binding. Ansible argument specs cannot express
                # "integer or null", so the tighter constraint lives only in
                # the JSON Schema and the spec falls back to raw.
                V(
                    "vni",
                    "raw",
                    "VXLAN VNI. Null for an L2-only bridge domain with no VXLAN binding.",
                    json_type=("integer", "null"),
                ),
                V(
                    "evi",
                    "raw",
                    "EVPN instance id. Null for an L2-only bridge domain.",
                    json_type=("integer", "null"),
                ),
                V("mac_learning", "bool", "Enable MAC learning. Defaults to true."),
                V("mac_aging", "int", "MAC aging time in seconds. Defaults to 300."),
                _MAC_DUPLICATION,
                V("export_target", "str", "Explicit export route-target."),
                V("import_target", "str", "Explicit import route-target."),
                _ACCESS,
                _IRB_INLINE,
                V("router", "str", "Owning IP-VRF. Normally resolved at playbook time."),
            ),
        ),
        V(
            "routers",
            "list",
            "IP-VRF definitions, placed by node_selector.",
            elements="dict",
            options=(
                V("name", "str", "Router (ip-vrf) name.", required=True),
                V("vni", "int", "VXLAN VNI.", required=True),
                V("evi", "int", "EVPN instance id."),
                V(
                    "node_selector",
                    "list",
                    "'key=value' selectors matched against node labels. Any "
                    "match places the router on the node.",
                    elements="str",
                ),
                V("export_target", "str", "Explicit export route-target."),
                V("import_target", "str", "Explicit import route-target."),
            ),
        ),
        V(
            "vlans",
            "list",
            "Bindings from bridge domains to interfaces, by interface label.",
            elements="dict",
            options=(
                V("name", "str", "Binding name.", required=True),
                V("bridge_domain", "str", "Target bridge domain.", required=True),
                V(
                    "vlan_id",
                    "raw",
                    "VLAN id, or 'untagged'.",
                    required=True,
                    json_type=("string", "integer"),
                ),
                V(
                    "interface_selector",
                    "list",
                    "'key=value' selectors matched against the labels of "
                    "'edge_interfaces' and 'lags'.",
                    elements="str",
                ),
            ),
        ),
        V(
            "edge_interfaces",
            "list",
            "Host-facing interfaces.",
            elements="dict",
            options=(
                V("name", "str", "Interface name in slash notation, e.g. ethernet-1/3.", required=True),
                V(
                    "encap",
                    "str",
                    "Set to 'dot1q' to enable VLAN tagging on the port.",
                    choices=("dot1q", "null"),
                ),
                _labels("interface"),
            ),
        ),
        V(
            "lags",
            "list",
            "Link aggregation groups and their EVPN ethernet-segments.",
            elements="dict",
            options=(
                V("name", "str", "LAG interface name, e.g. lag1.", required=True),
                V("description", "str", "Free-text description."),
                V(
                    "mode",
                    "str",
                    "Multi-homing mode. 'single-active' and 'port-active' both "
                    "render as single-active with preference-based DF election.",
                    choices=("all-active", "single-active", "port-active"),
                ),
                V("min_links", "int", "Minimum member links for the LAG to come up."),
                _LACP,
                V(
                    "fallback",
                    "dict",
                    "LACP fallback behaviour.",
                    options=(
                        V("mode", "str", "Fallback mode. Defaults to static."),
                        V("timeout", "int", "Fallback timeout in seconds. Defaults to 60."),
                    ),
                ),
                V("reload_delay_timer", "int", "Reload delay in seconds."),
                V("revertive", "bool", "Revertive DF election. False sets non-revertive."),
                V("df_preference", "int", "DF election preference for single-active mode."),
                V(
                    "members",
                    "list",
                    "Member ports.",
                    required=True,
                    elements="dict",
                    options=(
                        V("interface", "str", "Member interface in slash notation.", required=True),
                    ),
                ),
                _labels("LAG"),
                V(
                    "aggregate_id",
                    "raw",
                    "Aggregate id, carried as a string. The builders derive the "
                    "aggregate from 'name' instead.",
                    json_type=("string", "integer"),
                    consumed=False,
                ),
            ),
        ),
        V(
            "irb_interfaces",
            "list",
            "IRB (anycast gateway) interfaces for this node.",
            elements="dict",
            options=(
                V("bridge_domain", "str", "Bridge domain the IRB fronts.", required=True),
                V("router", "str", "IP-VRF the IRB belongs to.", required=True),
                V("ipv4", "str", "Gateway address in CIDR notation. Without it the IRB is skipped."),
                V("anycast_gw", "bool", "Enable anycast gateway."),
                V("proxy_arp", "bool", "Enable proxy ARP."),
                V("arp_timeout", "int", "ARP timeout in seconds. Defaults to 250."),
                V("ip_mtu", "int", "IP MTU. Defaults to 1500."),
                V("proxy_nd", "bool", "Enable proxy ND.", consumed=False),
                V("learn_unsolicited", "str", "Unsolicited neighbour learning mode.", consumed=False),
                V(
                    "evpn_route_advertisement_type",
                    "dict",
                    "ARP/ND EVPN advertisement selection.",
                    open_dict=True,
                    consumed=False,
                ),
                V(
                    "host_route_populate",
                    "raw",
                    "Host-route population selection: either a per-source map "
                    "or a single boolean.",
                    json_type=("object", "boolean"),
                    consumed=False,
                ),
            ),
        ),
        V(
            "routed_interfaces",
            "list",
            "Layer-3 (routed) subinterfaces on host-facing ports.",
            elements="dict",
            options=(
                V("name", "str", "Logical name. Used as the interface when 'interface' is absent.", required=True),
                V("interface", "str", "Physical interface in slash notation."),
                V("router", "str", "IP-VRF to bind the subinterface into.", required=True),
                V(
                    "vlan_id",
                    "raw",
                    "VLAN id for a tagged subinterface. Null or the string "
                    "'null' produces an untagged subinterface at index 4097.",
                    json_type=("string", "integer", "null"),
                ),
                V("ip_mtu", "int", "IP MTU. Defaults to 1500."),
                V("arp_timeout", "int", "ARP timeout in seconds. Defaults to 14400."),
                V(
                    "ipv4_addresses",
                    "list",
                    "IPv4 addresses on the subinterface.",
                    elements="dict",
                    options=(
                        V("ip_prefix", "str", "Address in CIDR notation.", required=True),
                        V("primary", "bool", "Mark as primary.", consumed=False),
                    ),
                ),
            ),
        ),
        V(
            "static_routes",
            "list",
            "Static routes with their next-hop groups.",
            elements="dict",
            options=(
                V("name", "str", "Route set name.", required=True),
                V("router", "str", "IP-VRF the routes belong to.", required=True),
                V("prefixes", "list", "Destination prefixes in CIDR notation.", elements="str", required=True),
                V(
                    "nexthop_group",
                    "dict",
                    "Next-hop group resolving these prefixes.",
                    required=True,
                    options=(
                        V("name", "str", "Next-hop group name.", required=True),
                        V(
                            "nexthops",
                            "list",
                            "Next hops in the group.",
                            elements="dict",
                            options=(
                                V("ip_address", "str", "Next-hop address.", required=True),
                                V("resolve", "bool", "Enable recursive resolution."),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        V(
            "event_handler",
            "dict",
            "SR Linux event-handler instances.",
            options=(
                V(
                    "node_isolation",
                    "dict",
                    "Bring down access links when the node loses its overlay "
                    "BGP sessions. Requires node-isolation.py on the device.",
                    options=(
                        V("down_links", "list", "Interfaces to bring down on isolation.", elements="str"),
                        V("hold_down_time", "int", "Hold-down in milliseconds. Defaults to 20000."),
                        V("required_bgp_sessions", "int", "Sessions below which the node is isolated. Defaults to 1."),
                    ),
                ),
            ),
        ),
    ),
    "overrides": (
        V(
            "config_overrides",
            "list",
            "Raw SR Linux JSON-RPC updates applied after every phase. The "
            "escape hatch for anything the builders do not model; contents are "
            "passed through unvalidated.",
            elements="dict",
            options=(
                V("path", "str", "JSON-RPC path, e.g. /system/information.", required=True),
                V("value", "raw", "Payload to set at that path.", required=True),
            ),
        ),
    ),
}

VARS: tuple[V, ...] = tuple(v for group in GROUPS.values() for v in group)

_BY_NAME: dict[str, V] = {v.name: v for v in VARS}


# ---------------------------------------------------------------------------
# JSON Schema rendering
# ---------------------------------------------------------------------------


def _json_node(var: V) -> dict[str, Any]:
    """Render one variable as a JSON Schema node."""
    node: dict[str, Any] = {"description": var.description}

    if var.json_type is not None:
        node["type"] = list(var.json_type)
    elif var.type != "raw":
        node["type"] = _JSON_TYPES[var.type]

    if var.choices:
        node["enum"] = list(var.choices)

    if var.type == "dict":
        if var.open_dict:
            node["additionalProperties"] = True
        else:
            node["properties"] = {o.name: _json_node(o) for o in var.options}
            node["additionalProperties"] = False
            required = [o.name for o in var.options if o.required]
            if required:
                node["required"] = required

    if var.type == "list":
        if var.elements == "dict":
            items: dict[str, Any] = {
                "type": "object",
                "properties": {o.name: _json_node(o) for o in var.options},
                "additionalProperties": False,
            }
            required = [o.name for o in var.options if o.required]
            if required:
                items["required"] = required
            node["items"] = items
        elif var.elements:
            node["items"] = {"type": _JSON_TYPES[var.elements]}

    return node


def ansible_vars_schema() -> dict[str, Any]:
    """Build the JSON Schema for a generated project's vars files.

    A single schema covers both ``group_vars/`` and ``host_vars/``: Ansible
    merges them into one flat scope, and which file a key lives in is a
    layering choice rather than part of the contract. Every key is therefore
    optional at the top level, and structure is enforced per key.

    ``additionalProperties: false`` is what turns a typo into a visible error
    instead of silence, so it is deliberately strict; ``ansible_*`` and ``x_*``
    stay open for connection settings and operator-defined helpers.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "NVD Ansible group_vars / host_vars",
        "description": (
            "Variable contract for an Ansible project generated by the NVD "
            "automation engine. Generated by "
            "automation/generators/ansible_vars_contract.py — the same "
            "declaration produces each role's meta/argument_specs.yml. "
            "Unknown top-level keys are rejected because every builder lookup "
            "defaults silently, so a misspelled key would otherwise drop "
            "config without any error (and, under 'purge', delete it from the "
            "device). Use an 'x_' prefix for your own helper variables."
        ),
        "type": "object",
        "properties": {v.name: _json_node(v) for v in VARS},
        "patternProperties": {f"^{prefix}": {} for prefix in PASSTHROUGH_PREFIXES},
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Ansible role argument spec rendering
# ---------------------------------------------------------------------------


def _arg_spec_option(var: V) -> dict[str, Any]:
    """Render one variable as an Ansible argument-spec option."""
    spec: dict[str, Any] = {"type": var.type, "description": var.description}

    if var.required:
        spec["required"] = True
    if var.choices:
        spec["choices"] = list(var.choices)
    if var.elements:
        spec["elements"] = var.elements

    # Sub-options are what make runtime validation catch nested typos, so they
    # are emitted wherever the structure is closed. An open dict gets none, or
    # Ansible would reject its free-form keys.
    if var.options and not var.open_dict:
        spec["options"] = {o.name: _arg_spec_option(o) for o in var.options}

    return spec


# Which variables each generated role reads. Roles ignore in-scope variables
# they do not declare, so these are genuine subsets: the topology role does not
# validate service keys, and a spine never trips over leaf-only structure.
ROLE_VARS: dict[str, tuple[str, ...]] = {
    "topology": (
        "node",
        "underlay_interfaces",
        "edge_interfaces",
        "lags",
        "routed_interfaces",
        "bridge_domains",
        "bfd",
        "default_mtu",
    ),
    "fabric": (
        "node",
        "fabric_name",
        "system0_prefix",
        "underlay_interfaces",
        "bgp",
        "routing_policy",
    ),
    "services": (
        "node",
        "bridge_domains",
        "routers",
        "vlans",
        "edge_interfaces",
        "lags",
        "irb_interfaces",
        "routed_interfaces",
        "static_routes",
        "event_handler",
    ),
    "overrides": ("config_overrides",),
    "purge": ("purge",),
    "configure": ("confirm_timeout",),
    "confirm": ("confirm_timeout",),
}

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "topology": "Underlay: system0, ISL interfaces and subinterfaces, BFD, hostname, LLDP.",
    "fabric": "Default network-instance (BGP underlay and overlay) and routing-policy.",
    "services": "Overlay services: edge interfaces, LAGs, IRBs, VXLAN, mac-vrf, ip-vrf, ethernet-segments.",
    "overrides": "Raw SR Linux JSON-RPC patches from 'config_overrides'.",
    "purge": "Delete device config that is no longer present in intent.",
    "configure": "Apply the accumulated update, replace and delete payloads.",
    "confirm": "Confirm a pending two-phase commit.",
}


def role_argument_specs() -> dict[str, dict[str, Any]]:
    """Build ``meta/argument_specs.yml`` content for each generated role.

    Keyed by role name. Roles absent from the result (``detect``) read no
    project variables and get no spec.

    ansible-core runs these automatically at role entry, which is the only
    layer that sees the merged group_vars + host_vars scope the builders
    actually receive — so it is where nested typos, missing required keys and
    wrong types surface. Top-level typos are invisible here (an undeclared
    variable is simply ignored) and are caught by the JSON Schema instead.
    """
    specs: dict[str, dict[str, Any]] = {}
    for role, var_names in ROLE_VARS.items():
        options = {name: _arg_spec_option(_BY_NAME[name]) for name in var_names}
        specs[role] = {
            "argument_specs": {
                "main": {
                    "short_description": _ROLE_DESCRIPTIONS[role],
                    "description": [
                        _ROLE_DESCRIPTIONS[role],
                        "Validated automatically by ansible-core on role entry. "
                        "Variables come from group_vars/ and host_vars/; see "
                        f"schemas/{SCHEMA_FILENAME} for the full contract.",
                    ],
                    "options": options,
                },
            },
        }
    return specs


# ---------------------------------------------------------------------------
# Documentation rendering
# ---------------------------------------------------------------------------


def inert_var_paths() -> list[str]:
    """Return dotted paths of keys accepted by the contract but never read.

    These are emitted so a generated project round-trips through its own
    schema, but the device config hardcodes them. Surfacing the list keeps
    operators from tuning a value that cannot take effect.
    """
    paths: list[str] = []

    def walk(var: V, prefix: str) -> None:
        path = f"{prefix}.{var.name}" if prefix else var.name
        if not var.consumed:
            paths.append(path)
            return
        for opt in var.options:
            walk(opt, path)

    for var in VARS:
        walk(var, "")
    return paths


def vars_reference_table() -> str:
    """Render a Markdown table of top-level variables for the project README."""
    lines = [
        "| Variable | Type | Purpose |",
        "|----------|------|---------|",
    ]
    for group, members in GROUPS.items():
        for var in members:
            summary = var.doc.split(". ")[0].rstrip(".")
            lines.append(f"| `{var.name}` | {var.type} | {summary} ({group}) |")
    return "\n".join(lines)
