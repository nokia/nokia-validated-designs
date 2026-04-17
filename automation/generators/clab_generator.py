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
    router: str = ""
    loopback_ips: list[str] = field(default_factory=list)


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
        loopback_ips = _loopback_ips_for_routed_client(client_ip, intent)
        routed_att = RoutedAttachment(
            name=ri.name,
            client_ip=client_ip,
            client_mask=client_mask,
            gateway=gateway,
            router=ri.router,
            loopback_ips=loopback_ips,
        )
        clients.append(
            ClientNode(name=client_name, is_bonded=False, links=[link], routed=routed_att)
        )

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
        lines.append(f"ip addr add {ra.client_ip}/{ra.client_mask} dev eth1")
        lines.append("")
        if ra.loopback_ips:
            lines.append("# Loopback IPs for static route targets")
            for lip in ra.loopback_ips:
                net = ipaddress.ip_interface(lip)
                lines.append(f"ip addr add {lip}/{net.network.max_prefixlen} dev lo")
            lines.append("")
        lines.append("# Policy routing so source-bound traffic uses the fabric")
        lines.append(f"ip route add default via {ra.gateway} dev eth1 table 10")
        lines.append(f"ip rule add from {ra.client_ip} table 10")
        for lip in ra.loopback_ips:
            lines.append(f"ip rule add from {lip} table 10")
        lines.append("")
        lines.append("# Continuous traffic")
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
    # VLAN attachment across all clients.
    @dataclass
    class _ClientSubnet:
        client: str
        ip: str
        bd: str
        router: str

    subnets: list[_ClientSubnet] = []
    for cl in clients:
        if cl.routed:
            continue
        for link in cl.links:
            for att in link.attachments:
                if att.client_ip:
                    subnets.append(
                        _ClientSubnet(
                            client=cl.name,
                            ip=att.client_ip,
                            bd=att.bridge_domain,
                            router=bd_to_router.get(att.bridge_domain, ""),
                        )
                    )

    # Routed clients
    routed_clients = [cl for cl in clients if cl.routed]

    # ── script assembly ─────────────────────────────────────────────────
    L = []  # output lines

    L.append(_VALIDATE_HEADER)

    # --- Convergence wait using first client ---
    if subnets:
        s0 = subnets[0]
        gw0 = _gateway_for_subnet(s0.bd, intent)
        L.append("if ! $NO_WAIT; then")
        L.append(f'  wait_for_convergence "{s0.client}" "{s0.ip}" "{gw0}" 120')
        L.append("fi")
        L.append("")

    # --- Gateway reachability (one test per client-subnet) ---
    L.append('sec_start=$((_seq + 1))')
    for s in subnets:
        gw = _gateway_for_subnet(s.bd, intent)
        L.append(
            f'enqueue_ping "{s.client}" "{s.ip}" "{gw}" '
            f'"{s.client} → gw {gw} ({s.bd})"'
        )
    for cl in routed_clients:
        ra = cl.routed
        assert ra is not None
        L.append(
            f'enqueue_ping "{cl.name}" "{ra.client_ip}" "{ra.gateway}" '
            f'"{cl.name} → gw {ra.gateway} (routed)"'
        )
    L.append('flush_section "Gateway reachability" "$sec_start"')
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
    L.append('sec_start=$((_seq + 1))')
    bd_clients: dict[str, list[_ClientSubnet]] = {}
    for s in subnets:
        bd_clients.setdefault(s.bd, []).append(s)
    for bd, members in sorted(bd_clients.items()):
        if len(members) < 2:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                L.append(
                    f'enqueue_bidir "{a.client}" "{a.ip}" '
                    f'"{b.client}" "{b.ip}" "L2 {bd}"'
                )
    L.append('flush_section "L2 intra-subnet (same bridge domain)" "$sec_start"')
    L.append("")

    # --- L3 inter-subnet (clients in different BDs of the same router) ---
    L.append('sec_start=$((_seq + 1))')
    router_bds: dict[str, list[str]] = {}
    for s in subnets:
        if s.router:
            router_bds.setdefault(s.router, [])
            if s.bd not in router_bds[s.router]:
                router_bds[s.router].append(s.bd)

    for router, bds in sorted(router_bds.items()):
        if len(bds) < 2:
            continue
        for i, bd_a in enumerate(bds):
            for bd_b in bds[i + 1 :]:
                # pick one representative client from each BD
                a = bd_clients[bd_a][0]
                b = bd_clients[bd_b][0]
                L.append(
                    f'enqueue_bidir "{a.client}" "{a.ip}" '
                    f'"{b.client}" "{b.ip}" '
                    f'"L3 {router} ({a.bd} ↔ {b.bd})"'
                )
    L.append(
        'flush_section "L3 inter-subnet (same router, different subnet)" "$sec_start"'
    )
    L.append("")

    # --- Routed interfaces and static routes ---
    if routed_clients or intent.static_routes:
        L.append('sec_start=$((_seq + 1))')
        for cl in routed_clients:
            ra = cl.routed
            assert ra is not None
            tested: set[str] = set()
            for s in subnets:
                if ra.router and s.router != ra.router:
                    continue
                key = f"{cl.name}-{s.bd}"
                if key in tested:
                    continue
                tested.add(key)
                L.append(
                    f'enqueue_bidir "{cl.name}" "{ra.client_ip}" '
                    f'"{s.client}" "{s.ip}" '
                    f'"routed {ra.name} ↔ {s.bd}"'
                )

        for sr in intent.static_routes:
            nexthops = sr.nexthop_group.get("nexthops", [])
            nh_ip = nexthops[0].get("ipPrefix", "") if nexthops else ""
            # find a client on a BD that has a route via the static route's router
            src = next(
                (s for s in subnets if s.router == sr.router), None
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
                label = f"static route {prefix} via {target_name or nh_ip} (from {src.client})"
                L.append(
                    f'enqueue_ping "{src.client}" "{src.ip}" '
                    f'"{first_host}" "{label}"'
                )
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
    """Return the anycast-gw IP for a bridge domain."""
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
