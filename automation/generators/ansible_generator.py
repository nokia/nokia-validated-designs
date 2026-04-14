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
"""

from __future__ import annotations

import logging
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from automation.core.models import (
    BridgeDomainIntent,
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    NodeIntent,
    RouterIntent,
    VlanIntent,
)

logger = logging.getLogger(__name__)

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
# Service placement — resolve which services land on which node
# ---------------------------------------------------------------------------


@dataclass
class _NodeServices:
    """Accumulated service state for a single node."""

    bridge_domains: list[dict[str, Any]] = field(default_factory=list)
    routers: list[dict[str, Any]] = field(default_factory=list)
    routed_interfaces: list[dict[str, Any]] = field(default_factory=list)
    static_routes: list[dict[str, Any]] = field(default_factory=list)


def _resolve_placement(intent: FabricIntent) -> dict[str, _NodeServices]:
    """Determine which services land on each node.

    Returns a mapping of ``node_name -> _NodeServices``.
    """
    node_map: dict[str, NodeIntent] = {n.name: n for n in intent.nodes}
    ei_map: dict[str, EdgeInterfaceIntent] = {e.name: e for e in intent.edge_interfaces}
    bd_map: dict[str, BridgeDomainIntent] = {b.name: b for b in intent.bridge_domains}
    router_map: dict[str, RouterIntent] = {r.name: r for r in intent.routers}
    irb_by_bd: dict[str, IrbInterfaceIntent] = {
        i.bridge_domain: i for i in intent.irb_interfaces
    }

    svc: dict[str, _NodeServices] = {n.name: _NodeServices() for n in intent.nodes}

    # --- Bridge domain placement via VLAN selectors ---
    # node -> set of BD names placed on that node
    node_bds: dict[str, set[str]] = defaultdict(set)
    # node -> BD -> list of access attachments (interface + vlan_id)
    node_bd_access: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for vlan in intent.vlans:
        bd_name = vlan.bridge_domain
        matched_nodes_intfs = _match_vlan_selectors(
            vlan, intent.edge_interfaces, intent.lags,
        )
        for node_name, intf_name, is_lag in matched_nodes_intfs:
            node_bds[node_name].add(bd_name)
            node_bd_access[node_name][bd_name].append({
                "interface": _intf_to_srl(intf_name) if not is_lag else intf_name,
                "vlan": vlan.vlan_id,
            })

    # IRBs also place the BD on nodes matched by the router's node_selector
    for irb in intent.irb_interfaces:
        router = router_map.get(irb.router)
        if not router:
            continue
        for node_name, node in node_map.items():
            if _node_matches_selector(node, router.node_selector):
                node_bds[node_name].add(irb.bridge_domain)

    # Build per-node bridge domain entries
    for node_name, bd_names in node_bds.items():
        for bd_name in sorted(bd_names):
            bd = bd_map.get(bd_name)
            if not bd:
                continue
            entry: dict[str, Any] = {
                "name": bd.name,
                "vni": bd.vni,
                "evi": bd.evi,
            }
            # Track provenance for extras-originated resources
            bd_origin = getattr(bd, "origin", "")
            access = node_bd_access.get(node_name, {}).get(bd_name, [])
            if access:
                entry["access"] = access
            irb = irb_by_bd.get(bd_name)
            irb_origin = ""
            if irb:
                irb_origin = getattr(irb, "origin", "")
                irb_entry: dict[str, Any] = {"anycast_gw": True}
                if irb.ipv4:
                    irb_entry["ipv4"] = irb.ipv4
                elif irb.ip_addresses:
                    for addr in irb.ip_addresses:
                        if addr.ipv4:
                            irb_entry["ipv4"] = addr.ipv4.get("ip_prefix", "")
                            break
                if irb.arp_timeout:
                    irb_entry["arp_timeout"] = irb.arp_timeout
                if irb.proxy_arp:
                    irb_entry["proxy_arp"] = irb.proxy_arp
                if irb.ip_mtu and irb.ip_mtu != 1500:
                    irb_entry["ip_mtu"] = irb.ip_mtu
                entry["irb"] = irb_entry
                entry["router"] = irb.router
            if bd_origin == "extras" or irb_origin == "extras":
                entry["_origin"] = "extras"
            svc[node_name].bridge_domains.append(entry)

    # --- Router placement via node_selector ---
    for router in intent.routers:
        for node_name, node in node_map.items():
            if _node_matches_selector(node, router.node_selector):
                svc[node_name].routers.append({
                    "name": router.name,
                    "vni": router.vni,
                    "evi": router.evi,
                })

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

    # --- Static routes (placed on explicit nodes, or all nodes with the router) ---
    for sr in intent.static_routes:
        target_nodes = sr.nodes if sr.nodes else [
            n.name for n in intent.nodes
            if any(
                r["name"] == sr.router
                for r in svc[n.name].routers
            )
        ]
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


def _match_vlan_selectors(
    vlan: VlanIntent,
    edge_interfaces: list[EdgeInterfaceIntent],
    lags: list[LagIntent],
) -> list[tuple[str, str, bool]]:
    """Match a VLAN's interface_selector against edges and LAGs.

    Returns list of (node_name, interface_name, is_lag) tuples.
    """
    results: list[tuple[str, str, bool]] = []

    for ei in edge_interfaces:
        if _labels_match(vlan.interface_selector, ei.labels):
            results.append((ei.node, ei.interface, False))

    for lag in lags:
        if _labels_match(vlan.interface_selector, lag.labels):
            lag_id = _lag_aggregate_id(lag)
            for member in lag.members:
                results.append((member.node, f"lag{lag_id}", True))

    return results


def _labels_match(selectors: list[str], labels: dict[str, str]) -> bool:
    """Check if any selector matches the labels (OR logic across selectors).

    Handles both full EDA-prefixed keys (``eda.nokia.com/foo=bar``) and
    short keys (``foo=bar``) by stripping whitespace and comparing directly.
    """
    for sel in selectors:
        sel = sel.strip()
        if "=" not in sel:
            continue
        key, value = sel.split("=", 1)
        key = key.strip()
        value = value.strip()
        if labels.get(key) == value:
            return True
    return False


def _node_matches_selector(node: NodeIntent, selectors: list[str]) -> bool:
    """Check if a node matches a list of label selectors (OR logic).

    A node matches if *any* selector matches (same semantics as EDA).
    """
    if not selectors:
        return False
    for sel in selectors:
        sel = sel.strip()
        if not sel or "=" not in sel:
            continue
        key, value = sel.split("=", 1)
        key = key.strip()
        value = value.strip()
        if node.labels.get(key) == value:
            return True
    return False


def _lag_aggregate_id(lag: LagIntent) -> str:
    """Extract the aggregate_id from the first LAG member."""
    if lag.members:
        return lag.members[0].aggregate_id
    return "1"


# ---------------------------------------------------------------------------
# Host vars builders
# ---------------------------------------------------------------------------


def _build_leaf_host_vars(
    node: NodeIntent,
    intent: FabricIntent,
    node_svc: _NodeServices,
) -> dict[str, Any]:
    """Build host_vars dict for a leaf node."""
    hv: dict[str, Any] = {}

    # Node identity
    hv["node"] = {
        "hostname": node.name,
        "role": node.role,
        "router_id": node.system0_ipv4.split("/")[0],
        "asn": node.asn,
    }

    # Underlay interfaces
    spine_asn = intent.spine_asn
    underlay = []
    for intf in sorted(node.uplink_interfaces):
        underlay.append({
            "name": _intf_to_srl(intf),
            "peer_asn": spine_asn,
        })
    if underlay:
        hv["underlay_interfaces"] = underlay

    # Edge interfaces (non-LAG-member)
    lag_member_ports = _lag_member_ports(intent, node.name)
    routed_ports = _routed_ports(intent, node.name)
    edges = []
    for ei in intent.edge_interfaces:
        if ei.node != node.name:
            continue
        if (ei.node, ei.interface) in lag_member_ports:
            continue
        if (ei.node, ei.interface) in routed_ports:
            continue
        entry: dict[str, Any] = {
            "name": _intf_to_srl(ei.interface),
            "encap": ei.encap,
        }
        if ei.labels:
            entry["labels"] = _format_labels(ei.labels)
        edges.append(entry)
    if edges:
        hv["edge_interfaces"] = edges

    # LAGs
    lags = _build_lag_entries(node, intent)
    if lags:
        hv["lags"] = lags

    # Bridge domains
    if node_svc.bridge_domains:
        hv["bridge_domains"] = node_svc.bridge_domains

    # Routers
    if node_svc.routers:
        hv["routers"] = node_svc.routers

    # Routed interfaces
    if node_svc.routed_interfaces:
        hv["routed_interfaces"] = node_svc.routed_interfaces

    # Static routes
    if node_svc.static_routes:
        hv["static_routes"] = node_svc.static_routes

    # Event handler (node isolation) for leaves with LAG members
    eh = _build_event_handler(node, intent)
    if eh:
        hv["event_handler"] = eh

    return hv


def _build_spine_host_vars(
    node: NodeIntent,
    intent: FabricIntent,
) -> dict[str, Any]:
    """Build host_vars dict for a spine node."""
    hv: dict[str, Any] = {}

    hv["node"] = {
        "hostname": node.name,
        "role": node.role,
        "router_id": node.system0_ipv4.split("/")[0],
        "asn": node.asn,
    }

    # Spine underlay interfaces need per-leaf peer_asn
    node_map = {n.name: n for n in intent.nodes}
    underlay = []
    for link in sorted(intent.links, key=lambda l: l.name):
        peer_node_name: str | None = None
        intf: str | None = None
        if link.local_node == node.name:
            peer_node_name = link.remote_node
            intf = link.local_interface
        elif link.remote_node == node.name:
            peer_node_name = link.local_node
            intf = link.remote_interface
        if peer_node_name and intf:
            peer = node_map.get(peer_node_name)
            underlay.append({
                "name": _intf_to_srl(intf),
                "peer_asn": peer.asn if peer else intent.leaf_asn_start,
            })

    if underlay:
        hv["underlay_interfaces"] = underlay

    return hv


def _build_lag_entries(node: NodeIntent, intent: FabricIntent) -> list[dict[str, Any]]:
    """Build LAG entries for a node's host_vars."""
    entries: list[dict[str, Any]] = []
    for lag in intent.lags:
        node_members = [m for m in lag.members if m.node == node.name]
        if not node_members:
            continue

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
            entry["labels"] = _format_labels(lag.labels)

        members = []
        for m in sorted(node_members, key=lambda x: x.interface):
            members.append({"interface": _intf_to_srl(m.interface)})
        entry["members"] = members

        entries.append(entry)
    return entries


