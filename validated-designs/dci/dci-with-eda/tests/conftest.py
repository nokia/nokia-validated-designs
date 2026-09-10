# © 2025 Nokia
# Licensed under the BSD 3-Clause License
# SPDX-License-Identifier: BSD-3-Clause
"""
Shared fixtures / helpers for the DCI validated-design test suite.

The tests drive the *running* containerlab topology over `docker exec`, so the
lab must already be deployed (`containerlab deploy -t dci-with-eda.clab.yaml`)
on the same host where pytest runs.

Nothing here is SR-Linux-release specific beyond the `sr_cli` CLI and the
client bootstrap (netns + 802.1q + LACP bonds) created by base-configs/*.sh.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Set DCI_TRACE=1 to echo every shell command the suite runs (and any piped
# sr_cli config payload). Combine with `pytest -s` so the trace reaches the
# terminal, e.g.  DCI_TRACE=1 pytest -v -s -m convergence
_TRACE = os.environ.get("DCI_TRACE")

import pytest

# containerlab topology that defines this lab (resolved so fcli works from any cwd)
CLAB_TOPO = Path(__file__).resolve().parent.parent / "dci-with-eda.clab.yaml"

# EDA moves the mgmt gRPC server off the containerlab default of 57400 as part of
# onboarding; it must match the port in the NodeProfile the TopoNodes reference.
GNMI_PORT = 57410

# --------------------------------------------------------------------------- #
# Topology facts (kept in one place so tests stay declarative)
# --------------------------------------------------------------------------- #

DC1_DCGWS = ["dcgw1", "dcgw2"]
DC2_DCGWS = ["dcgw3", "dcgw4"]
ALL_DCGWS = DC1_DCGWS + DC2_DCGWS

# the L3-DCI tenant IP-VRF (one shared name on every leaf + DCGW)
L3DCI_NI = "ipvrf-l3dci"
# L3 DCI routed tenant subnets (cross-DC); see README addressing table
L3DCI_SUBNET_DC1 = "10.200.1.0/24"
L3DCI_SUBNET_DC2 = "10.200.2.0/24"
# VPNv4 (l3vpn-ipv4-unicast) BGP RIB on DCGWs lives under ``default`` (WAN iBGP);
# the eBGP fabric group keeps l3vpn-ipv4-unicast admin-state disable.
WAN_VPNV4_NI = "default"

# leaf VTEPs per DC + the underlay subnet each DC's VTEPs live in. Used to assert
# that remote-DC destinations are only ever reached via the LOCAL DCGW pair
# (i.e. no raw VXLAN route leaks end-to-end across the DCI).
DC1_LEAVES = ["leaf1", "leaf2", "leaf3", "leaf4"]
DC2_LEAVES = ["leaf5", "leaf6", "leaf7", "leaf8"]
DC_LEAVES = {1: DC1_LEAVES, 2: DC2_LEAVES}
DC_DCGWS = {1: DC1_DCGWS, 2: DC2_DCGWS}
DC_VTEP_SUBNET = {1: "192.0.2.", 2: "192.0.3."}

# direct DCGW<->DCGW mesh ports (IS-IS metric 10) per node
DIRECT_MESH_PORTS = {
    "dcgw1": ["ethernet-1/3", "ethernet-1/4", "ethernet-1/5"],
    "dcgw2": ["ethernet-1/3", "ethernet-1/4", "ethernet-1/5"],
    "dcgw3": ["ethernet-1/3", "ethernet-1/4", "ethernet-1/5"],
    "dcgw4": ["ethernet-1/3", "ethernet-1/4", "ethernet-1/5"],
}
# every fabric/DCI-facing port on a DCGW (e1-1..e1-7) - used to isolate a GW
DCGW_ALL_PORTS = [f"ethernet-1/{i}" for i in range(1, 8)]

# WAN / DCI MPLS transports that run in parallel over the same IS-IS core. Both an
# LDP and an SR-ISIS tunnel exist to every remote loopback; LDP is preferred
# (tunnel-table preference 9 < SR-ISIS 11), so disabling one shifts to the other.
WAN_TRANSPORTS = ("ldp", "sr-isis")
# name of the IS-IS instance carrying the WAN core, i.e. the DefaultISISInstance
# CR name. The fabric underlay runs in a separate instance called "default".
WAN_ISIS = "wan-isis"
P_ROUTERS = ["p1", "p2"]
WAN_NODES = ALL_DCGWS + P_ROUTERS          # DCGWs terminate services; p1/p2 are LSRs
# system0 loopbacks and IS-IS SR node-SID indexes are NOT hardcoded here the way
# they are in the hand-built lab: EDA allocates both out of pools, so the values
# depend on the order the fabric happened to converge in and change between
# deployments. They are read back off the nodes instead - see node_sys0() and
# node_sid() below.
# the two "diagonals" of the 2x2 gateway grid (DC1={dcgw1,dcgw2}, DC2={dcgw3,dcgw4}):
# each pair fails exactly ONE gateway per DC, and (given BD-A->dcgw1/3, BD-B->dcgw2/4)
# forces a primary-service failover in BOTH DCs at once.
DIAGONAL_DCGW_PAIRS = [("dcgw1", "dcgw4"), ("dcgw2", "dcgw3")]


def remote_dcgw_loopbacks(node: str) -> set[str]:
    """system0 loopbacks of the DCGWs in the OTHER DC (the DCI tunnel endpoints)."""
    peers = DC2_DCGWS if node in DC1_DCGWS else DC1_DCGWS
    return {node_sys0(p) for p in peers}


@dataclass(frozen=True)
class Endpoint:
    """A logical client (netns) inside a network-multitool container."""

    container: str
    netns: str
    ip: str
    dc: int
    service: str  # "l2" | "l3"
    homing: str  # "mh" | "sh"


# logical clients created by base-configs/*.sh
ENDPOINTS = {
    # DC1 - ES mh-dc1 (leaf1/2)
    "mh1-l2a": Endpoint("mh-dc1", "mh1-l2a", "10.100.0.11", 1, "l2", "mh"),
    "mh1-l2b": Endpoint("mh-dc1", "mh1-l2b", "10.100.0.12", 1, "l2", "mh"),
    "mh1-l3a": Endpoint("mh-dc1", "mh1-l3a", "10.200.1.11", 1, "l3", "mh"),
    "mh1-l3b": Endpoint("mh-dc1", "mh1-l3b", "10.200.1.12", 1, "l3", "mh"),
    "sh1-l2": Endpoint("sh-dc1", "sh1-l2", "10.100.0.13", 1, "l2", "sh"),
    "sh1-l3": Endpoint("sh-dc1", "sh1-l3", "10.200.1.13", 1, "l3", "sh"),
    # DC1 - L2 DCI BD-B (2nd stretched BD, vlan 110, pinned to dcgw2)
    "mh1-l2c": Endpoint("mh-dc1", "mh1-l2c", "10.110.0.11", 1, "l2b", "mh"),
    "mh1-l2d": Endpoint("mh-dc1", "mh1-l2d", "10.110.0.12", 1, "l2b", "mh"),
    "sh1-l2b": Endpoint("sh-dc1", "sh1-l2b", "10.110.0.13", 1, "l2b", "sh"),
    # DC1 - ES mh-dc1b (leaf3/4)
    "mh1b-l2a": Endpoint("mh-dc1b", "mh1b-l2a", "10.100.0.14", 1, "l2", "mh"),
    "mh1b-l2b": Endpoint("mh-dc1b", "mh1b-l2b", "10.100.0.15", 1, "l2", "mh"),
    "mh1b-l3a": Endpoint("mh-dc1b", "mh1b-l3a", "10.200.1.14", 1, "l3", "mh"),
    "mh1b-l3b": Endpoint("mh-dc1b", "mh1b-l3b", "10.200.1.15", 1, "l3", "mh"),
    # DC2 - ES mh-dc2 (leaf5/6)
    "mh2-l2a": Endpoint("mh-dc2", "mh2-l2a", "10.100.0.21", 2, "l2", "mh"),
    "mh2-l2b": Endpoint("mh-dc2", "mh2-l2b", "10.100.0.22", 2, "l2", "mh"),
    "mh2-l3a": Endpoint("mh-dc2", "mh2-l3a", "10.200.2.21", 2, "l3", "mh"),
    "mh2-l3b": Endpoint("mh-dc2", "mh2-l3b", "10.200.2.22", 2, "l3", "mh"),
    "sh2-l2": Endpoint("sh-dc2", "sh2-l2", "10.100.0.23", 2, "l2", "sh"),
    "sh2-l3": Endpoint("sh-dc2", "sh2-l3", "10.200.2.23", 2, "l3", "sh"),
    # DC2 - L2 DCI BD-B (2nd stretched BD, vlan 110, pinned to dcgw4)
    "mh2-l2c": Endpoint("mh-dc2", "mh2-l2c", "10.110.0.21", 2, "l2b", "mh"),
    "mh2-l2d": Endpoint("mh-dc2", "mh2-l2d", "10.110.0.22", 2, "l2b", "mh"),
    "sh2-l2b": Endpoint("sh-dc2", "sh2-l2b", "10.110.0.23", 2, "l2b", "sh"),
    # DC2 - ES mh-dc2b (leaf7/8)
    "mh2b-l2a": Endpoint("mh-dc2b", "mh2b-l2a", "10.100.0.24", 2, "l2", "mh"),
    "mh2b-l2b": Endpoint("mh-dc2b", "mh2b-l2b", "10.100.0.25", 2, "l2", "mh"),
    "mh2b-l3a": Endpoint("mh-dc2b", "mh2b-l3a", "10.200.2.24", 2, "l3", "mh"),
    "mh2b-l3b": Endpoint("mh-dc2b", "mh2b-l3b", "10.200.2.25", 2, "l3", "mh"),
}

# leaves each access segment (Endpoint.container) attaches to. The host /32 is
# LOCAL on these leaves (connected anycast /24 + ARP), so they do NOT install a
# bgp-evpn-ifl host route; every OTHER node in the DC does. For a multi-homed ES
# the /32 aliases across BOTH listed VTEPs (advertise-ifl-host-ad-routes).
ES_LEAVES = {
    "mh-dc1": ["leaf1", "leaf2"],
    "mh-dc1b": ["leaf3", "leaf4"],
    "sh-dc1": ["leaf3"],
    "mh-dc2": ["leaf5", "leaf6"],
    "mh-dc2b": ["leaf7", "leaf8"],
    "sh-dc2": ["leaf7"],
}

# DCI-facing ports on a DCGW (direct mesh e1-3..5 + WAN uplinks e1-6/7); summed to
# observe how much DCI traffic each gateway forwards.
DCGW_DCI_PORTS = [f"ethernet-1/{i}" for i in range(3, 8)]

# Canary flows used by teardown to wait for the fabric to become healthy again.
# One per stretched bridge domain, because the two are deliberately pinned to
# different gateways of a pair: a BD-A canary alone goes green while BD-B is
# still moving, and the next test then starts against a half-converged fabric.
CANARIES = [
    (ENDPOINTS["mh1-l2a"], ENDPOINTS["mh2-l2a"]),
    (ENDPOINTS["mh1-l2b"], ENDPOINTS["mh2-l2b"]),
]


# --------------------------------------------------------------------------- #
# low-level docker / sr_cli helpers
# --------------------------------------------------------------------------- #

_name_cache: list[str] | None = None


def _docker_names() -> list[str]:
    global _name_cache
    if _name_cache is None:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        _name_cache = out.split()
    return _name_cache


def cname(node: str) -> str:
    """Resolve a topology node to its actual container name (prefix-agnostic)."""
    for n in _docker_names():
        if n == node or n.endswith(f"-{node}"):
            return n
    return node


def _run(cmd: list[str], input_: str | None = None, timeout: int = 90):
    if _TRACE:
        print(f"\n  $ {' '.join(cmd)}", flush=True)
        if input_:
            for ln in input_.splitlines():
                print(f"      | {ln}", flush=True)
    return subprocess.run(
        cmd, input=input_, capture_output=True, text=True, timeout=timeout
    )


def srl(node: str, command: str) -> str:
    """Run a single read-only sr_cli command, return combined output."""
    r = _run(["docker", "exec", cname(node), "sr_cli", command])
    return (r.stdout or "") + (r.stderr or "")


@functools.lru_cache(maxsize=None)
def node_sys0(node: str) -> str:
    """system0 IPv4 address of `node` (no prefix length).

    This is the node's BGP and MPLS transport endpoint. EDA hands it out of the
    fabric's systemPoolIPv4, so it has to be read back rather than derived from
    the node name."""
    out = srl(node, "info flat interface system0")
    m = re.search(r"ipv4 address (\d+\.\d+\.\d+\.\d+)/", out)
    assert m, f"no system0 IPv4 address on {node}:\n{out}"
    return m.group(1)


@functools.lru_cache(maxsize=None)
def node_sid(node: str) -> int:
    """IS-IS SR node-SID index of `node`, from the node-sid-index pool."""
    out = srl(node, "info flat network-instance default protocols isis")
    m = re.search(r"ipv4-node-sid index (\d+)", out)
    assert m, f"no IS-IS SR node-SID on {node}:\n{out}"
    return int(m.group(1))


def local_dcgw_vteps(dc: int) -> set[str]:
    """system0 addresses of the DCGW pair that fronts `dc`."""
    return {node_sys0(gw) for gw in DC_DCGWS[dc]}


def srl_configure(node: str, lines: list[str]) -> str:
    """Apply config `lines` on `node` (enter candidate / commit now)."""
    payload = "enter candidate\ndiscard stay\n" + "\n".join(lines) + "\ncommit now\n"
    r = _run(["docker", "exec", "-i", cname(node), "sr_cli"], input_=payload)
    out = (r.stdout or "") + (r.stderr or "")
    assert "committed" in out.lower() or "leaving candidate" in out.lower(), (
        f"config commit failed on {node}:\n{out}"
    )
    return out


def set_ports(node: str, ports: list[str], state: str):
    """admin enable/disable a set of interfaces on a node."""
    assert state in ("enable", "disable")
    lines = [f"set / interface {p} admin-state {state}" for p in ports]
    srl_configure(node, lines)


# --------------------------------------------------------------------------- #
# WAN MPLS transport (LDP / SR-ISIS) helpers
# --------------------------------------------------------------------------- #

def _tunnels_from_rows(rows: list[dict], node: str) -> dict[str, dict[str, list[str]]]:
    """Parse tunnel-table JSON rows for a single DCGW `node` (see `dci_tunnels`)."""
    res: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        if not _fcli_node_matches(r.get("Node"), node):
            continue
        prefix, ttype = r.get("Prefix"), r.get("type")
        if not prefix or not str(prefix).endswith("/32") or ttype not in WAN_TRANSPORTS:
            continue
        ports = [str(p) for p in (r.get("egress-itf") or []) if p]
        res.setdefault(prefix, {})[ttype] = ports
    return res


def dci_tunnels(node: str) -> dict[str, dict[str, list[str]]]:
    """IPv4 tunnel-table on `node`, via `fcli tunnel-table` (one gNMI query).

    Returns {loopback-prefix: {transport: [egress-port, ...]}} for the
    ldp/sr-isis tunnels. fcli resolves each tunnel's next-hop-group to the
    egress sub-interface(s), so an ECMP tunnel lists every egress port.
    """
    return _tunnels_from_rows(
        fcli_json(["tunnel-table"], inv=f"hostname={node}"), node
    )


# SR-ISIS underlay config lines (per node; the node-SID index is substituted).
def _sr_isis_enable_lines(node: str) -> list[str]:
    idx = node_sid(node)
    # These three lines are exactly what EDA renders for SR-ISIS - no more. The
    # instance-level container is empty: EDA does not bind an adjacency-SID
    # label block, so restoring one here would leave drift EDA cannot reconcile.
    return [
        "set / network-instance default segment-routing mpls global-block label-range sr-global",
        f"set / network-instance default protocols isis instance {WAN_ISIS} segment-routing mpls",
        f"set / network-instance default protocols isis instance {WAN_ISIS} interface system0.0 "
        f"segment-routing mpls ipv4-node-sid index {idx}",
    ]


# all three SR lines must be removed in ONE commit: the per-node ipv4-node-sid
# depends on the SRGB global-block, so they cannot be deleted piecemeal.
_SR_ISIS_DISABLE_LINES = [
    f"delete / network-instance default protocols isis instance {WAN_ISIS} interface system0.0 segment-routing",
    f"delete / network-instance default protocols isis instance {WAN_ISIS} segment-routing",
    "delete / network-instance default segment-routing",
]


def disable_wan_transport(transport: str):
    """Disable one MPLS transport (`ldp` | `sr-isis`) on every WAN node."""
    assert transport in WAN_TRANSPORTS
    for node in WAN_NODES:
        if transport == "ldp":
            srl_configure(node, ["set / network-instance default protocols ldp admin-state disable"])
        else:
            # Read the node-SID into the lru_cache BEFORE deleting it. It is
            # pool-allocated, so restoring SR means reading it back off the node
            # - and by then the delete has removed the only copy. Priming here
            # is what makes the teardown able to put SR back.
            node_sid(node)
            srl_configure(node, _SR_ISIS_DISABLE_LINES)


def enable_wan_transport(transport: str):
    """Restore one MPLS transport (`ldp` | `sr-isis`) on every WAN node."""
    assert transport in WAN_TRANSPORTS
    for node in WAN_NODES:
        if transport == "ldp":
            srl_configure(node, ["set / network-instance default protocols ldp admin-state enable"])
        else:
            srl_configure(node, _sr_isis_enable_lines(node))


# --------------------------------------------------------------------------- #
# fcli (fabric CLI) helpers - one gNMI query returns structured state for the
# WHOLE fabric, which is far cleaner than docker-exec + regex for control-plane
# / RIB assertions. Data-plane checks (ping/iperf in client netns, per-port
# counters) still use docker exec because fcli has no notion of them.
# --------------------------------------------------------------------------- #

# fcli (the SR Linux fabric CLI from github.com/srl-labs/nornir-srl) is a hard
# prerequisite for this suite - it is the reporting tool used for every fabric-
# wide control-plane / RIB / tunnel / counter assertion. It is NOT a pip
# dependency; install it as a standalone tool with `uv`.
#
# Minimum version: 0.4.3 is the first release that ships the `tunnel-table`
# report (and the cumulative `ifstats` counters / cleaned JSON keys) this suite
# relies on. Install from upstream (the fork's default branch may lag behind).
FCLI_MIN_VERSION = "0.4.3"
FCLI_INSTALL_HINT = (
    f"fcli (nornir-srl) >= {FCLI_MIN_VERSION} is REQUIRED to run this suite "
    "(it provides the tunnel-table report).\n"
    "Install/upgrade it with uv:\n"
    "    uv tool install --force git+https://github.com/srl-labs/nornir-srl\n"
    "then make sure the uv tools bin dir (typically ~/.local/bin) is on your PATH.\n"
    "(If you don't have uv: https://docs.astral.sh/uv/getting-started/installation/)"
)


def fcli_available() -> bool:
    return shutil.which("fcli") is not None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Numeric components of a dotted version string ('0.4.3' -> (0, 4, 3)).

    Stops at the first non-numeric component so pre-release suffixes (e.g.
    '0.4.3rc1') compare by their leading release number.
    """
    parts: list[int] = []
    for comp in str(v).strip().split("."):
        m = re.match(r"\d+", comp)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


