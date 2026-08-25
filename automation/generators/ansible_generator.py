"""
Ansible project generator for Nokia SR Linux fabrics.

Day-1 / Day-2 lifecycle
-----------------------
Day-1 (bootstrap):
    The ``generate()`` function transforms a FabricIntent into a complete
    Ansible project.  Running the generated playbook provisions every node
    from scratch — underlay, overlay, services — in a single pass.

Day-2 (incremental changes):
    Users edit the generated host_vars YAML files (add/remove bridge
    domains, change IP addresses, adjust LAG parameters, …) and re-run
    the playbook.  A Jinja2 filter plugin (shipped separately) computes
    the minimal JSON-RPC diff and applies only the delta.

The host_vars format intentionally mirrors EDA intent so that operators
familiar with EDA can work with Ansible using the same mental model.

That Day-2 editing surface is described by
:mod:`automation.generators.ansible_vars_contract`, which this generator
renders into the project twice: as ``schemas/ansible_vars_schema.json``
(referenced from every vars file via a ``yaml-language-server`` directive, so
editors catch unknown keys) and as each role's ``meta/argument_specs.yml``
(validated by ansible-core at role entry, so nested typos and missing keys
fail the run).  Without them a misspelled key is silently ignored by the
builders' ``.get()`` lookups, and under ``purge`` the config it should have
produced is deleted from the device.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from automation.core.models import (
    BridgeDomainIntent,
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    LagMember,
    LinkIntent,
    NodeIntent,
    RouterIntent,
    VlanIntent,
)
from automation.core.selectors import node_matches_selector
from automation.generators.ansible_vars_contract import (
    SCHEMA_FILENAME,
    ansible_vars_schema,
    inert_var_paths,
    role_argument_specs,
    vars_reference_table,
)

logger = logging.getLogger(__name__)

# Directory inside the generated project holding the vars JSON Schema.
SCHEMAS_DIR = "schemas"

VXLAN_INDEX_START = 500
IRB_INDEX_START = 0


# ---------------------------------------------------------------------------
# Interface name conversion
# ---------------------------------------------------------------------------


def _intf_to_srl(name: str) -> str:
    """Convert dash-separated interface name to SR Linux slash notation.

    ``ethernet-1-3``  -> ``ethernet-1/3``
    ``ethernet-1-32`` -> ``ethernet-1/32``

    Only the *last* dash before the port number is converted.
    """
    m = re.match(r"^(ethernet-\d+)-(\d+)$", name)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return name


# ---------------------------------------------------------------------------
# EDA jspath → SR Linux JSON-RPC path conversion
# ---------------------------------------------------------------------------

_JSPATH_PREDICATE_RE = re.compile(r'\{\.(\w[\w-]*)=="?([^"}\s]+)"?\}')


def _jspath_to_jsonrpc(jspath: str) -> str:
    """Convert an EDA jspath to an SR Linux JSON-RPC path.

    ``.system.information``
        → ``/system/information``

    ``.network-instance{.name=="default"}.protocols.bgp``
        → ``/network-instance[name=default]/protocols/bgp``
    """
    if jspath.startswith("/"):
        return jspath

    path = jspath.lstrip(".")
    path = _JSPATH_PREDICATE_RE.sub(r"[\1=\2]", path)
    segments = path.split(".")
    return "/" + "/".join(segments)


# ---------------------------------------------------------------------------
# Configlet resolution — map ConfigletIntents to per-node overrides
# ---------------------------------------------------------------------------


def _resolve_configlets(intent: FabricIntent) -> dict[str, list[dict[str, Any]]]:
    """Convert extras ConfigletIntents to per-node config_overrides lists.

    Only configlets with ``origin == "extras"`` are emitted; design-generated
    configlets (bgp-evpn-rapid, node-isolation, etc.) are already natively
    handled by the srl_builders and would cause duplicates.

    Returns ``{node_name: [{"path": ..., "value": ...}, ...]}``.
    """
    node_map: dict[str, NodeIntent] = {n.name: n for n in intent.nodes}
    overrides: dict[str, list[dict[str, Any]]] = defaultdict(list)

    extras = sorted(
        (c for c in intent.configlets if c.origin == "extras"),
        key=lambda c: c.priority,
    )

    for cfglet in extras:
        target_nodes: set[str] = set()

        for name in cfglet.endpoints:
            if name in node_map:
                target_nodes.add(name)

        if cfglet.endpoint_selector:
            for name, node in node_map.items():
                if node_matches_selector(node, cfglet.endpoint_selector):
                    target_nodes.add(name)

        entries: list[dict[str, Any]] = []
        for cfg in cfglet.configs:
            entry: dict[str, Any] = {
                "path": _jspath_to_jsonrpc(cfg.path),
                "value": json.loads(cfg.config),
            }
            entries.append(entry)

        for node_name in sorted(target_nodes):
            overrides[node_name].extend(entries)

    return dict(overrides)


def _resolve_default_mtus(intent: FabricIntent) -> dict[str, dict[str, Any]]:
    """Resolve ``default_mtus`` per-node using label selectors.

    Returns ``{node_name: {"interface_mtu": ..., "layer2_subif_mtu": ..., "layer3_mtu": ...}}``.
    When multiple DefaultMtuIntent entries match a node, later entries win.
    """
    node_map: dict[str, NodeIntent] = {n.name: n for n in intent.nodes}
    result: dict[str, dict[str, Any]] = {}

    for mtu in intent.default_mtus:
        target_nodes: set[str] = set()

        for name in mtu.nodes:
            if name in node_map:
                target_nodes.add(name)

        if mtu.node_selector:
            for name, node in node_map.items():
                if node_matches_selector(node, mtu.node_selector):
                    target_nodes.add(name)

        entry: dict[str, Any] = {}
        if mtu.interface_mtu is not None:
            entry["interface_mtu"] = mtu.interface_mtu
        if mtu.layer2_subif_mtu is not None:
            entry["layer2_subif_mtu"] = mtu.layer2_subif_mtu
        if mtu.layer3_mtu is not None:
            entry["layer3_mtu"] = mtu.layer3_mtu

        if entry:
            for node_name in target_nodes:
                result[node_name] = entry

    return result


# ---------------------------------------------------------------------------
# Per-node intent index — built once, reused across all builders
# ---------------------------------------------------------------------------


@dataclass
class _IntentIndex:
    """Pre-computed per-node views of a FabricIntent.

    Built once in ``generate()`` and passed to every host-vars builder, so
    lookups that would otherwise be N×M scans (``for ei in edges: if ei.node == ...``)
    become O(1) dict hits.
    """

    edges_by_node: dict[str, list[EdgeInterfaceIntent]]
    lag_member_ports_by_node: dict[str, set[tuple[str, str]]]
    routed_ports_by_node: dict[str, set[tuple[str, str]]]
    # Each entry: list of (lag, members-on-this-node) so builders don't
    # re-filter members per LAG.
    lags_by_node: dict[str, list[tuple[LagIntent, list[LagMember]]]]
    links_by_node: dict[str, list[LinkIntent]]
    node_map: dict[str, NodeIntent]


def _build_intent_index(intent: FabricIntent) -> _IntentIndex:
    edges_by_node: dict[str, list[EdgeInterfaceIntent]] = defaultdict(list)
    for ei in intent.edge_interfaces:
        edges_by_node[ei.node].append(ei)

    lag_member_ports_by_node: dict[str, set[tuple[str, str]]] = defaultdict(set)
    lags_by_node: dict[str, list[tuple[LagIntent, list[LagMember]]]] = defaultdict(list)
    for lag in intent.lags:
        by_node: dict[str, list[LagMember]] = defaultdict(list)
        for m in lag.members:
            by_node[m.node].append(m)
            lag_member_ports_by_node[m.node].add((m.node, m.interface))
        for node_name, members in by_node.items():
            lags_by_node[node_name].append((lag, members))

    ei_by_name: dict[str, EdgeInterfaceIntent] = {e.name: e for e in intent.edge_interfaces}
    routed_ports_by_node: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for ri in intent.routed_interfaces:
        ei = ei_by_name.get(ri.interface)
        if ei:
            routed_ports_by_node[ei.node].add((ei.node, ei.interface))

    links_by_node: dict[str, list[LinkIntent]] = defaultdict(list)
    for link in intent.links:
        links_by_node[link.local_node].append(link)
        links_by_node[link.remote_node].append(link)

    return _IntentIndex(
        edges_by_node=dict(edges_by_node),
        lag_member_ports_by_node=dict(lag_member_ports_by_node),
        routed_ports_by_node=dict(routed_ports_by_node),
        lags_by_node=dict(lags_by_node),
        links_by_node=dict(links_by_node),
        node_map={n.name: n for n in intent.nodes},
    )


# ---------------------------------------------------------------------------
# Service placement — resolve which services land on which node
# ---------------------------------------------------------------------------


@dataclass
class _NodeServices:
    """Accumulated service state for a single node."""

    irb_interfaces: list[dict[str, Any]] = field(default_factory=list)
    routed_interfaces: list[dict[str, Any]] = field(default_factory=list)
    static_routes: list[dict[str, Any]] = field(default_factory=list)


def _resolve_placement(intent: FabricIntent) -> dict[str, _NodeServices]:
    """Determine which node-specific services land on each node.

    Bridge domains, routers, and VLANs are now emitted as shared definitions
    in group_vars and resolved at playbook time by the srl_config filter.
    This function only resolves per-node resources: IRB interfaces (which
    may have node-specific IP addresses), routed interfaces, and static routes.

    Returns a mapping of ``node_name -> _NodeServices``.
    """
    node_map: dict[str, NodeIntent] = {n.name: n for n in intent.nodes}
    ei_map: dict[str, EdgeInterfaceIntent] = {e.name: e for e in intent.edge_interfaces}
    router_map: dict[str, RouterIntent] = {r.name: r for r in intent.routers}

    svc: dict[str, _NodeServices] = {n.name: _NodeServices() for n in intent.nodes}

    # --- IRB placement via router node_selector ---
    for irb in intent.irb_interfaces:
        router = router_map.get(irb.router)
        if not router:
            continue
        irb_entry = _irb_host_entry(irb)
        for node_name, node in node_map.items():
            if node_matches_selector(node, router.node_selector):
                svc[node_name].irb_interfaces.append(irb_entry)

    # --- Routed interfaces (placed on the owning edge interface's node) ---
    for ri in intent.routed_interfaces:
        ei = ei_map.get(ri.interface)
        if not ei:
            continue
        ipv4_addrs = []
        for a in ri.ipv4_addresses:
            ipv4_addrs.append({
                "ip_prefix": a.get("ipPrefix", a.get("ip_prefix", "")),
                "primary": a.get("primary", True),
            })
        svc[ei.node].routed_interfaces.append({
            "name": ri.name,
            "interface": _intf_to_srl(ei.interface),
            "router": ri.router,
            "vlan_id": ri.vlan_id,
            "ip_mtu": ri.ip_mtu,
            "arp_timeout": ri.arp_timeout,
            "ipv4_addresses": ipv4_addrs,
        })

    # --- Static routes (placed on explicit nodes, or all nodes with matching router) ---
    for sr in intent.static_routes:
        router = router_map.get(sr.router)
        if sr.nodes:
            target_nodes = sr.nodes
        elif router:
            target_nodes = [
                n.name for n in intent.nodes
                if node_matches_selector(n, router.node_selector)
            ]
        else:
            target_nodes = []
        nhg = sr.nexthop_group
        nexthops = []
        for nh in nhg.get("nexthops", []):
            nexthops.append({
                "ip_address": nh.get("ipPrefix", nh.get("ip_address", "")),
                "resolve": nh.get("resolve", False),
            })
        for node_name in target_nodes:
            if node_name not in svc:
                continue
            svc[node_name].static_routes.append({
                "name": sr.name,
                "router": sr.router,
                "prefixes": list(sr.prefixes),
                "nexthop_group": {
                    "name": sr.name,
                    "nexthops": nexthops,
                },
            })

    return svc


def _irb_host_entry(irb: IrbInterfaceIntent) -> dict[str, Any]:
    """Build an IRB interface entry for host_vars."""
    entry: dict[str, Any] = {
        "bridge_domain": irb.bridge_domain,
        "router": irb.router,
    }
    if irb.ipv4:
        entry["ipv4"] = irb.ipv4
    elif irb.ip_addresses:
        for addr in irb.ip_addresses:
            if addr.ipv4:
                entry["ipv4"] = addr.ipv4.get("ip_prefix", "")
                break
    if irb.anycast_gw:
        entry["anycast_gw"] = irb.anycast_gw
    if irb.proxy_arp:
        entry["proxy_arp"] = irb.proxy_arp
    if irb.arp_timeout:
        entry["arp_timeout"] = irb.arp_timeout
    if irb.ip_mtu and irb.ip_mtu != 1500:
        entry["ip_mtu"] = irb.ip_mtu
    if irb.proxy_nd:
        entry["proxy_nd"] = irb.proxy_nd
    if irb.learn_unsolicited and irb.learn_unsolicited != "NONE":
        entry["learn_unsolicited"] = irb.learn_unsolicited
    if irb.evpn_route_advertisement_type is not None:
        entry["evpn_route_advertisement_type"] = irb.evpn_route_advertisement_type
    if irb.host_route_populate is not None:
        entry["host_route_populate"] = irb.host_route_populate
    return entry



# ---------------------------------------------------------------------------
# Host vars builders
# ---------------------------------------------------------------------------


def _build_leaf_host_vars(
    node: NodeIntent,
    intent: FabricIntent,
    node_svc: _NodeServices,
    index: _IntentIndex | None = None,
) -> dict[str, Any]:
    """Build host_vars dict for a leaf node."""
    if index is None:
        index = _build_intent_index(intent)

    hv: dict[str, Any] = {}

    # Node identity with labels for runtime selector matching
    node_entry: dict[str, Any] = {
        "hostname": node.name,
        "role": node.role,
        "router_id": node.system0_ipv4.split("/")[0],
        "asn": node.asn,
    }
    if node.labels:
        node_entry["labels"] = dict(node.labels)
    hv["node"] = node_entry

    # Underlay interfaces. A physical port that is also a LAG member
    # cannot carry a routed subinterface on SR Linux, so exclude any
    # uplink that is consumed by a LAG on this node (relevant e.g. for
    # collapsed-spine designs, where spine↔ToR ISL ports are bundled
    # into an ESI LAG and do not run eBGP).
    lag_member_ports = index.lag_member_ports_by_node.get(node.name, set())
    routed_ports = index.routed_ports_by_node.get(node.name, set())
    spine_asn = intent.spine_asn
    underlay = []
    for intf in sorted(node.uplink_interfaces):
        if (node.name, intf) in lag_member_ports:
            continue
        underlay.append({
            "name": _intf_to_srl(intf),
            "peer_asn": spine_asn,
        })
    if underlay:
        hv["underlay_interfaces"] = underlay

    # Edge interfaces (non-LAG-member) with raw labels for selector matching
    edges = []
    for ei in index.edges_by_node.get(node.name, []):
        if (ei.node, ei.interface) in lag_member_ports:
            continue
        if (ei.node, ei.interface) in routed_ports:
            continue
        entry: dict[str, Any] = {
            "name": _intf_to_srl(ei.interface),
            "encap": ei.encap,
        }
        if ei.labels:
            entry["labels"] = dict(ei.labels)
        edges.append(entry)
    if edges:
        hv["edge_interfaces"] = edges

    # LAGs with raw labels for selector matching
    lags = _build_lag_entries(node, index)
    if lags:
        hv["lags"] = lags

    # IRB interfaces (pre-resolved per node, with node-specific IP addresses)
    if node_svc.irb_interfaces:
        hv["irb_interfaces"] = node_svc.irb_interfaces

    # Routed interfaces
    if node_svc.routed_interfaces:
        hv["routed_interfaces"] = node_svc.routed_interfaces

    # Static routes
    if node_svc.static_routes:
        hv["static_routes"] = node_svc.static_routes

    # Event handler (node isolation) for leaves with LAG members
    eh = _build_event_handler(node, index)
    if eh:
        hv["event_handler"] = eh

    return hv


def _build_spine_host_vars(
    node: NodeIntent,
    intent: FabricIntent,
    index: _IntentIndex | None = None,
) -> dict[str, Any]:
    """Build host_vars dict for a spine node."""
    if index is None:
        index = _build_intent_index(intent)

    hv: dict[str, Any] = {}

    hv["node"] = {
        "hostname": node.name,
        "role": node.role,
        "router_id": node.system0_ipv4.split("/")[0],
        "asn": node.asn,
    }

    # Spine underlay interfaces need per-leaf peer_asn
    underlay = []
    for link in sorted(index.links_by_node.get(node.name, []), key=lambda l: l.name):
        if link.local_node == node.name:
            peer_node_name = link.remote_node
            intf = link.local_interface
        else:
            peer_node_name = link.local_node
            intf = link.remote_interface
        peer = index.node_map.get(peer_node_name)
        underlay.append({
            "name": _intf_to_srl(intf),
            "peer_asn": peer.asn if peer else intent.leaf_asn_start,
        })

    if underlay:
        hv["underlay_interfaces"] = underlay

    return hv


def _build_lag_entries(
    node: NodeIntent, index: _IntentIndex,
) -> list[dict[str, Any]]:
    """Build LAG entries for a node's host_vars."""
    entries: list[dict[str, Any]] = []
    for lag, node_members in index.lags_by_node.get(node.name, []):
        agg_id = node_members[0].aggregate_id
        lag_iface_name = f"lag{agg_id}"
        entry: dict[str, Any] = {
            "name": lag_iface_name,
            "description": lag.name,
            "aggregate_id": agg_id,
            "mode": lag.multihoming_mode,
            "min_links": lag.min_links,
        }

        lacp_entry: dict[str, Any] = {"interval": lag.lacp.interval}
        if lag.lacp.system_id_mac:
            lacp_entry["system_id_mac"] = lag.lacp.system_id_mac
        if lag.lacp.system_priority:
            lacp_entry["system_priority"] = lag.lacp.system_priority
        admin_key = lag.lacp.admin_key if lag.lacp.admin_key is not None else int(agg_id)
        lacp_entry["admin_key"] = admin_key
        entry["lacp"] = lacp_entry

        if lag.lacp.fallback:
            entry["fallback"] = {
                "mode": lag.lacp.fallback.get("mode", "static"),
                "timeout": lag.lacp.fallback.get("timeout", 60),
            }

        if lag.reload_delay_timer:
            entry["reload_delay_timer"] = lag.reload_delay_timer

        if lag.multihoming_mode in ("port-active", "single-active"):
            entry["revertive"] = lag.revertive
            if lag.preferred_active_node == node.name:
                entry["df_preference"] = 800
            else:
                entry["df_preference"] = 500

        if lag.labels:
            entry["labels"] = dict(lag.labels)

        members = []
        for m in sorted(node_members, key=lambda x: x.interface):
            members.append({"interface": _intf_to_srl(m.interface)})
        entry["members"] = members

        entries.append(entry)
    return entries


