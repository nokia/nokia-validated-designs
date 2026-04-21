"""
Containerlab topology generator.

Generates a .clab.yml file and Linux client startup scripts from
a FabricIntent. Clients are auto-derived from edge interfaces
(single-homed), LAGs (dual-homed with bonding), and routed
interfaces (dedicated L3 clients).

Each client gets per-VLAN sub-interfaces, IP addressing from IRB
subnets, routing-table isolation, and continuous background ping
traffic to the IRB gateway.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from automation.core.models import (
    ConfigletIntent,
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    RoutedInterfaceIntent,
    VlanIntent,
)
from automation.core.selectors import labels_match

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform → containerlab type mapping
# ---------------------------------------------------------------------------

PLATFORM_TYPE_MAP = {
    "7220 IXR-D3L": "ixr-d3l",
    "7220 IXR-D2L": "ixr-d2l",
    "7220 IXR-D3": "ixr-d3",
    "7220 IXR-D2": "ixr-d2",
    "7220 IXR-D5": "ixr-d5",
    "7220 IXR-H2": "ixr-h2",
    "7220 IXR-H3": "ixr-h3",
    "7250 IXR-6e": "ixr-6e",
    "7250 IXR-10e": "ixr-10e",
    "7250 IXR-6": "ixr-6",
    "7250 IXR-10": "ixr-10",
}

CLIENT_IMAGE = "ghcr.io/srl-labs/network-multitool"
SRL_IMAGE_BASE = "ghcr.io/nokia/srlinux"
CLIENT_LINK_MTU = 9000


# ---------------------------------------------------------------------------
# Data structures for client derivation
# ---------------------------------------------------------------------------


@dataclass
class VlanAttachment:
    """A single VLAN attachment on a client interface.

    IPv4 fields are always populated when an IRB is reachable. IPv6
    fields are populated only when the IRB has an ipv6 entry; clients
    with v6 addresses get additional ``ip -6 addr`` / ``ping6`` lines.
    """

    vlan_name: str
    bridge_domain: str
    vlan_id: str  # "10", "20", "untagged"
    irb_subnet: str = ""  # e.g., "172.16.10.0/24"
    irb_gateway: str = ""  # e.g., "172.16.10.254"
    client_ip: str = ""  # assigned later
    client_mask: int = 24  # prefix length
    # Dual-stack
    irb_subnet6: str = ""  # e.g., "2001:db8:0:10::/64"
    irb_gateway6: str = ""  # e.g., "2001:db8:0:10::254"
    client_ip6: str = ""
    client_mask6: int = 64


@dataclass
class ClientLink:
    """A link from a client eth interface to a leaf port."""

    leaf_node: str
    leaf_interface: str  # e.g., "ethernet-1-3"
    eth_index: int  # 1-based: eth1, eth2, ...
    labels: dict[str, str] = field(default_factory=dict)
    attachments: list[VlanAttachment] = field(default_factory=list)


@dataclass
class RoutedAttachment:
    """A routed (L3) attachment — no bridge domain, direct IP.

    Supports dual-stack by optionally populating the IPv6 fields; when
    populated, the generated client script adds ``ip -6 addr`` lines
    and a continuous IPv6 ping to the gateway.
    """

    name: str
    client_ip: str
    client_mask: int
    gateway: str
    router: str = ""
    loopback_ips: list[str] = field(default_factory=list)
    # Dual-stack
    client_ip6: str = ""
    gateway6: str = ""
    client_mask6: int = 64


@dataclass
class ClientNode:
    """A Linux client node in the containerlab topology."""

    name: str  # e.g., "cl-l1", "cl-l4l5", "cl-s5"
    is_bonded: bool = False
    links: list[ClientLink] = field(default_factory=list)
    routed: RoutedAttachment | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(intent: FabricIntent, output_dir: Path) -> Path:
    """
    Generate containerlab topology and client configs.

    Returns:
        Path to the generated .clab.yml file.
    """
    clients = _derive_clients(intent)
    _match_vlans_to_clients(clients, intent)
    _allocate_ips(clients, intent)

    needs_node_isolation = _configlets_need_node_isolation(intent.configlets)
    clab_topo = _build_clab_topology(intent, clients, needs_node_isolation)

    # Write clab file
    output_dir.mkdir(parents=True, exist_ok=True)
    clab_path = output_dir / f"{intent.fabric_name}.clab.yml"
    with open(clab_path, "w") as f:
        yaml.dump(clab_topo, f, default_flow_style=False, sort_keys=False)
    logger.info("Wrote containerlab topology to %s", clab_path)

    # Write client startup scripts
    config_dir = output_dir / "client-configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    for client in clients:
        script = _generate_client_script(client)
        script_path = config_dir / f"{client.name}.sh"
        with open(script_path, "w") as f:
            f.write(script)
    logger.info(
        "Wrote %d client configs to %s", len(clients), config_dir
    )

    # Copy node-isolation.py to output dir if configlets require it
    if needs_node_isolation:
        _copy_node_isolation_script(output_dir)

    # Generate overlay validation script
    _generate_validate_overlay(clients, intent, output_dir)

    return clab_path


# ---------------------------------------------------------------------------
# Client derivation
# ---------------------------------------------------------------------------


def _derive_clients(intent: FabricIntent) -> list[ClientNode]:
    """
    Derive Linux client nodes from edge interfaces, LAGs,
    and routed interfaces.
    """
    clients: list[ClientNode] = []

    # Collect all LAG member (node, interface) pairs → exclude from single-homed
    lag_member_ports: set[tuple[str, str]] = set()
    for lag in intent.lags:
        for m in lag.members:
            lag_member_ports.add((m.node, m.interface))

    # Collect all ISL endpoint (node, interface) pairs. LAGs whose
    # members are all ISL endpoints are internal fabric LAGs (typical
    # of collapsed-spine: spine↔tor uplinks that run LACP on both
    # sides) and must not produce a synthetic clab client.
    isl_ports: set[tuple[str, str]] = set()
    for link in intent.links:
        isl_ports.add((link.local_node, link.local_interface))
        isl_ports.add((link.remote_node, link.remote_interface))

    # Collect routed interface ports → separate dedicated clients
    ei_by_name: dict[str, EdgeInterfaceIntent] = {e.name: e for e in intent.edge_interfaces}
    routed_ports: dict[tuple[str, str], RoutedInterfaceIntent] = {}
    for ri in intent.routed_interfaces:
        # ri.interface is the EDA resource name, e.g., "leaf1-ethernet-1-4"
        ei = ei_by_name.get(ri.interface)
        if ei:
            routed_ports[(ei.node, ei.interface)] = ri

    # --- Single-homed clients ---
    # Group edge interfaces by node (excluding LAG members and routed ports)
    leaf_edges: dict[str, list[EdgeInterfaceIntent]] = {}
    for ei in intent.edge_interfaces:
        key = (ei.node, ei.interface)
        if key in lag_member_ports or key in routed_ports:
            continue
        leaf_edges.setdefault(ei.node, []).append(ei)

    for node_name, edges in sorted(leaf_edges.items()):
        # Derive a short, per-node client suffix so that designs with
        # multiple node families (e.g. collapsed-spine has both
        # "spine1" and "tor1") do not collide on the same ``cl-1``
        # name.
        client_name = f"cl-{_node_short_name(node_name)}"

        links = []
        for idx, ei in enumerate(sorted(edges, key=lambda e: e.interface), start=1):
            links.append(
                ClientLink(
                    leaf_node=ei.node,
                    leaf_interface=ei.interface,
                    eth_index=idx,
                    labels=dict(ei.labels),
                )
            )

        clients.append(ClientNode(name=client_name, is_bonded=False, links=links))

    # --- Dual-homed clients (LAGs) ---
    # A design can have multiple LAGs across the same pair of nodes
    # (e.g. two different server LAGs dual-homed to spine1+spine2).
    # Pre-compute per-base-name counts so we only append a disambiguator
    # when there's a collision.
    def _is_fabric_lag(lag: LagIntent) -> bool:
        """A LAG whose every member port is also an ISL endpoint is an
        internal fabric LAG (spine↔tor, leaf↔leaf) and must not be
        rendered as a stand-alone clab client.
        """
        return all((m.node, m.interface) in isl_ports for m in lag.members)

    server_lags = [lag for lag in intent.lags if not _is_fabric_lag(lag)]

    base_to_lag_names: dict[str, list[str]] = {}
    for lag in server_lags:
        members = sorted(lag.members, key=lambda m: m.node)
        short_parts: list[str] = []
        for m in members:
            part = _node_short_name(m.node)
            if part and part not in short_parts:
                short_parts.append(part)
        base = "cl-" + "".join(short_parts)
        base_to_lag_names.setdefault(base, []).append(lag.name)

    for lag in server_lags:
        members = sorted(lag.members, key=lambda m: m.node)
        short_parts = []
        for m in members:
            part = _node_short_name(m.node)
            if part and part not in short_parts:
                short_parts.append(part)
        base = "cl-" + "".join(short_parts)
        if len(base_to_lag_names[base]) > 1:
            idx = base_to_lag_names[base].index(lag.name) + 1
            client_name = f"{base}-{idx}"
        else:
            client_name = base

        links = []
        for idx, m in enumerate(members, start=1):
            links.append(
                ClientLink(
                    leaf_node=m.node,
                    leaf_interface=m.interface,
                    eth_index=idx,
                    labels=dict(lag.labels),
                )
            )

        clients.append(ClientNode(name=client_name, is_bonded=True, links=links))

    # --- Routed interface clients ---
    for (node_name, phys_intf), ri in sorted(routed_ports.items()):
        # Derive client name from routed interface name
        client_name = f"cl-{ri.name}"

        # Get the client-side IP from the routed interface's IPv4 addresses
        client_ip = ""
        client_mask = 31
        gateway = ""
        if ri.ipv4_addresses:
            addr = ri.ipv4_addresses[0]
            ip_prefix = addr.get("ipPrefix", addr.get("ip_prefix", ""))
            if ip_prefix:
                net = ipaddress.ip_interface(ip_prefix)
                # Router has .0, client gets .1 (for /31)
                if net.network.prefixlen == 31:
                    hosts = list(net.network.hosts())
                    gateway = str(net.ip)
                    client_ip = str(hosts[1] if net.ip == hosts[0] else hosts[0])
                    client_mask = 31
                else:
                    # For larger subnets, client gets gateway+1
                    gateway = str(net.ip)
                    client_ip = str(net.ip + 1)
                    client_mask = net.network.prefixlen

        # Dual-stack: derive IPv6 client address from the routed
        # interface's IPv6 prefix. Router keeps the declared address;
        # client gets gateway+1.
        client_ip6 = ""
        client_mask6 = 64
        gateway6 = ""
        if ri.ipv6_addresses:
            addr6 = ri.ipv6_addresses[0]
            ip_prefix6 = addr6.get("ipPrefix", addr6.get("ip_prefix", ""))
            if ip_prefix6:
                net6 = ipaddress.ip_interface(ip_prefix6)
                gateway6 = str(net6.ip)
                client_ip6 = str(net6.ip + 1)
                client_mask6 = net6.network.prefixlen

        link = ClientLink(
            leaf_node=node_name,
            leaf_interface=phys_intf,
            eth_index=1,
            labels={},
        )
        loopback_ips = _loopback_ips_for_routed_client(client_ip, intent)
        routed_att = RoutedAttachment(
            name=ri.name,
            client_ip=client_ip,
            client_mask=client_mask,
            gateway=gateway,
            router=ri.router,
            loopback_ips=loopback_ips,
            client_ip6=client_ip6,
            gateway6=gateway6,
            client_mask6=client_mask6,
        )
        clients.append(
            ClientNode(name=client_name, is_bonded=False, links=[link], routed=routed_att)
        )

    # Final collision guard: different edge types (single-homed edges,
    # LAGs, routed) can independently derive the same client name
    # (e.g. ``cl-t1`` from both a single-homed tor1 edge and a
    # single-chassis tor1 LAG). Rename duplicates with incrementing
    # suffixes so that every client gets its own startup script and
    # clab node.
    seen: dict[str, int] = {}
    for cl in clients:
        if cl.name in seen:
            seen[cl.name] += 1
            cl.name = f"{cl.name}-{seen[cl.name]}"
        else:
            seen[cl.name] = 1

    return clients


def _loopback_ips_for_routed_client(client_ip: str, intent: FabricIntent) -> list[str]:
    """Collect first-host IPs from static route prefixes whose nexthop matches *client_ip*."""
    ips: list[str] = []
    for sr in intent.static_routes:
        nhg = sr.nexthop_group
        nexthops = nhg.get("nexthops", [])
        if any(nh.get("ipPrefix", "") == client_ip for nh in nexthops):
            for prefix in sr.prefixes:
                net = ipaddress.ip_network(prefix, strict=False)
                first_host = next(net.hosts(), None)
                if first_host:
                    ips.append(str(first_host))
    return ips


# ---------------------------------------------------------------------------
# VLAN → client matching
# ---------------------------------------------------------------------------


def _match_vlans_to_clients(clients: list[ClientNode], intent: FabricIntent) -> None:
    """Match VLANs to client links via label selectors."""

    # Build IRB lookup: bridge_domain → IrbInterfaceIntent
    irb_by_bd: dict[str, IrbInterfaceIntent] = {}
    for irb in intent.irb_interfaces:
        irb_by_bd[irb.bridge_domain] = irb

    for client in clients:
        if client.routed:
            continue  # Routed clients don't use VLAN matching

        # For bonded clients, all links share the same labels (from the LAG),
        # so only match on the first link to avoid duplicate attachments.
        links_to_match = [client.links[0]] if client.is_bonded else client.links

        for link in links_to_match:
            for vlan in intent.vlans:
                if labels_match(vlan.interface_selector, link.labels):
                    irb = irb_by_bd.get(vlan.bridge_domain)
                    subnet = ""
                    gateway = ""
                    subnet6 = ""
                    gateway6 = ""
                    mask6 = 64
                    if irb:
                        if irb.ipv4:
                            iface = ipaddress.ip_interface(irb.ipv4)
                            subnet = str(iface.network)
                            gateway = str(iface.ip)
                        if irb.ip_addresses:
                            for addr in irb.ip_addresses:
                                if addr.ipv4 and not subnet:
                                    pfx = addr.ipv4.get("ip_prefix", "")
                                    if pfx:
                                        iface = ipaddress.ip_interface(pfx)
                                        subnet = str(iface.network)
                                        gateway = str(iface.ip)
                                if addr.ipv6 and not subnet6:
                                    pfx6 = addr.ipv6.get("ip_prefix", "")
                                    if pfx6:
                                        iface6 = ipaddress.ip_interface(pfx6)
                                        subnet6 = str(iface6.network)
                                        gateway6 = str(iface6.ip)
                                        mask6 = iface6.network.prefixlen

                    link.attachments.append(
                        VlanAttachment(
                            vlan_name=vlan.name,
                            bridge_domain=vlan.bridge_domain,
                            vlan_id=vlan.vlan_id,
                            irb_subnet=subnet,
                            irb_gateway=gateway,
                            client_mask=ipaddress.ip_network(subnet).prefixlen
                            if subnet
                            else 24,
                            irb_subnet6=subnet6,
                            irb_gateway6=gateway6,
                            client_mask6=mask6,
                        )
                    )



# ---------------------------------------------------------------------------
# IP allocation
# ---------------------------------------------------------------------------


def _allocate_ips(clients: list[ClientNode], intent: FabricIntent) -> None:
    """Allocate unique IPv4 (and IPv6, when present) addresses per subnet."""
    subnet_counters: dict[str, int] = {}  # IPv4 subnet → next offset
    subnet6_counters: dict[str, int] = {}  # IPv6 subnet → next offset

    for client in clients:
        for link in client.links:
            for att in link.attachments:
                if att.irb_subnet:
                    net = ipaddress.ip_network(att.irb_subnet)
                    offset = subnet_counters.get(att.irb_subnet, 1)
                    att.client_ip = str(net.network_address + offset)
                    att.client_mask = net.prefixlen
                    subnet_counters[att.irb_subnet] = offset + 1
                if att.irb_subnet6:
                    net6 = ipaddress.ip_network(att.irb_subnet6)
                    # IPv6 client offsets start at 1 (::1, ::2, ...).
                    offset6 = subnet6_counters.get(att.irb_subnet6, 1)
                    att.client_ip6 = str(net6.network_address + offset6)
                    att.client_mask6 = net6.prefixlen
                    subnet6_counters[att.irb_subnet6] = offset6 + 1


# ---------------------------------------------------------------------------
# Containerlab topology builder
# ---------------------------------------------------------------------------


def _build_clab_topology(
    intent: FabricIntent,
    clients: list[ClientNode],
    needs_node_isolation: bool = False,
) -> dict:
    """Build the containerlab topology dict."""

    # Determine SRL version and platform from nodes
    srl_version = intent.nodes[0].version if intent.nodes else "24.10.2"
    srl_image = f"{SRL_IMAGE_BASE}:{srl_version}"

    # Determine mgmt subnet from node IPs
    mgmt_subnet = _derive_mgmt_subnet(intent)

    # --- Nodes ---
    nodes: dict[str, Any] = {}

    # SR Linux nodes
    for node in sorted(intent.nodes, key=lambda n: n.name):
        platform_type = PLATFORM_TYPE_MAP.get(node.platform, "ixr-d3l")
        node_def: dict[str, Any] = {"mgmt-ipv4": node.mgmt_ipv4}
        # Only specify type if it differs from the default
        node_def["type"] = platform_type
        nodes[node.name] = node_def

    # Allocate static mgmt IPs for clients to avoid overlap with SR Linux nodes
    client_ips = _allocate_client_mgmt_ips(intent, len(clients), mgmt_subnet)

    # Linux client nodes
    for idx, client in enumerate(sorted(clients, key=lambda c: c.name)):
        nodes[client.name] = {
            "kind": "linux",
            "mgmt-ipv4": client_ips[idx],
            "exec": [f"bash /client-configs/{client.name}.sh"],
        }

    # --- Links ---
    links: list[dict[str, list[str]]] = []

    # ISL links (leaf ↔ spine)
    for link in sorted(intent.links, key=lambda l: l.name):
        endpoints = [
            f"{link.local_node}:{_to_clab_intf(link.local_interface)}",
            f"{link.remote_node}:{_to_clab_intf(link.remote_interface)}",
        ]
        links.append({"endpoints": endpoints})

    # Client links
    for client in clients:
        for cl in client.links:
            endpoints = [
                f"{cl.leaf_node}:{_to_clab_intf(cl.leaf_interface)}",
                f"{client.name}:eth{cl.eth_index}",
            ]
            links.append({"endpoints": endpoints})

    # --- SR Linux kind definition ---
    srl_kind: dict[str, Any] = {"image": srl_image}
    if needs_node_isolation:
        srl_kind["binds"] = [
            "node-isolation.py:/etc/opt/srlinux/eventmgr/node-isolation.py"
        ]

    # --- Assemble topology ---
    topo: dict[str, Any] = {
        "name": intent.fabric_name,
        "prefix": "",
        "mgmt": {
            "network": "eda_mgmt",
            "ipv4-subnet": mgmt_subnet,
        },
        "topology": {
            "defaults": {"kind": "nokia_srlinux"},
            "kinds": {
                "nokia_srlinux": srl_kind,
                "linux": {
                    "image": CLIENT_IMAGE,
                    "binds": ["client-configs:/client-configs"],
                },
            },
            "nodes": nodes,
            "links": links,
        },
    }

    return topo


# ---------------------------------------------------------------------------
# Client startup script generation
# ---------------------------------------------------------------------------


def _generate_client_script(client: ClientNode) -> str:
    """Generate a bash startup script for a client node."""
    lines: list[str] = ["#!/bin/bash", f"# Auto-generated startup for {client.name}", ""]

    if client.routed:
        ra = client.routed
        lines.append("# Routed interface (direct L3)")
        lines.append(f"ip link set dev eth1 mtu {CLIENT_LINK_MTU}")
        if ra.client_ip:
            lines.append(f"ip addr add {ra.client_ip}/{ra.client_mask} dev eth1")
        if ra.client_ip6:
            lines.append(
                f"ip -6 addr add {ra.client_ip6}/{ra.client_mask6} dev eth1"
            )
        lines.append("")
        if ra.loopback_ips:
            lines.append("# Loopback IPs for static route targets")
            for lip in ra.loopback_ips:
                net = ipaddress.ip_interface(lip)
                lines.append(f"ip addr add {lip}/{net.network.max_prefixlen} dev lo")
            lines.append("")
        lines.append("# Policy routing so source-bound traffic uses the fabric")
        if ra.client_ip and ra.gateway:
            lines.append(f"ip route add default via {ra.gateway} dev eth1 table 10")
            lines.append(f"ip rule add from {ra.client_ip} table 10")
        if ra.client_ip6 and ra.gateway6:
            lines.append(
                f"ip -6 route add default via {ra.gateway6} dev eth1 table 10"
            )
            lines.append(f"ip -6 rule add from {ra.client_ip6} table 10")
        for lip in ra.loopback_ips:
            lines.append(f"ip rule add from {lip} table 10")
        lines.append("")
        lines.append("# Continuous traffic")
        if ra.gateway:
            lines.append(
                f"nohup ping -i 0.5 -I eth1 {ra.gateway} > /dev/null 2>&1 &"
            )
        if ra.gateway6:
            lines.append(
                f"nohup ping -6 -i 0.5 -I eth1 {ra.gateway6} > /dev/null 2>&1 &"
            )
        return "\n".join(lines) + "\n"

    if client.is_bonded:
        return _generate_bonded_script(client, lines)
    else:
        return _generate_single_homed_script(client, lines)


def _generate_single_homed_script(
    client: ClientNode, lines: list[str]
) -> str:
    """Generate script for a single-homed (non-bonded) client."""

    # Collect all tagged VLANs across all interfaces
    for link in client.links:
        iface = f"eth{link.eth_index}"
        tagged = [a for a in link.attachments if a.vlan_id not in ("untagged", "null")]
        untagged = [a for a in link.attachments if a.vlan_id in ("untagged", "null")]

        lines.append(f"# Interface setup: {iface}")
        lines.append(f"ip link set dev {iface} mtu {CLIENT_LINK_MTU}")
        if tagged:
            lines.append(f"ip link set dev {iface} down")
            for att in tagged:
                sub = f"{iface}.{att.vlan_id}"
                lines.append(
                    f"ip link add link {iface} name {sub} type vlan id {att.vlan_id}"
                )
            lines.append(f"ip link set dev {iface} up")
            for att in tagged:
                sub = f"{iface}.{att.vlan_id}"
                lines.append(f"ip link set dev {sub} up")
            lines.append("")

        # Assign IPs (v4 and, when present, v6)
        for att in untagged:
            if att.client_ip:
                lines.append(
                    f"ip addr add {att.client_ip}/{att.client_mask} dev {iface}"
                )
            if att.client_ip6:
                lines.append(
                    f"ip -6 addr add {att.client_ip6}/{att.client_mask6} dev {iface}"
                )
        for att in tagged:
            sub = f"{iface}.{att.vlan_id}"
            if att.client_ip:
                lines.append(
                    f"ip addr add {att.client_ip}/{att.client_mask} dev {sub}"
                )
            if att.client_ip6:
                lines.append(
                    f"ip -6 addr add {att.client_ip6}/{att.client_mask6} dev {sub}"
                )

    # Default routes per subnet (using routing tables for isolation)
    lines.append("")
    lines.append("# Per-subnet routing tables for isolation")
    table_id = 10
    all_attachments = []
    for link in client.links:
        for att in link.attachments:
            if not (att.client_ip or att.client_ip6):
                continue
            iface = f"eth{link.eth_index}"
            if att.vlan_id not in ("untagged", "null"):
                iface = f"{iface}.{att.vlan_id}"
            if att.client_ip and att.irb_gateway:
                lines.append(
                    f"ip route add default via {att.irb_gateway} dev {iface} table {table_id}"
                )
                lines.append(
                    f"ip rule add from {att.client_ip} table {table_id}"
                )
            if att.client_ip6 and att.irb_gateway6:
                lines.append(
                    f"ip -6 route add default via {att.irb_gateway6} dev {iface} table {table_id}"
                )
                lines.append(
                    f"ip -6 rule add from {att.client_ip6} table {table_id}"
                )
            all_attachments.append((iface, att))
            table_id += 10

    # Traffic generation
    lines.append("")
    lines.append("# Continuous traffic")
    for iface, att in all_attachments:
        if att.irb_gateway:
            lines.append(
                f"nohup ping -i 0.5 -I {iface} {att.irb_gateway} > /dev/null 2>&1 &"
            )
        if att.irb_gateway6:
            lines.append(
                f"nohup ping -6 -i 0.5 -I {iface} {att.irb_gateway6} > /dev/null 2>&1 &"
            )

    return "\n".join(lines) + "\n"


def _generate_bonded_script(client: ClientNode, lines: list[str]) -> str:
    """Generate script for a dual-homed (bonded) client."""
    member_count = len(client.links)

    lines.append("# Bond setup (LACP)")
    lines.append("ip link add bond0 type bond mode 802.3ad")
    for link in client.links:
        iface = f"eth{link.eth_index}"
        lines.append(f"ip link set dev {iface} down")
        lines.append(f"ip link set dev {iface} mtu {CLIENT_LINK_MTU}")
    for link in client.links:
        iface = f"eth{link.eth_index}"
        lines.append(f"ip link set {iface} master bond0")
    lines.append(f"ip link set dev bond0 mtu {CLIENT_LINK_MTU}")
    for link in client.links:
        iface = f"eth{link.eth_index}"
        lines.append(f"ip link set dev {iface} up")
    lines.append("ip link set dev bond0 up")
    lines.append("")

    # All links in a bond share the same labels, use first link's attachments
    # (labels were copied from the LAG, so all links have the same attachments)
    attachments = client.links[0].attachments if client.links else []

    # Create tagged VLAN sub-interfaces on bond0
    tagged = [a for a in attachments if a.vlan_id not in ("untagged", "null")]
    untagged = [a for a in attachments if a.vlan_id in ("untagged", "null")]

    if tagged:
        lines.append("# VLAN sub-interfaces on bond0")
        for att in tagged:
            sub = f"bond0.{att.vlan_id}"
            lines.append(
                f"ip link add link bond0 name {sub} type vlan id {att.vlan_id}"
            )
            lines.append(f"ip link set dev {sub} up")
        lines.append("")

    # Assign IPs (v4 and, when present, v6)
    lines.append("# IP addressing")
    for att in untagged:
        if att.client_ip:
            lines.append(f"ip addr add {att.client_ip}/{att.client_mask} dev bond0")
        if att.client_ip6:
            lines.append(
                f"ip -6 addr add {att.client_ip6}/{att.client_mask6} dev bond0"
            )
    for att in tagged:
        sub = f"bond0.{att.vlan_id}"
        if att.client_ip:
            lines.append(f"ip addr add {att.client_ip}/{att.client_mask} dev {sub}")
        if att.client_ip6:
            lines.append(
                f"ip -6 addr add {att.client_ip6}/{att.client_mask6} dev {sub}"
            )

    # Routes per subnet
    lines.append("")
    lines.append("# Per-subnet routing tables")
    table_id = 10
    all_attachments_iface = []
    for att in untagged + tagged:
        if not (att.client_ip or att.client_ip6):
            continue
        iface = "bond0"
        if att.vlan_id not in ("untagged", "null"):
            iface = f"bond0.{att.vlan_id}"
        if att.client_ip and att.irb_gateway:
            lines.append(
                f"ip route add default via {att.irb_gateway} dev {iface} table {table_id}"
            )
            lines.append(f"ip rule add from {att.client_ip} table {table_id}")
        if att.client_ip6 and att.irb_gateway6:
            lines.append(
                f"ip -6 route add default via {att.irb_gateway6} dev {iface} table {table_id}"
            )
            lines.append(f"ip -6 rule add from {att.client_ip6} table {table_id}")
        all_attachments_iface.append((iface, att))
        table_id += 10

    # Traffic
    lines.append("")
    lines.append("# Continuous traffic")
    for iface, att in all_attachments_iface:
        if att.irb_gateway:
            lines.append(
                f"nohup ping -i 0.5 -I {iface} {att.irb_gateway} > /dev/null 2>&1 &"
            )
        if att.irb_gateway6:
            lines.append(
                f"nohup ping -6 -i 0.5 -I {iface} {att.irb_gateway6} > /dev/null 2>&1 &"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configlets_need_node_isolation(configlets: list[ConfigletIntent]) -> bool:
    """Check if any configlet references the node-isolation.py event-handler script."""
    for configlet in configlets:
        for entry in configlet.configs:
            if "node-isolation.py" in entry.config:
                return True
    return False


def _generate_validate_overlay(
    clients: list[ClientNode], intent: FabricIntent, output_dir: Path
) -> None:
    """Generate a parallel overlay validation script from the client/intent data."""

    # ── index structures ────────────────────────────────────────────────
    # bridge_domain → router name (via IRB interfaces)
    bd_to_router: dict[str, str] = {}
    for irb in intent.irb_interfaces:
        bd_to_router[irb.bridge_domain] = irb.router

    # Collect (client_name, src_ip, bridge_domain, router) for every
    # VLAN attachment across all clients. Each attachment generates up
    # to two subnet entries: one for IPv4 and one for IPv6 (dual-stack).
    @dataclass
    class _ClientSubnet:
        client: str
        ip: str
        bd: str
        router: str
        family: str = "ipv4"  # or "ipv6"

    subnets: list[_ClientSubnet] = []
    for cl in clients:
        if cl.routed:
            continue
        for link in cl.links:
            for att in link.attachments:
                router = bd_to_router.get(att.bridge_domain, "")
                if att.client_ip:
                    subnets.append(
                        _ClientSubnet(
                            client=cl.name,
                            ip=att.client_ip,
                            bd=att.bridge_domain,
                            router=router,
                            family="ipv4",
                        )
                    )
                if att.client_ip6:
                    subnets.append(
                        _ClientSubnet(
                            client=cl.name,
                            ip=att.client_ip6,
                            bd=att.bridge_domain,
                            router=router,
                            family="ipv6",
                        )
                    )

    v4_subnets = [s for s in subnets if s.family == "ipv4"]
    v6_subnets = [s for s in subnets if s.family == "ipv6"]

    # Routed clients
    routed_clients = [cl for cl in clients if cl.routed]

    # ── script assembly ─────────────────────────────────────────────────
    L = []  # output lines

    L.append(_VALIDATE_HEADER)

    # --- Convergence wait using first v4 client ---
    if v4_subnets:
        s0 = v4_subnets[0]
        gw0 = _gateway_for_subnet(s0.bd, intent)
        L.append("if ! $NO_WAIT; then")
        L.append(f'  wait_for_convergence "{s0.client}" "{s0.ip}" "{gw0}" 120')
        L.append("fi")
        L.append("")

    # --- Gateway reachability (IPv4, then IPv6) ---
    L.append('sec_start=$((_seq + 1))')
    for s in v4_subnets:
        gw = _gateway_for_subnet(s.bd, intent)
        if not gw:
            continue
        L.append(
            f'enqueue_ping "{s.client}" "{s.ip}" "{gw}" '
            f'"{s.client} → gw {gw} ({s.bd})"'
        )
    for cl in routed_clients:
        ra = cl.routed
        assert ra is not None
        if ra.client_ip and ra.gateway:
            L.append(
                f'enqueue_ping "{cl.name}" "{ra.client_ip}" "{ra.gateway}" '
                f'"{cl.name} → gw {ra.gateway} (routed)"'
            )
    L.append('flush_section "Gateway reachability (IPv4)" "$sec_start"')
    L.append("")

    if v6_subnets or any(
        cl.routed and cl.routed.client_ip6 and cl.routed.gateway6
        for cl in routed_clients
    ):
        L.append('sec_start=$((_seq + 1))')
        for s in v6_subnets:
            gw6 = _gateway6_for_subnet(s.bd, intent)
            if not gw6:
                continue
            L.append(
                f'enqueue_ping6 "{s.client}" "{s.ip}" "{gw6}" '
                f'"{s.client} → gw {gw6} ({s.bd})"'
            )
        for cl in routed_clients:
            ra = cl.routed
            assert ra is not None
            if ra.client_ip6 and ra.gateway6:
                L.append(
                    f'enqueue_ping6 "{cl.name}" "{ra.client_ip6}" "{ra.gateway6}" '
                    f'"{cl.name} → gw {ra.gateway6} (routed)"'
                )
        L.append('flush_section "Gateway reachability (IPv6)" "$sec_start"')
        L.append("")

    L.append("if $QUICK; then")
    L.append('  echo ""')
    L.append('  echo "━━━ Summary (quick mode) ━━━"')
    L.append('  echo "  Passed: $TOTAL_PASS  Failed: $TOTAL_FAIL  Skipped: 0"')
    L.append('  [ "$TOTAL_FAIL" -eq 0 ]')
    L.append("  exit")
    L.append("fi")
    L.append("")

    # --- L2 intra-subnet (clients sharing the same bridge domain) ---
    def _emit_l2(family_subnets: list[_ClientSubnet], family: str) -> None:
        if len(family_subnets) < 2:
            return
        bd_clients: dict[str, list[_ClientSubnet]] = {}
        for s in family_subnets:
            bd_clients.setdefault(s.bd, []).append(s)
        ping_fn = "enqueue_bidir" if family == "ipv4" else "enqueue_bidir6"
        L.append('sec_start=$((_seq + 1))')
        for bd, members in sorted(bd_clients.items()):
            if len(members) < 2:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    L.append(
                        f'{ping_fn} "{a.client}" "{a.ip}" '
                        f'"{b.client}" "{b.ip}" "L2 {bd}"'
                    )
        L.append(
            f'flush_section "L2 intra-subnet (same bridge domain, {family.upper()})" '
            '"$sec_start"'
        )
        L.append("")

    _emit_l2(v4_subnets, "ipv4")
    _emit_l2(v6_subnets, "ipv6")

    # --- L3 inter-subnet (clients in different BDs of the same router) ---
    def _emit_l3(family_subnets: list[_ClientSubnet], family: str) -> None:
        ping_fn = "enqueue_bidir" if family == "ipv4" else "enqueue_bidir6"
        bd_clients: dict[str, list[_ClientSubnet]] = {}
        router_bds: dict[str, list[str]] = {}
        for s in family_subnets:
            bd_clients.setdefault(s.bd, []).append(s)
            if s.router:
                router_bds.setdefault(s.router, [])
                if s.bd not in router_bds[s.router]:
                    router_bds[s.router].append(s.bd)
        # nothing to test if no router has 2+ BDs
        if not any(len(bds) >= 2 for bds in router_bds.values()):
            return
        L.append('sec_start=$((_seq + 1))')
        for router, bds in sorted(router_bds.items()):
            if len(bds) < 2:
                continue
            for i, bd_a in enumerate(bds):
                for bd_b in bds[i + 1 :]:
                    a = bd_clients[bd_a][0]
                    b = bd_clients[bd_b][0]
                    L.append(
                        f'{ping_fn} "{a.client}" "{a.ip}" '
                        f'"{b.client}" "{b.ip}" '
                        f'"L3 {router} ({a.bd} ↔ {b.bd})"'
                    )
        L.append(
            f'flush_section "L3 inter-subnet (same router, different subnet, '
            f'{family.upper()})" "$sec_start"'
        )
        L.append("")

    _emit_l3(v4_subnets, "ipv4")
    _emit_l3(v6_subnets, "ipv6")

    # --- Routed interfaces and static routes (IPv4 only) ---
    if routed_clients or intent.static_routes:
        emitted_header = False
        for cl in routed_clients:
            ra = cl.routed
            assert ra is not None
            if not ra.client_ip:
                continue
            tested: set[str] = set()
            for s in v4_subnets:
                if ra.router and s.router != ra.router:
                    continue
                key = f"{cl.name}-{s.bd}"
                if key in tested:
                    continue
                tested.add(key)
                if not emitted_header:
                    L.append('sec_start=$((_seq + 1))')
                    emitted_header = True
                L.append(
                    f'enqueue_bidir "{cl.name}" "{ra.client_ip}" '
                    f'"{s.client}" "{s.ip}" '
                    f'"routed {ra.name} ↔ {s.bd}"'
                )

        for sr in intent.static_routes:
            nexthops = sr.nexthop_group.get("nexthops", [])
            nh_ip = nexthops[0].get("ipPrefix", "") if nexthops else ""
            src = next(
                (s for s in v4_subnets if s.router == sr.router), None
            )
            if not src:
                continue
            for prefix in sr.prefixes:
                net = ipaddress.ip_network(prefix, strict=False)
                first_host = next(net.hosts(), None)
                if not first_host:
                    continue
                target_name = ""
                for cl in routed_clients:
                    if cl.routed and cl.routed.client_ip == nh_ip:
                        target_name = cl.name
                        break
                label = (
                    f"static route {prefix} via {target_name or nh_ip} "
                    f"(from {src.client})"
                )
                if not emitted_header:
                    L.append('sec_start=$((_seq + 1))')
                    emitted_header = True
                L.append(
                    f'enqueue_ping "{src.client}" "{src.ip}" '
                    f'"{first_host}" "{label}"'
                )
        if emitted_header:
            L.append(
                'flush_section "Routed interfaces and static routes" "$sec_start"'
            )
            L.append("")

    # --- Summary ---
    L.append('echo ""')
    L.append('echo "━━━ Summary ━━━"')
    L.append('echo "  Passed: $TOTAL_PASS  Failed: $TOTAL_FAIL  Skipped: 0"')
    L.append('[ "$TOTAL_FAIL" -eq 0 ]')

    script_path = output_dir / "validate-overlay.sh"
    script_path.write_text("\n".join(L) + "\n")
    logger.info("Wrote validation script to %s", script_path)


def _gateway_for_subnet(bridge_domain: str, intent: FabricIntent) -> str:
    """Return the anycast-gw IPv4 for a bridge domain."""
    for irb in intent.irb_interfaces:
        if irb.bridge_domain != bridge_domain:
            continue
        if irb.ipv4:
            return irb.ipv4.split("/")[0]
        if irb.ip_addresses:
            for addr in irb.ip_addresses:
                if addr.ipv4 and addr.ipv4.get("ip_prefix"):
                    return addr.ipv4["ip_prefix"].split("/")[0]
    return ""


def _gateway6_for_subnet(bridge_domain: str, intent: FabricIntent) -> str:
    """Return the anycast-gw IPv6 for a bridge domain."""
    for irb in intent.irb_interfaces:
        if irb.bridge_domain != bridge_domain:
            continue
        if irb.ip_addresses:
            for addr in irb.ip_addresses:
                if addr.ipv6 and addr.ipv6.get("ip_prefix"):
                    return addr.ipv6["ip_prefix"].split("/")[0]
    return ""


_VALIDATE_HEADER = r"""#!/bin/bash
# Auto-generated overlay validation script
# Usage: ./validate-overlay.sh [--quick] [--verbose] [--no-wait]