def fcli_version() -> str | None:
    """The installed fcli version (e.g. '0.4.3'), or None if it can't be read."""
    if not fcli_available():
        return None
    r = _run(["fcli", "--version"], timeout=30)
    m = re.search(r"\d+\.\d+(?:\.\d+)?", (r.stdout or "") + (r.stderr or ""))
    return m.group() if m else None


def fcli_json(report_args: list[str], timeout: int = 90,
              inv: str | None = None) -> list[dict]:
    """Run an fcli report fabric-wide and return parsed JSON rows.

    `inv` is an optional inventory filter (e.g. ``hostname=dcgw1`` or
    ``role=dcgw``) passed as the global ``-i`` option so the query (and any
    sampling, as in `ifstats`) is scoped to just those nodes.

    fcli is a prerequisite (see `FCLI_INSTALL_HINT`): a missing tool or a failed
    query is a hard error rather than a skip, so fabric problems surface."""
    if not fcli_available():
        pytest.fail(FCLI_INSTALL_HINT, pytrace=False)
    cmd = ["fcli", "-t", str(CLAB_TOPO), "-o", "json", "-p", str(GNMI_PORT)]
    if inv:
        cmd += ["-i", inv]
    cmd += list(report_args)
    r = _run(cmd, timeout=timeout)
    if r.returncode != 0:
        pytest.fail(f"fcli '{' '.join(report_args)}' failed (rc={r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[:300]}", pytrace=False)
    out = (r.stdout or "").strip()
    if not out or out == "No data...":
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pytest.fail(f"fcli '{' '.join(report_args)}' returned non-JSON output: "
                    f"{out[:300]}", pytrace=False)