def _build_event_handler(
    node: NodeIntent, index: _IntentIndex,
) -> dict[str, Any] | None:
    """Build event_handler config for node isolation on LAG leaves."""
    lag_member_intfs: list[str] = []
    for _lag, members in index.lags_by_node.get(node.name, []):
        for m in members:
            lag_member_intfs.append(_intf_to_srl(m.interface))

    if not lag_member_intfs:
        return None

    return {
        "node_isolation": {
            "down_links": sorted(set(lag_member_intfs)),
            "hold_down_time": 20000,
            "required_bgp_sessions": 1,
        },
    }



# ---------------------------------------------------------------------------
# Group vars builders
# ---------------------------------------------------------------------------


def _build_group_vars_all(intent: FabricIntent) -> dict[str, Any]:
    """Build group_vars/all.yml content."""
    return {
        "fabric_name": intent.fabric_name,
        "system0_prefix": intent.system0_prefix,
        "purge": True,
        "confirm_timeout": 0,
        "ansible_connection": "ansible.netcommon.httpapi",
        "ansible_network_os": "nokia.srlinux.srlinux",
        "ansible_user": intent.credentials.username,
        "ansible_password": intent.credentials.password,
        "ansible_httpapi_validate_certs": False,
    }


def _bgp_gv(intent: FabricIntent, multipath_max_paths: int) -> dict[str, Any]:
    """Build the shared BGP/BFD/routing-policy group_vars block.

    Only the ipv4/ipv6-unicast ``multipath_max_paths`` varies between roles
    (2 for leafs, 6 for spines).
    """
    return {
        "bgp": {
            "preference": {"ebgp": 170, "ibgp": 170},
            "route_advertisement": {
                "rapid_withdrawal": True,
                "wait_for_fib_install": False,
            },
            "afi_safi": {
                "evpn": {
                    "multipath_max_paths": 64,
                    "inter_as_vpn": True,
                    "rapid_update": True,
                },
                "ipv4_unicast": {
                    "multipath_max_paths": multipath_max_paths,
                    "advertise_ipv6_next_hops": True,
                    "receive_ipv6_next_hops": True,
                    "rapid_update": True,
                },
                "ipv6_unicast": {
                    "multipath_max_paths": multipath_max_paths,
                    "rapid_update": True,
                },
            },
            "group_name": f"bgpgroup-ebgp-{intent.fabric_name}",
            "ebgp_default_policy": {
                "import_reject_all": True,
                "export_reject_all": True,
            },
        },
        "bfd": {
            "desired_min_transmit_interval": 1000000,
            "required_min_receive": 1000000,
            "detection_multiplier": 3,
            "min_echo_receive_interval": 1000000,
        },
        "routing_policy": _routing_policy_gv(intent),
    }