set -euo pipefail

QUICK=false
VERBOSE=false
NO_WAIT=false

for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=true ;;
    --verbose) VERBOSE=true ;;
    --no-wait) NO_WAIT=true ;;
  esac
done

RESULT_DIR=$(mktemp -d)
trap 'rm -rf "$RESULT_DIR"' EXIT

exec_on() {
  local node="$1"; shift
  docker exec "$node" "$@" 2>/dev/null
}

wait_for_convergence() {
  local src="$1" src_ip="$2" dst_ip="$3" max_wait="${4:-120}"
  local elapsed=0 interval=5
  echo "Waiting for overlay convergence (${src} ${src_ip} → ${dst_ip}, timeout ${max_wait}s)..."
  while [ "$elapsed" -lt "$max_wait" ]; do
    if exec_on "$src" ping -c1 -W2 -I "$src_ip" "$dst_ip" > /dev/null 2>&1; then
      echo "  Overlay reachable after ${elapsed}s"
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
    echo "  ${elapsed}s — not yet reachable, retrying..."
  done
  echo "  ⚠️  Timed out after ${max_wait}s — proceeding anyway"
  return 0
}

_seq=0
enqueue_ping() {
  local src="$1" src_ip="$2" dst_ip="$3" label="$4"
  _seq=$((_seq + 1))
  local id=$_seq
  (
    if $VERBOSE; then echo "    cmd: docker exec $src ping -c1 -W2 -I $src_ip $dst_ip"; fi
    if exec_on "$src" ping -c1 -W2 -I "$src_ip" "$dst_ip" > /dev/null 2>&1; then
      echo "pass" > "$RESULT_DIR/${id}.rc"
    else
      echo "fail" > "$RESULT_DIR/${id}.rc"
    fi
    echo "$label" > "$RESULT_DIR/${id}.label"
  ) &
}