def _fcli_node_matches(value: str | None, node: str) -> bool:
    """True when an fcli `Node` column value refers to topology node `node`
    (prefix-agnostic, mirroring `cname`)."""
    return bool(value) and (value == node or value.endswith(f"-{node}"))


def l3dci_ipv4_rib() -> list[dict]:
    """Fabric-wide IPv4 RIB rows for the L3-DCI IP-VRF (`fcli ipv4-rib`).

    Each row: {Node, NI, Prefix, next-hop[], type, Act(yes|no), metric, pref, itf[]}.
    Includes non-best paths (Act=no), so a host-route that leaked across the DCI
    cannot hide behind a better path.
    """
    return [r for r in fcli_json(["ipv4-rib"]) if r.get("NI") == L3DCI_NI]


@functools.lru_cache(maxsize=1)
def fcli_bgp_rib_l3vpn_v4_available() -> bool:
    """True when ``fcli bgp-rib -r l3vpn-v4`` works (nornir-srl with L3VPN RIB support).

    Older fcli builds lack this report mode; callers should skip rather than fail
    the whole suite so ``tunnel-table``-only installs still run the rest.
    """
    if not fcli_available():
        return False
    cmd = ["fcli", "-t", str(CLAB_TOPO), "-o", "json", "bgp-rib", "-r", "l3vpn-v4"]
    r = _run(cmd, timeout=90)
    if r.returncode != 0:
        return False
    comb = ((r.stderr or "") + (r.stdout or "")).lower()
    if "bad jmespath" in comb or ("invalid" in comb and "jmespath" in comb):
        return False
    return True