def _routing_policy_gv(intent: FabricIntent) -> dict[str, Any]:
    """Build the routing_policy group_vars block from intent.

    Emits:
      * ``prefix_set`` / ``export_policy`` / ``import_policy`` — first name
        from the fabric-level refs (the SRL builder's existing scalar API).
      * ``prefix_sets`` — full PrefixSet definitions (serialized intent).
      * ``policies`` — full Policy definitions (serialized intent).
    The SRL builder renders the routing-policy tree directly from these.
    """
    default_ps = f"prefixset-{intent.fabric_name}"
    default_export = f"ebgp-isl-export-policy-{intent.fabric_name}"
    default_import = f"ebgp-isl-import-policy-{intent.fabric_name}"

    prefix_set_name = (
        intent.prefix_sets[0].name if intent.prefix_sets else default_ps
    )
    export_name = (
        intent.fabric_export_policies[0]
        if intent.fabric_export_policies
        else default_export
    )
    import_name = (
        intent.fabric_import_policies[0]
        if intent.fabric_import_policies
        else default_import
    )

    prefix_sets = [
        ps.model_dump(exclude_none=True, exclude={"internal"}) for ps in intent.prefix_sets
    ]
    policies = [
        rp.model_dump(exclude_none=True, exclude={"internal"}) for rp in intent.routing_policies
    ]

    return {
        "prefix_set": prefix_set_name,
        "export_policy": export_name,
        "import_policy": import_name,
        "prefix_sets": prefix_sets,
        "policies": policies,
    }