def _build_event_handler(
    node: NodeIntent, intent: FabricIntent,
) -> dict[str, Any] | None:
    """Build event_handler config for node isolation on LAG leaves."""
    lag_member_intfs: list[str] = []
    for lag in intent.lags:
        for m in lag.members:
            if m.node == node.name:
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


def _lag_member_ports(
    intent: FabricIntent, node_name: str,
) -> set[tuple[str, str]]:
    """Return set of (node, interface) pairs that are LAG members."""
    ports: set[tuple[str, str]] = set()
    for lag in intent.lags:
        for m in lag.members:
            if m.node == node_name:
                ports.add((m.node, m.interface))
    return ports


def _routed_ports(
    intent: FabricIntent, node_name: str,
) -> set[tuple[str, str]]:
    """Return set of (node, interface) for routed interface ports."""
    ports: set[tuple[str, str]] = set()
    for ri in intent.routed_interfaces:
        for ei in intent.edge_interfaces:
            if ei.name == ri.interface and ei.node == node_name:
                ports.add((ei.node, ei.interface))
    return ports


def _format_labels(labels: dict[str, str]) -> dict[str, Any]:
    """Format labels for YAML output.

    Strips the ``eda.nokia.com/`` prefix and converts ``enabled`` -> ``true``.
    Skips the ``role`` label since it's already captured in node identity.
    """
    out: dict[str, Any] = {}
    for k, v in sorted(labels.items()):
        if k.startswith("eda.nokia.com/"):
            short_key = k.removeprefix("eda.nokia.com/")
        else:
            short_key = k
        if short_key == "role":
            continue
        out[short_key] = True if v == "enabled" else v
    return out


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
        "ansible_user": "admin",
        "ansible_password": "NokiaSrl1!",
        "ansible_httpapi_validate_certs": False,
    }