def l3vpn_ipv4_rib_wan() -> list[dict]:
    """Fabric-wide L3VPN IPv4 BGP RIB in the WAN instance (``default`` NI).

    Rows come from ``fcli bgp-rib -r l3vpn-v4``; each includes ``Pfx`` (prefix),
    ``RD``, ``st`` (used/valid/best markers), ``next-hop``, etc. Leaves without
    an l3vpn-ipv4-unicast path are omitted by fcli (empty per host).
    """
    return [r for r in fcli_json(["bgp-rib", "-r", "l3vpn-v4"]) if r.get("NI") == WAN_VPNV4_NI]


def remote_l3dci_subnet(dc: int) -> str:
    """The other DC's L3 DCI tenant /24 as advertised over the WAN."""
    return L3DCI_SUBNET_DC2 if dc == 1 else L3DCI_SUBNET_DC1


def remote_dcgw_loopback_ips(dc: int) -> set[str]:
    """system0 loopbacks of the DCGW pair in the *other* DC (WAN next-hops)."""
    peers = DC2_DCGWS if dc == 1 else DC1_DCGWS
    return {node_sys0(p) for p in peers}


# --------------------------------------------------------------------------- #
# ping helpers + convergence measurement
# --------------------------------------------------------------------------- #

@dataclass
class PingResult:
    transmitted: int
    received: int
    loss_pct: float
    outage_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.received > 0 and self.loss_pct < 100.0