def _build_group_vars_leafs(intent: FabricIntent) -> dict[str, Any]:
    """Build group_vars/leafs.yml with leaf-common BGP/BFD and shared services."""
    gv = _bgp_gv(intent, multipath_max_paths=2)

    if intent.bridge_domains:
        gv["bridge_domains"] = [_bd_group_entry(bd) for bd in intent.bridge_domains]
    if intent.routers:
        gv["routers"] = [_router_group_entry(r) for r in intent.routers]
    if intent.vlans:
        gv["vlans"] = [_vlan_group_entry(v) for v in intent.vlans]

    return gv


def _bd_group_entry(bd: BridgeDomainIntent) -> dict[str, Any]:
    """Build a bridge domain entry for group_vars (no access, no irb)."""
    entry: dict[str, Any] = {
        "name": bd.name,
        "vni": bd.vni,
        "evi": bd.evi,
    }
    if not bd.mac_learning:
        entry["mac_learning"] = bd.mac_learning
    if bd.mac_aging != 300:
        entry["mac_aging"] = bd.mac_aging
    if bd.mac_duplication is not None:
        entry["mac_duplication"] = bd.mac_duplication
    if bd.export_target is not None:
        entry["export_target"] = bd.export_target
    if bd.import_target is not None:
        entry["import_target"] = bd.import_target
    return entry