enqueue_bidir() {
  local a="$1" a_ip="$2" b="$3" b_ip="$4" label="$5"
  enqueue_ping "$a" "$a_ip" "$b_ip" "$label ($a → $b)"
  enqueue_ping "$b" "$b_ip" "$a_ip" "$label ($b → $a)"
}

enqueue_ping6() {
  local src="$1" src_ip="$2" dst_ip="$3" label="$4"
  _seq=$((_seq + 1))
  local id=$_seq
  (
    if $VERBOSE; then echo "    cmd: docker exec $src ping -6 -c1 -W2 -I $src_ip $dst_ip"; fi
    if exec_on "$src" ping -6 -c1 -W2 -I "$src_ip" "$dst_ip" > /dev/null 2>&1; then
      echo "pass" > "$RESULT_DIR/${id}.rc"
    else
      echo "fail" > "$RESULT_DIR/${id}.rc"
    fi
    echo "$label" > "$RESULT_DIR/${id}.label"
  ) &
}

enqueue_bidir6() {
  local a="$1" a_ip="$2" b="$3" b_ip="$4" label="$5"
  enqueue_ping6 "$a" "$a_ip" "$b_ip" "$label ($a → $b)"
  enqueue_ping6 "$b" "$b_ip" "$a_ip" "$label ($b → $a)"
}