def _parse_stats(text: str) -> PingResult:
    m = re.search(
        r"(\d+) packets transmitted, (\d+)[^\n]*received.*?"
        r"(\d+(?:\.\d+)?)% packet loss",
        text, re.S,
    )
    if not m:
        return PingResult(0, 0, 100.0)
    return PingResult(int(m.group(1)), int(m.group(2)), float(m.group(3)))


def ping(ep_src: Endpoint, dst_ip: str, count: int = 4,
         interval: float = 0.3, wait: int = 2) -> PingResult:
    """Ping `dst_ip` from a client netns; returns parsed stats."""
    cmd = [
        "docker", "exec", cname(ep_src.container),
        "ip", "netns", "exec", ep_src.netns,
        "ping", "-c", str(count), "-i", str(interval), "-W", str(wait), dst_ip,
    ]
    timeout = int(count * interval + wait + 20)
    r = _run(cmd, timeout=timeout)
    return _parse_stats(r.stdout + r.stderr)


def measure_convergence(ep_src: Endpoint, dst_ip: str, action,
                        interval: float = 0.2, pre: float = 3.0,
                        post: float = 15.0, wait: int = 1) -> PingResult:
    """
    Run a steady ping while `action()` injects a fault mid-stream and estimate
    the data-plane outage as the largest gap between successful replies.

    `action` is invoked once, `pre` seconds into the run, and must NOT restore
    the fault (a fixture is responsible for restoration).
    """
    duration = pre + post
    count = int(duration / interval) + 5
    cmd = [
        "docker", "exec", cname(ep_src.container),
        "ip", "netns", "exec", ep_src.netns,
        "ping", "-D", "-i", str(interval), "-W", str(wait), "-c", str(count), dst_ip,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(pre)
        action()
        out, _ = proc.communicate(timeout=duration + 60)
    finally:
        if proc.poll() is None:
            proc.kill()
            out, _ = proc.communicate()

    res = _parse_stats(out)
    ts = [float(m) for m in
          re.findall(r"^\[(\d+\.\d+)\][^\n]*bytes from", out, re.M)]
    if len(ts) >= 2:
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        res.outage_s = max(0.0, max(gaps) - interval)
    elif res.received == 0:
        res.outage_s = None  # never recovered
    else:
        res.outage_s = 0.0
    return res


# --------------------------------------------------------------------------- #
# iperf3 multi-flow helpers (ECMP / redundancy under real traffic)
# --------------------------------------------------------------------------- #
#
# NOTE on bandwidth: these containers forward in software, so keep the per-flow
# rate modest (a few Mbit/s). Pushing high rates causes host-side drops that are
# unrelated to the fabric and would mask the convergence behaviour we measure.

@dataclass
class FlowResult:
    name: str
    sent: int
    lost: int
    loss_pct: float


def _iperf_udp_cmd(ep_src: Endpoint, dst_ip: str, bitrate: str,
                   duration: float, streams: int, length: int):
    return [
        "docker", "exec", cname(ep_src.container),
        "ip", "netns", "exec", ep_src.netns,
        "iperf3", "-c", dst_ip, "-u", "-b", bitrate, "-P", str(streams),
        "-l", str(length), "-t", str(duration), "--json",
    ]


def _parse_iperf_json(name: str, raw: str) -> FlowResult:
    import json
    try:
        data = json.loads(raw)
        s = data["end"]["sum"]
        return FlowResult(name, int(s.get("packets", 0)),
                          int(s.get("lost_packets", 0)),
                          float(s.get("lost_percent", 100.0)))
    except Exception:
        return FlowResult(name, 0, 0, 100.0)


def run_flows(flows: list[tuple[str, str]], bitrate: str = "3M",
              duration: float = 20.0, streams: int = 4, length: int = 1200,
              action=None, action_delay: float = 6.0) -> list[FlowResult]:
    """
    Launch each (src_endpoint_key, dst_endpoint_key) flow concurrently as a UDP
    iperf3 test (each flow uses `streams` parallel sub-streams for ECMP entropy).
    Optionally invoke `action()` `action_delay` seconds in (e.g. fail a gateway).
    Returns per-flow loss results once all complete.
    """
    procs = []
    for src_key, dst_key in flows:
        src, dst = ENDPOINTS[src_key], ENDPOINTS[dst_key]
        cmd = _iperf_udp_cmd(src, dst.ip, bitrate, duration, streams, length)
        procs.append((f"{src_key}->{dst_key}",
                      subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)))
    if action is not None:
        time.sleep(action_delay)
        action()
    results = []
    for name, p in procs:
        out, _ = p.communicate(timeout=duration + 60)
        results.append(_parse_iperf_json(name, out))
    return results