def _router_group_entry(r: RouterIntent) -> dict[str, Any]:
    """Build a router entry for group_vars (with node_selector)."""
    entry: dict[str, Any] = {
        "name": r.name,
        "vni": r.vni,
        "evi": r.evi,
    }
    if r.node_selector:
        entry["node_selector"] = list(r.node_selector)
    if r.export_target is not None:
        entry["export_target"] = r.export_target
    if r.import_target is not None:
        entry["import_target"] = r.import_target
    return entry


def _vlan_group_entry(v: VlanIntent) -> dict[str, Any]:
    """Build a VLAN entry for group_vars (with interface_selector)."""
    entry: dict[str, Any] = {
        "name": v.name,
        "bridge_domain": v.bridge_domain,
        "vlan_id": v.vlan_id,
    }
    if v.interface_selector:
        entry["interface_selector"] = list(v.interface_selector)
    return entry


def _build_group_vars_spines(intent: FabricIntent) -> dict[str, Any]:
    """Build group_vars/spines.yml with spine-common BGP/BFD settings."""
    return _bgp_gv(intent, multipath_max_paths=6)


# ---------------------------------------------------------------------------
# Inventory builder
# ---------------------------------------------------------------------------


def _build_inventory(intent: FabricIntent) -> dict[str, Any]:
    """Build Ansible inventory.yml."""
    leafs: dict[str, Any] = {}
    spines: dict[str, Any] = {}

    for node in sorted(intent.nodes, key=lambda n: n.name):
        entry = {"ansible_host": node.mgmt_ipv4}
        if node.role == "leaf":
            leafs[node.name] = entry
        else:
            spines[node.name] = entry

    inventory: dict[str, Any] = {
        "all": {
            "children": {},
        },
    }
    if leafs:
        inventory["all"]["children"]["leafs"] = {"hosts": leafs}
    if spines:
        inventory["all"]["children"]["spines"] = {"hosts": spines}

    return inventory


# ---------------------------------------------------------------------------
# Static file content
# ---------------------------------------------------------------------------


def _ansible_cfg() -> str:
    return textwrap.dedent("""\
        [defaults]
        host_key_checking = False
        interpreter_python = auto_silent
        filter_plugins = ./filter_plugins
        deprecation_warnings = False
        display_skipped_hosts = False
        display_ok_hosts = False
    """)


def _playbook_yml(fabric_name: str) -> list[dict[str, Any]]:
    """Generate a concise two-play, role-based playbook."""
    play_configure = {
        "name": f"Configure {fabric_name} fabric",
        "hosts": "all",
        "gather_facts": False,
        "roles": [
            {"role": "detect", "tags": ["always"]},
            {"role": "topology", "tags": ["topology"]},
            {"role": "fabric", "tags": ["fabric"]},
            {"role": "services", "tags": ["services"]},
            {"role": "overrides", "tags": ["overrides", "services"]},
            {
                "role": "purge",
                "tags": ["never", "full"],
                "when": "purge | default(true) | bool",
            },
            {"role": "configure", "tags": ["always"]},
        ],
    }

    play_confirm = {
        "name": f"Confirm {fabric_name} fabric changes",
        "hosts": "all",
        "gather_facts": False,
        "any_errors_fatal": True,
        "roles": [{"role": "confirm"}],
    }

    return [play_configure, play_confirm]


# ---------------------------------------------------------------------------
# Role task generators
# ---------------------------------------------------------------------------

def _role_detect() -> list[dict[str, Any]]:
    return [
        {
            "name": "Get software version",
            "nokia.srlinux.get": {
                "paths": [{
                    "path": "/platform/control[slot=A]/software-version",
                    "datastore": "state",
                }],
            },
            "register": "version_result",
            "no_log": True,
        },
        {
            "name": "Set sw_version fact",
            "ansible.builtin.set_fact": {
                "sw_version": "{{ version_result.result[0].split('-')[0] }}",
            },
            "no_log": True,
        },
        {
            "name": "Initialize config accumulators",
            "ansible.builtin.set_fact": {
                "config_update": [],
                "config_replace": [],
                "config_delete": [],
            },
            "no_log": True,
        },
    ]