def _build_group_vars_leafs(intent: FabricIntent) -> dict[str, Any]:
    """Build group_vars/leafs.yml with leaf-common BGP/BFD settings."""
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
                    "multipath_max_paths": 2,
                    "advertise_ipv6_next_hops": True,
                    "receive_ipv6_next_hops": True,
                    "rapid_update": True,
                },
                "ipv6_unicast": {
                    "multipath_max_paths": 2,
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
        "routing_policy": {
            "prefix_set": f"prefixset-{intent.fabric_name}",
            "export_policy": f"ebgp-isl-export-policy-{intent.fabric_name}",
            "import_policy": f"ebgp-isl-import-policy-{intent.fabric_name}",
        },
    }


def _build_group_vars_spines(intent: FabricIntent) -> dict[str, Any]:
    """Build group_vars/spines.yml with spine-common BGP/BFD settings."""
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
                    "multipath_max_paths": 6,
                    "advertise_ipv6_next_hops": True,
                    "receive_ipv6_next_hops": True,
                    "rapid_update": True,
                },
                "ipv6_unicast": {
                    "multipath_max_paths": 6,
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
        "routing_policy": {
            "prefix_set": f"prefixset-{intent.fabric_name}",
            "export_policy": f"ebgp-isl-export-policy-{intent.fabric_name}",
            "import_policy": f"ebgp-isl-import-policy-{intent.fabric_name}",
        },
    }


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
        {
            "name": "Show changes",
            "ansible.builtin.debug": {"var": "deploy_result.diff"},
            "when": "deploy_result is defined and deploy_result.changed | default(false)",
        },
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