def dcgw_dci_out_packets(node: str) -> int:
    """Total packets a DCGW has forwarded out of its DCI-facing ports.

    Uses `fcli ifstats` (cumulative `out-pkts` counter), scoped to `node` via
    the inventory filter so only that gateway is sampled. A short sample
    interval keeps the snapshot quick; only the cumulative total is used here.
    """
    total = 0
    for r in fcli_json(["ifstats", "-s", "1"], inv=f"hostname={node}"):
        if not _fcli_node_matches(r.get("Node"), node):
            continue
        if r.get("interface") in DCGW_DCI_PORTS:
            total += int(r.get("out-pkts", 0) or 0)
    return total


def all_dcgws_meshed() -> bool:
    """True when every DCGW holds BOTH an LDP and an SR-ISIS tunnel to each
    remote-DC DCGW loopback.

    A ping canary alone cannot prove the fabric fully recovered: because every
    service fails over to its peer gateway, an isolated DCGW (ports left down) or
    an unrestored transport stays invisible to end-to-end pings. This control-
    plane check catches both, so a botched teardown surfaces instead of silently
    leaving the lab degraded.

    Uses one fabric-wide ``tunnel-table`` snapshot and filters per node, so
    `wait_until_healthy` does not issue four separate fcli calls per poll iteration.
    """
    rows = fcli_json(["tunnel-table"])
    for node in ALL_DCGWS:
        tunnels = _tunnels_from_rows(rows, node)
        for loopback in remote_dcgw_loopbacks(node):
            row = tunnels.get(f"{loopback}/32", {})
            if not all(t in row for t in WAN_TRANSPORTS):
                return False
    return True