def _role_phase(phase: str) -> list[dict[str, Any]]:
    """Generate tasks for a topology/fabric/services phase role."""
    return [
        {
            "name": f"Build {phase} payloads",
            "ansible.builtin.set_fact": {
                "phase_config": _LiteralStr(
                    f"{{{{ hostvars[inventory_hostname] | srl_config(sw_version, '{phase}') }}}}"
                ),
            },
            "delegate_to": "localhost",
            "no_log": True,
        },
        {
            "name": f"Accumulate {phase} updates",
            "ansible.builtin.set_fact": {
                "config_update": "{{ config_update + phase_config['update'] }}",
                "config_replace": "{{ config_replace + phase_config['replace'] }}",
            },
            "no_log": True,
        },
    ]


def _role_purge() -> list[dict[str, Any]]:
    return [
        {
            "name": "Fetch device config for pruning",
            "nokia.srlinux.get": {
                "paths": [
                    {"path": "/network-instance", "datastore": "running"},
                    {"path": "/tunnel-interface[name=vxlan0]/vxlan-interface", "datastore": "running"},
                    {"path": "/interface", "datastore": "running"},
                ],
            },
            "register": "state_result",
        },
        {
            "name": "Normalize device state",
            "ansible.builtin.set_fact": {
                "device_state": "{{ state_result.result | srl_normalize_state }}",
            },
            "when": "state_result is defined and state_result.result is defined",
        },
        {
            "name": "Build prune deletes",
            "ansible.builtin.set_fact": {
                "config_delete": _LiteralStr(
                    "{{ config_delete + (hostvars[inventory_hostname]"
                    " | srl_config_deletes(device_state | default({}), sw_version)) }}"
                ),
            },
            "delegate_to": "localhost",
            "when": "device_state is defined",
        },
    ]


def _role_configure() -> list[dict[str, Any]]:
    has_work = (
        "(config_update | default([]) | length > 0) or "
        "(config_replace | default([]) | length > 0) or "
        "(config_delete | default([]) | length > 0)"
    )
    return [
        {
            "name": "Apply configuration",
            "nokia.srlinux.config": {
                "update": "{{ config_update | default([]) }}",
                "replace": "{{ config_replace | default([]) }}",
                "delete": "{{ config_delete | default([]) }}",
                "confirm_timeout": "{{ confirm_timeout | default(0) | int }}",
            },
            "register": "deploy_result",
            "when": has_work,
        },
        # {
        #     "name": "Show changes",
        #     "ansible.builtin.debug": {"var": "deploy_result.diff"},
        #     "when": "deploy_result is defined and deploy_result.changed | default(false)",
        # },
    ]


def _role_confirm() -> list[dict[str, Any]]:
    confirm_when = "confirm_timeout | default(0) | int > 0"
    return [
        {
            "name": "Pause for validation",
            "ansible.builtin.pause": {
                "seconds": "{{ [confirm_timeout | int - 5, 10] | max }}",
                "prompt": "Validating changes... will auto-confirm after timeout",
            },
            "run_once": True,
            "when": confirm_when,
        },
        {
            "name": "Confirm pending commits",
            "nokia.srlinux.config": {
                "datastore": "tools",
                "update": [{
                    "path": "/system/configuration/confirmed-accept",
                    "value": {},
                }],
            },
            "when": confirm_when,
        },
    ]


def _role_overrides() -> list[dict[str, Any]]:
    """Accumulate user-provided config_overrides into the update bucket.

    Overrides come from ``extras.configlets`` in the design inputs (converted
    to JSON-RPC path/value pairs at generation time) or from hand-edited
    ``host_vars`` / ``group_vars`` YAML during Day-2 operations.
    """
    return [
        {
            "name": "Accumulate config overrides",
            "ansible.builtin.set_fact": {
                "config_update": "{{ config_update + (config_overrides | default([])) }}",
            },
            "when": "config_overrides is defined and config_overrides | length > 0",
        },
    ]


_ROLES: dict[str, Any] = {
    "detect": _role_detect,
    "topology": lambda: _role_phase("topology"),
    "fabric": lambda: _role_phase("fabric"),
    "services": lambda: _role_phase("services"),
    "overrides": _role_overrides,
    "purge": _role_purge,
    "configure": _role_configure,
    "confirm": _role_confirm,
}


def _generate_roles(output_dir: Path) -> None:
    """Write roles/<name>/{tasks,meta}/main.yml for every role.

    Roles that read project variables also get ``meta/argument_specs.yml``.
    ansible-core validates it on role entry with no task wiring needed, so a
    misspelled nested key or a missing required one aborts the run instead of
    silently dropping the config it should have produced.
    """
    arg_specs = role_argument_specs()
    for role_name, task_fn in _ROLES.items():
        role_dir = output_dir / "roles" / role_name
        role_tasks_dir = role_dir / "tasks"
        role_tasks_dir.mkdir(parents=True, exist_ok=True)
        _write_yaml(role_tasks_dir / "main.yml", task_fn())

        spec = arg_specs.get(role_name)
        if spec is not None:
            role_meta_dir = role_dir / "meta"
            role_meta_dir.mkdir(parents=True, exist_ok=True)
            _write_yaml(role_meta_dir / "argument_specs.yml", spec)

    logger.debug(
        "Generated %d roles under %s/roles (%d with argument specs)",
        len(_ROLES), output_dir, len(arg_specs),
    )


# ---------------------------------------------------------------------------
# Static file content
# ---------------------------------------------------------------------------


def _requirements_yml() -> dict[str, Any]:
    return {
        "collections": [
            {"name": "nokia.srlinux", "version": ">=0.4.0"},
            {"name": "ansible.netcommon", "version": ">=5.0.0"},
        ],
    }