flush_section() {
  local section="$1" start_id="$2"
  wait
  echo ""
  echo "━━━ $section ━━━"
  local pass=0 fail=0
  for i in $(seq "$start_id" "$_seq"); do
    local rc label
    rc=$(cat "$RESULT_DIR/${i}.rc")
    label=$(cat "$RESULT_DIR/${i}.label")
    if [ "$rc" = "pass" ]; then
      echo "  ✅ $label"
      pass=$((pass + 1))
    else
      echo "  ❌ $label"
      fail=$((fail + 1))
    fi
  done
  TOTAL_PASS=$((TOTAL_PASS + pass))
  TOTAL_FAIL=$((TOTAL_FAIL + fail))
}

TOTAL_PASS=0
TOTAL_FAIL=0

"""


def _copy_node_isolation_script(output_dir: Path) -> None:
    """Copy node-isolation.py from bundled resources to the output directory."""
    src = Path(__file__).parent / "resources" / "node-isolation.py"
    dst = output_dir / "node-isolation.py"
    dst.write_text(src.read_text())
    logger.info("Copied node-isolation.py to %s", dst)


def _to_clab_intf(interface: str) -> str:
    """Convert SR Linux interface name to containerlab short form.

    ethernet-1-32 → e1-32
    ethernet-1-3/1 → e1-3-1  (breakout channel)
    """
    return interface.replace("ethernet-", "e").replace("/", "-")


def _extract_leaf_number(node_name: str) -> str | None:
    """Extract the numeric suffix from a leaf name (e.g., 'leaf4' → '4')."""
    m = re.search(r"(\d+)$", node_name)
    return m.group(1) if m else None


def _node_short_name(node_name: str) -> str:
    """Derive a short ``{prefix}{number}`` id from a node name.

    The prefix is the first letter of the leading alphabetic segment
    (``leaf1`` → ``l1``, ``spine1`` → ``s1``, ``tor2`` → ``t2``). This
    keeps client names unique in mixed-role designs. If the node name
    has no numeric suffix, the original name is returned.
    """
    m = re.match(r"^([A-Za-z]+)(\d+)$", node_name)
    if m:
        prefix, num = m.group(1), m.group(2)
        return f"{prefix[0].lower()}{num}"
    return node_name


def _allocate_client_mgmt_ips(
    intent: FabricIntent, count: int, mgmt_subnet: str
) -> list[str]:
    """Allocate static mgmt IPs for Linux clients that don't overlap with SR Linux nodes.

    Containerlab assigns dynamic IPs without considering static allocations,
    so every node must have a static IP to prevent collisions.  Client IPs
    are allocated from the top of the subnet (.254, .253, ...) downward,
    skipping the network/broadcast addresses and any IPs already used by
    SR Linux nodes.
    """
    net = ipaddress.ip_network(mgmt_subnet, strict=False)
    reserved = {
        ipaddress.ip_address(n.mgmt_ipv4)
        for n in intent.nodes
        if n.mgmt_ipv4
    }
    reserved.add(net.network_address)
    reserved.add(net.broadcast_address)
    # .1 is typically the Docker bridge gateway
    reserved.add(net.network_address + 1)

    allocated: list[str] = []
    candidate = int(net.broadcast_address) - 1  # start at .254
    while len(allocated) < count and candidate > int(net.network_address):
        ip = ipaddress.ip_address(candidate)
        if ip not in reserved:
            allocated.append(str(ip))
            reserved.add(ip)
        candidate -= 1

    if len(allocated) < count:
        raise ValueError(
            f"Cannot allocate {count} client mgmt IPs in {mgmt_subnet} "
            f"(only {len(allocated)} available)"
        )
    return allocated


def _derive_mgmt_subnet(intent: FabricIntent) -> str:
    """Derive the management subnet from intent or node mgmt IPs.

    If ``intent.mgmt_subnet`` is set, use it directly.  Otherwise,
    fall back to inferring a /24 from the first node's mgmt IPv4.
    """
    if intent.mgmt_subnet:
        return intent.mgmt_subnet
    for node in intent.nodes:
        if node.mgmt_ipv4:
            try:
                ip = ipaddress.ip_address(node.mgmt_ipv4)
                # Assume /24
                net = ipaddress.ip_network(f"{ip}/24", strict=False)
                return str(net)
            except ValueError:
                continue
    return "172.21.21.0/24"
