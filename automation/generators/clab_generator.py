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
    EdgeInterfaceIntent,
    FabricIntent,
    IrbInterfaceIntent,
    LagIntent,
    RoutedInterfaceIntent,
    VlanIntent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform → containerlab type mapping
# ---------------------------------------------------------------------------

PLATFORM_TYPE_MAP = {
    "7220 IXR-D3L": "ixrd3l",
    "7220 IXR-D2L": "ixrd2l",
    "7220 IXR-D3": "ixrd3",
    "7220 IXR-D2": "ixrd2",
    "7220 IXR-D5": "ixrd5",
    "7220 IXR-H2": "ixrh2",
    "7220 IXR-H3": "ixrh3",
    "7250 IXR-6e": "ixr6e",
    "7250 IXR-10e": "ixr10e",
    "7250 IXR-6": "ixr6",
    "7250 IXR-10": "ixr10",
}

CLIENT_IMAGE = "ghcr.io/srl-labs/network-multitool"
SRL_IMAGE_BASE = "ghcr.io/nokia/srlinux"


# ---------------------------------------------------------------------------
# Data structures for client derivation
# ---------------------------------------------------------------------------


@dataclass
class VlanAttachment:
    """A single VLAN attachment on a client interface."""

    vlan_name: str
    bridge_domain: str
    vlan_id: str  # "10", "20", "untagged"
    irb_subnet: str  # e.g., "172.16.10.0/24"
    irb_gateway: str  # e.g., "172.16.10.254"
    client_ip: str = ""  # assigned later
    client_mask: int = 24  # prefix length


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
    """A routed (L3) attachment — no bridge domain, direct IP."""

    name: str
    client_ip: str
    client_mask: int
    gateway: str


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

    clab_topo = _build_clab_topology(intent, clients)

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

    # Collect routed interface ports → separate dedicated clients
    routed_ports: dict[str, RoutedInterfaceIntent] = {}
    for ri in intent.routed_interfaces:
        # ri.interface is the EDA resource name, e.g., "leaf1-ethernet-1-4"
        # Find the edge interface to get the (node, physical_interface)
        for ei in intent.edge_interfaces:
            if ei.name == ri.interface:
                routed_ports[(ei.node, ei.interface)] = ri
                break

    # --- Single-homed clients ---
    # Group edge interfaces by node (excluding LAG members and routed ports)
    leaf_edges: dict[str, list[EdgeInterfaceIntent]] = {}
    for ei in intent.edge_interfaces:
        key = (ei.node, ei.interface)
        if key in lag_member_ports or key in routed_ports:
            continue
        leaf_edges.setdefault(ei.node, []).append(ei)

    for node_name, edges in sorted(leaf_edges.items()):
        # Extract leaf number for naming
        leaf_num = _extract_leaf_number(node_name)
        client_name = f"cl-l{leaf_num}" if leaf_num else f"cl-{node_name}"

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
    for lag in intent.lags:
        members = sorted(lag.members, key=lambda m: m.node)
        leaf_nums = []
        for m in members:
            num = _extract_leaf_number(m.node)
            if num and num not in leaf_nums:
                leaf_nums.append(num)
        client_name = "cl-l" + "l".join(str(n) for n in leaf_nums)

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

        link = ClientLink(
            leaf_node=node_name,
            leaf_interface=phys_intf,
            eth_index=1,
            labels={},
        )
        routed_att = RoutedAttachment(
            name=ri.name,
            client_ip=client_ip,
            client_mask=client_mask,
            gateway=gateway,
        )
        clients.append(
            ClientNode(name=client_name, is_bonded=False, links=[link], routed=routed_att)
        )

    return clients


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
                if _labels_match(vlan.interface_selector, link.labels):
                    irb = irb_by_bd.get(vlan.bridge_domain)
                    subnet = ""
                    gateway = ""
                    if irb:
                        if irb.ipv4:
                            iface = ipaddress.ip_interface(irb.ipv4)
                            subnet = str(iface.network)
                            gateway = str(iface.ip)
                        elif irb.ip_addresses:
                            for addr in irb.ip_addresses:
                                if addr.ipv4:
                                    pfx = addr.ipv4.get("ip_prefix", "")
                                    if pfx:
                                        iface = ipaddress.ip_interface(pfx)
                                        subnet = str(iface.network)
                                        gateway = str(iface.ip)
                                        break

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
                        )
                    )


def _labels_match(selectors: list[str], labels: dict[str, str]) -> bool:
    """Check if any selector matches the labels (OR logic)."""
    for sel in selectors:
        if "=" in sel:
            key, value = sel.split("=", 1)
            if labels.get(key) == value:
                return True
    return False


# ---------------------------------------------------------------------------
# IP allocation
# ---------------------------------------------------------------------------