def _readme(fabric_name: str, generated_at: str = "") -> str:
    # Interpolation happens after dedent: a substituted block carries its own
    # (zero) indentation, which would otherwise collapse the common prefix
    # textwrap.dedent measures and leave the whole document indented.
    staleness_note = ""
    if generated_at:
        staleness_note = "\n" + textwrap.dedent(f"""\

            > **Generated at:** {generated_at}
            >
            > This project resolves label selectors at generation time.
            > If the topology or services inputs have changed since this timestamp,
            > re-generate the project to pick up the latest intent.""")
    body = textwrap.dedent("""\
        # {fabric_name} — Ansible Project

        Auto-generated by the NVD Ansible generator.{staleness_note}

        ## Quick start

        ```bash
        # Install dependencies
        ansible-galaxy collection install -r requirements.yml

        # Full deploy (all phases, with pruning)
        ansible-playbook -i inventory.yml playbook.yml

        # Deploy topology only
        ansible-playbook -i inventory.yml playbook.yml --tags topology

        # Deploy services only
        ansible-playbook -i inventory.yml playbook.yml --tags services

        # Full deploy with confirmed commit (120s validation window)
        ansible-playbook -i inventory.yml playbook.yml -e confirm_timeout=120

        # Full deploy without pruning stale resources
        ansible-playbook -i inventory.yml playbook.yml -e purge=false

        # Single node, services only
        ansible-playbook -i inventory.yml playbook.yml --tags services --limit leaf4
        ```

        ## Phases

        The playbook supports three tagged phases that can be run independently:

        | Tag | Scope |
        |-----|-------|
        | `topology` | system0, underlay interfaces, BFD, hostname, LLDP |
        | `fabric` | default NI (BGP underlay/overlay), routing-policy |
        | `services` | edge interfaces, LAGs, IRBs, VXLAN, mac-vrf, ip-vrf, ES |
        | `overrides` | raw SR Linux config patches from `extras.configlets` |

        Running without `--tags` applies all phases.  With `--tags full`,
        pruning of stale resources is also performed.

        ## Declarative intent

        The playbook uses `replace` for subtrees we fully own (subinterfaces,
        network-instances, routing-policy, VXLAN, LAG aggregates, ethernet-segments)
        and `update` for physical interfaces and system leaves.  This means the
        device itself removes anything under a replaced path that is not in the
        intent -- true declarative configuration.

        ## Confirmed commits

        Set `confirm_timeout` (seconds) to enable two-phase commits:

        ```bash
        ansible-playbook -i inventory.yml playbook.yml -e confirm_timeout=120
        ```

        Changes are applied with a timeout; if not confirmed within the window,
        the device automatically rolls back.

        ## Config overrides

        The `config_overrides` variable holds raw SR Linux JSON-RPC path/value
        entries that are applied after all phase-based configuration.  These
        come from `extras.configlets` in the design inputs and are pre-resolved
        into each node's `host_vars`.

        For Day-2, you can add or edit overrides directly in `host_vars/` or
        `group_vars/` files:

        ```yaml
        config_overrides:
          - path: /system/information
            value:
              contact: support@example.com
          - path: /system/name/domain-name
            value: lab.example.com
        ```

        ## Variable reference

        `group_vars/` and `host_vars/` are merged into one flat scope, so which
        file a key lives in is a layering choice, not part of the contract.
        Every key below is optional except `node`.

        {vars_table}

        Two details catch people out.  Interface names here use SR Linux slash
        notation (`ethernet-1/3`), not the dash notation of the design
        `inputs/`.  And a handful of keys are accepted so a generated project
        round-trips through its own schema, but are never read — the emitted
        device config hardcodes them, so editing them does nothing:

        {inert_list}

        ## Validating your edits

        Because every builder lookup defaults silently, a misspelled key would
        otherwise drop config with no error at all — and with `purge` enabled,
        the config it should have produced is then deleted from the device.
        Two layers guard against that, and they catch different mistakes.

        **While editing** — `schemas/{schema_filename}` describes the whole
        contract, and each vars file points at it with a
        `# yaml-language-server: $schema=` comment on line 1.  Any editor with
        YAML language server support (VS Code / Cursor with the Red Hat YAML
        extension, `coc-yaml`, `lsp-mode`) then flags unknown top-level keys
        and wrong types as you type, and completes key names.

        To check the same thing in CI:

        ```bash
        pip install check-jsonschema
        check-jsonschema --schemafile schemas/{schema_filename} \\
          group_vars/*.yml host_vars/*.yml
        ```

        **At playbook runtime** — each role ships `meta/argument_specs.yml`,
        which ansible-core validates on role entry with no extra wiring.  This
        is the layer that sees the merged `group_vars` + `host_vars` scope the
        builders actually receive, so it catches misspelled *nested* keys,
        missing required keys and wrong types:

        ```
        TASK [services : Validating arguments against arg spec 'main']
        fatal: [leaf1]: FAILED! => Validation of arguments failed:
        irb_interfaces.anycast_g. Supported parameters include: anycast_gw, ...
        ```

        Neither layer is redundant.  Role argument specs ignore variables a
        role does not declare, so a top-level typo (`irb_interfacs:`) passes
        runtime validation untouched and only the schema catches it; the schema
        in turn never sees the merged scope.  Run both.

        ## Day-2 changes

        Edit files under `host_vars/` to add or modify services, then re-run
        the playbook.  Resources removed from intent are automatically pruned
        (set `purge=false` to disable).

        Be aware that regenerating this project overwrites `inventory.yml`,
        `group_vars/`, `host_vars/`, `roles/` and this README.  Edits made here
        are a fast path for a one-off change or an urgent fix; for anything you
        need to keep, edit the design `inputs/` and regenerate, so the intent
        and the project cannot drift apart.

        ## Structure

        ```
        .
        ├── ansible.cfg
        ├── inventory.yml
        ├── playbook.yml
        ├── requirements.yml
        ├── schemas/
        │   └── {schema_filename}
        ├── group_vars/
        │   ├── all.yml
        │   ├── leafs.yml
        │   └── spines.yml
        ├── host_vars/
        │   └── <node>.yml
        ├── filter_plugins/
        │   └── srl_config.py
        ├── srl_builders/
        └── roles/
            ├── detect/
            ├── topology/
            ├── fabric/
            ├── services/
            ├── overrides/
            ├── purge/
            ├── configure/
            └── confirm/
        ```

        Every role has `tasks/main.yml`.  Every role except `detect` — which
        reads no project variables — also has `meta/argument_specs.yml`.
    """)
    inert = inert_var_paths()
    inert_list = "\n".join(f"- `{path}`" for path in inert) if inert else "- (none)"
    return body.format(
        fabric_name=fabric_name,
        staleness_note=staleness_note,
        vars_table=vars_reference_table(),
        inert_list=inert_list,
        schema_filename=SCHEMA_FILENAME,
    )


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