def wait_until_healthy(timeout: float = 180.0, settle: int = 2) -> bool:
    """Poll until both canary cross-DC L2 flows are lossless AND all four DCGWs
    hold both WAN transports to every remote DCGW loopback (post-restore
    sanity)."""
    deadline = time.time() + timeout
    good = 0
    while time.time() < deadline:
        if (all(ping(src, dst.ip, count=2, interval=0.3).loss_pct == 0.0
                for src, dst in CANARIES)
                and all_dcgws_meshed()):
            good += 1
            if good >= settle:
                return True
        else:
            good = 0
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session", autouse=True)
def require_fcli():
    """Hard prerequisite: fcli >= FCLI_MIN_VERSION must be on PATH (install via
    uv) - the suite uses it for all fabric-wide reporting and needs the
    tunnel-table report. Stop the whole session with install/upgrade
    instructions if it is missing or too old, rather than silently skipping."""
    if not fcli_available():
        pytest.exit(FCLI_INSTALL_HINT, returncode=1)
    ver = fcli_version()
    if ver is None or _version_tuple(ver) < _version_tuple(FCLI_MIN_VERSION):
        pytest.exit(
            f"fcli {ver or '(version unknown)'} is too old: this suite requires "
            f">= {FCLI_MIN_VERSION} for the tunnel-table report.\n\n"
            + FCLI_INSTALL_HINT,
            returncode=1,
        )
    yield