def _allocate_ips(clients: list[ClientNode], intent: FabricIntent) -> None:
    """Allocate unique IPs per subnet across all clients."""
    subnet_counters: dict[str, int] = {}  # subnet → next offset

    for client in clients:
        for link in client.links:
            for att in link.attachments:
                if not att.irb_subnet:
                    continue
                net = ipaddress.ip_network(att.irb_subnet)
                offset = subnet_counters.get(att.irb_subnet, 1)
                att.client_ip = str(net.network_address + offset)
                att.client_mask = net.prefixlen
                subnet_counters[att.irb_subnet] = offset + 1


# ---------------------------------------------------------------------------
# Containerlab topology builder
# ---------------------------------------------------------------------------


def _build_clab_topology(
    intent: FabricIntent, clients: list[ClientNode]
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
        platform_type = PLATFORM_TYPE_MAP.get(node.platform, "ixrd3l")
        node_def: dict[str, Any] = {"mgmt-ipv4": node.mgmt_ipv4}
        # Only specify type if it differs from the default
        node_def["type"] = platform_type
        nodes[node.name] = node_def

    # Linux client nodes
    for client in sorted(clients, key=lambda c: c.name):
        nodes[client.name] = {
            "kind": "linux",
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
                "nokia_srlinux": {"image": srl_image},
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
        # Simple routed client — direct L3 on eth1
        ra = client.routed
        lines.append("# Routed interface (direct L3)")
        lines.append(f"ip addr add {ra.client_ip}/{ra.client_mask} dev eth1")
        lines.append(f"ip route add default via {ra.gateway} dev eth1")
        lines.append("")
        lines.append("# Traffic")
        lines.append(
            f"nohup ping -i 0.5 -I eth1 {ra.gateway} > /dev/null 2>&1 &"
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

        if tagged:
            lines.append(f"# Interface setup: {iface}")
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

        # Assign IPs
        for att in untagged:
            if att.client_ip:
                lines.append(
                    f"ip addr add {att.client_ip}/{att.client_mask} dev {iface}"
                )
        for att in tagged:
            if att.client_ip:
                sub = f"{iface}.{att.vlan_id}"
                lines.append(
                    f"ip addr add {att.client_ip}/{att.client_mask} dev {sub}"
                )

    # Default routes per subnet (using routing tables for isolation)
    lines.append("")
    lines.append("# Per-subnet routing tables for isolation")
    table_id = 10
    all_attachments = []
    for link in client.links:
        for att in link.attachments:
            if att.client_ip and att.irb_gateway:
                iface = f"eth{link.eth_index}"
                if att.vlan_id not in ("untagged", "null"):
                    iface = f"{iface}.{att.vlan_id}"
                lines.append(
                    f"ip route add default via {att.irb_gateway} dev {iface} table {table_id}"
                )
                lines.append(
                    f"ip rule add from {att.client_ip} table {table_id}"
                )
                all_attachments.append((iface, att))
                table_id += 10

    # Traffic generation
    lines.append("")
    lines.append("# Continuous traffic")
    for iface, att in all_attachments:
        lines.append(
            f"nohup ping -i 0.5 -I {iface} {att.irb_gateway} > /dev/null 2>&1 &"
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
    for link in client.links:
        iface = f"eth{link.eth_index}"
        lines.append(f"ip link set {iface} master bond0")
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

    # Assign IPs
    lines.append("# IP addressing")
    for att in untagged:
        if att.client_ip:
            lines.append(f"ip addr add {att.client_ip}/{att.client_mask} dev bond0")
    for att in tagged:
        if att.client_ip:
            sub = f"bond0.{att.vlan_id}"
            lines.append(f"ip addr add {att.client_ip}/{att.client_mask} dev {sub}")

    # Routes per subnet
    lines.append("")
    lines.append("# Per-subnet routing tables")
    table_id = 10
    all_attachments_iface = []
    for att in untagged + tagged:
        if att.client_ip and att.irb_gateway:
            iface = "bond0"
            if att.vlan_id not in ("untagged", "null"):
                iface = f"bond0.{att.vlan_id}"
            lines.append(
                f"ip route add default via {att.irb_gateway} dev {iface} table {table_id}"
            )
            lines.append(f"ip rule add from {att.client_ip} table {table_id}")
            all_attachments_iface.append((iface, att))
            table_id += 10

    # Traffic
    lines.append("")
    lines.append("# Continuous traffic")
    for iface, att in all_attachments_iface:
        lines.append(
            f"nohup ping -i 0.5 -I {iface} {att.irb_gateway} > /dev/null 2>&1 &"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _derive_mgmt_subnet(intent: FabricIntent) -> str:
    """Derive the management subnet from node mgmt IPs."""
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