class _LiteralStr(str):
    """Marker for YAML literal block scalars."""


def _literal_representer(dumper: yaml.Dumper, data: _LiteralStr) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_representer)


def _yaml_dump(data: Any) -> str:
    """Dump data to YAML with consistent formatting."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False, width=120)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(intent: FabricIntent, output_dir: Path | str | None = None) -> Path:
    """
    Generate a complete Ansible project from a FabricIntent.

    Args:
        intent: The complete fabric intent.
        output_dir: Target directory. Defaults to ``./ansible-<fabric_name>``.

    Returns:
        Path to the generated project directory.
    """
    if output_dir is None:
        output_dir = Path(f"ansible-{intent.fabric_name}")
    output_dir = Path(output_dir)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Generating Ansible project for fabric '%s'", intent.fabric_name)

    index = _build_intent_index(intent)
    node_services = _resolve_placement(intent)
    node_overrides = _resolve_configlets(intent)
    node_mtus = _resolve_default_mtus(intent)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "group_vars").mkdir(exist_ok=True)
    (output_dir / "host_vars").mkdir(exist_ok=True)
    (output_dir / "filter_plugins").mkdir(exist_ok=True)

    _write_yaml(output_dir / "inventory.yml", _build_inventory(intent))
    _write_vars_schema(output_dir)

    _write_vars_yaml(output_dir / "group_vars" / "all.yml", _build_group_vars_all(intent))
    _write_vars_yaml(output_dir / "group_vars" / "leafs.yml", _build_group_vars_leafs(intent))
    _write_vars_yaml(output_dir / "group_vars" / "spines.yml", _build_group_vars_spines(intent))

    for node in intent.nodes:
        svc = node_services.get(node.name, _NodeServices())
        if node.role == "leaf":
            hv = _build_leaf_host_vars(node, intent, svc, index)
        else:
            hv = _build_spine_host_vars(node, intent, index)
        mtu = node_mtus.get(node.name)
        if mtu:
            hv["default_mtu"] = mtu
        co = node_overrides.get(node.name)
        if co:
            hv["config_overrides"] = co
        _write_vars_yaml(output_dir / "host_vars" / f"{node.name}.yml", hv)

    _copy_filter_plugins(output_dir)
    _generate_roles(output_dir)

    _write_yaml(output_dir / "playbook.yml", _playbook_yml(intent.fabric_name))
    # Add generation timestamp as a comment to the playbook
    playbook_path = output_dir / "playbook.yml"
    existing = playbook_path.read_text()
    playbook_path.write_text(
        f"# Generated by NVD automation — {generated_at}\n{existing}"
    )
    _write_text(output_dir / "ansible.cfg", _ansible_cfg())
    _write_yaml(output_dir / "requirements.yml", _requirements_yml())
    _write_text(output_dir / "README.md", _readme(intent.fabric_name, generated_at))

    node_count = len(intent.nodes)
    bd_count = len(intent.bridge_domains)
    router_count = len(intent.routers)
    logger.info(
        "Ansible project written to %s (%d nodes, %d bridge domains, %d routers)",
        output_dir, node_count, bd_count, router_count,
    )

    return output_dir


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: Any) -> None:
    with open(path, "w") as f:
        f.write(_yaml_dump(data))
    logger.debug("Wrote %s", path)


def _write_vars_yaml(path: Path, data: Any) -> None:
    """Write a group_vars/host_vars file with a schema directive on top.

    The ``yaml-language-server`` comment is the same mechanism the design
    ``inputs/`` fragments already use, so editing a generated vars file gets
    the same key completion and unknown-key warnings as editing the inputs.
    Both vars directories sit one level below the project root, so the
    relative path is identical for each.
    """
    header = (
        f"# yaml-language-server: $schema=../{SCHEMAS_DIR}/{SCHEMA_FILENAME}\n"
        "# Generated by NVD automation. Regenerating overwrites this file —\n"
        "# for durable changes edit the design inputs/ and regenerate.\n"
    )
    with open(path, "w") as f:
        f.write(header + _yaml_dump(data))
    logger.debug("Wrote %s", path)


def _write_vars_schema(output_dir: Path) -> Path:
    """Write the vars JSON Schema that the vars files' directives point at."""
    schemas_dir = output_dir / SCHEMAS_DIR
    schemas_dir.mkdir(parents=True, exist_ok=True)
    path = schemas_dir / SCHEMA_FILENAME
    path.write_text(json.dumps(ansible_vars_schema(), indent=2) + "\n")
    logger.debug("Wrote %s", path)
    return path


def _write_text(path: Path, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
    logger.debug("Wrote %s", path)


def _copy_filter_plugins(output_dir: Path) -> None:
    """Copy filter plugin files into the generated project.

    ``srl_config.py`` goes into ``filter_plugins/`` (where Ansible looks).
    ``srl_builders/`` goes into the project root (next to ``filter_plugins/``)
    so Ansible does not try to load builder modules as filter plugins.
    """
    import shutil

    src = Path(__file__).parent / "ansible_filter_plugins"
    if not src.exists():
        logger.warning("Filter plugin source not found at %s", src)
        return

    fp_dir = output_dir / "filter_plugins"
    fp_dir.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.is_dir():
            target = output_dir / item.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, fp_dir / item.name)

    logger.debug("Copied filter plugins to %s", output_dir)