@pytest.fixture(scope="session", autouse=True)
def lab_ready(require_fcli):
    """Skip the whole suite early if the lab is not deployed (after confirming
    the fcli prerequisite, so a missing tool surfaces its install hint first)."""
    clients = ["mh-dc1", "mh-dc2", "sh-dc1", "sh-dc2", "mh-dc1b", "mh-dc2b"]
    missing = [n for n in ALL_DCGWS + clients
               if cname(n) not in _docker_names()]
    if missing:
        pytest.skip(f"lab not deployed - missing containers: {missing}")
    yield


@pytest.fixture(scope="session")
def bgp_peers_fabric() -> list[dict]:
    """Fabric-wide BGP neighbor state (one fcli query, shared by the suite)."""
    return fcli_json(["bgp-peers"])


@pytest.fixture(scope="session")
def evpn_nexthops_by_node() -> dict[str, dict[int, set[str]]]:
    """Fabric-wide EVPN next-hops grouped by node and route type
    (2=MAC/IP, 3=inclusive-multicast, 5=IP-prefix), collected via fcli."""
    by_node: dict[str, dict[int, set[str]]] = {}
    for rt in (2, 3, 5):
        for row in fcli_json(["bgp-rib", "-r", "evpn", "-t", str(rt)]):
            node, nh = row.get("Node"), row.get("next-hop")
            if node and nh:
                by_node.setdefault(node, {}).setdefault(rt, set()).add(nh)
    return by_node


@pytest.fixture
def restore_direct_mesh():
    """Re-enable the DC1 direct-mesh ports after a failover test."""
    yield
    for node in DC1_DCGWS:
        set_ports(node, DIRECT_MESH_PORTS[node], "enable")
    assert wait_until_healthy(), "fabric did not recover after restoring direct mesh"


@pytest.fixture
def restore_dcgw():
    """Re-enable every port on any DCGW a test isolated."""
    isolated: list[str] = []
    yield isolated
    for node in isolated:
        set_ports(node, DCGW_ALL_PORTS, "enable")
    if isolated:
        assert wait_until_healthy(), "fabric did not recover after restoring DCGW(s)"


@pytest.fixture
def restore_wan_transport():
    """Re-enable any WAN MPLS transport (ldp/sr-isis) a test disabled."""
    disabled: list[str] = []
    yield disabled
    for transport in disabled:
        enable_wan_transport(transport)
    if disabled:
        assert wait_until_healthy(), "fabric did not recover after restoring WAN transport"