_ROLES: dict[str, Any] = {
    "detect": _role_detect,
    "topology": lambda: _role_phase("topology"),
    "fabric": lambda: _role_phase("fabric"),
    "services": lambda: _role_phase("services"),
    "purge": _role_purge,
    "configure": _role_configure,
    "confirm": _role_confirm,
}


def _generate_roles(output_dir: Path) -> None:
    """Write roles/<name>/tasks/main.yml for every role."""
    for role_name, task_fn in _ROLES.items():
        role_tasks_dir = output_dir / "roles" / role_name / "tasks"
        role_tasks_dir.mkdir(parents=True, exist_ok=True)
        _write_yaml(role_tasks_dir / "main.yml", task_fn())
    logger.debug("Generated %d roles under %s/roles", len(_ROLES), output_dir)


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


def _readme(fabric_name: str) -> str:
    return textwrap.dedent(f"""\
        # {fabric_name} — Ansible Project

        Auto-generated by the NVD Ansible generator.

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

        ## Day-2 changes

        Edit files under `host_vars/` to add or modify services, then
        re-run the playbook.  Resources removed from intent are automatically
        pruned (set `purge=false` to disable).

        ## Structure

        ```
        .
        ├── ansible.cfg
        ├── inventory.yml
        ├── playbook.yml
        ├── requirements.yml
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
            ├── purge/
            ├── configure/
            └── confirm/
        ```
    """)


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

    logger.info("Generating Ansible project for fabric '%s'", intent.fabric_name)

    node_services = _resolve_placement(intent)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "group_vars").mkdir(exist_ok=True)
    (output_dir / "host_vars").mkdir(exist_ok=True)
    (output_dir / "filter_plugins").mkdir(exist_ok=True)

    _write_yaml(output_dir / "inventory.yml", _build_inventory(intent))

    _write_yaml(output_dir / "group_vars" / "all.yml", _build_group_vars_all(intent))
    _write_yaml(output_dir / "group_vars" / "leafs.yml", _build_group_vars_leafs(intent))
    _write_yaml(output_dir / "group_vars" / "spines.yml", _build_group_vars_spines(intent))

    for node in intent.nodes:
        svc = node_services.get(node.name, _NodeServices())
        if node.role == "leaf":
            hv = _build_leaf_host_vars(node, intent, svc)
        else:
            hv = _build_spine_host_vars(node, intent)
        _write_yaml(output_dir / "host_vars" / f"{node.name}.yml", hv)

    _copy_filter_plugins(output_dir)
    _generate_roles(output_dir)

    _write_yaml(output_dir / "playbook.yml", _playbook_yml(intent.fabric_name))
    _write_text(output_dir / "ansible.cfg", _ansible_cfg())
    _write_yaml(output_dir / "requirements.yml", _requirements_yml())
    _write_text(output_dir / "README.md", _readme(intent.fabric_name))

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
