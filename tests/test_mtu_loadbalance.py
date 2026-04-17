"""
MTU and load-balancing tests.

MTU tests:
  Verify that each SR Linux interface's port-mtu is large enough to
  carry the configured ip-mtu plus Ethernet framing (14B header + 4B
  optional VLAN tag).  Then probe the actual data-plane path MTU with
  DF-bit pings from client containers.

Load-balancing tests:
  Generate multiple bidirectional iperf3 flows across the fabric and
  verify via fcli ifstats that traffic is distributed across spine
  uplinks.  Absolute throughput is irrelevant (SR Linux container
  throughput is capped); only the traffic distribution matters.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass

import pytest

from tests.conftest import ClabTopology, ClientAttachment
from tests.helpers.fcli import FcliClient
from tests.helpers.iperf import kill_server, start_server

logger = logging.getLogger(__name__)

ETH_HEADER = 14
VLAN_TAG = 4


@dataclass
class InterfaceMTU:
    """Port-mtu and ip-mtu pair for a single sub-interface."""

    node: str
    interface: str
    subinterface: str
    port_mtu: int
    ip_mtu: int
    vlan_tagged: bool = False

    @property
    def required_port_mtu(self) -> int:
        overhead = ETH_HEADER + (VLAN_TAG if self.vlan_tagged else 0)
        return self.ip_mtu + overhead


def _get_port_mtu(container: str) -> dict[str, int]:
    """Query port-mtu for every interface on *container* via sr_cli.

    Returns ``{"ethernet-1/3": 9412, ...}``.
    """
    cmd = [
        "docker", "exec", container, "sr_cli", "-e",
        "info from state /interface * mtu",
    ]
    result: dict[str, int] = {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return result
        current_intf = ""
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            m = re.match(r"interface\s+(\S+)", stripped)
            if m:
                current_intf = m.group(1)
            m2 = re.match(r"mtu\s+(\d+)", stripped)
            if m2 and current_intf:
                result[current_intf] = int(m2.group(1))
    except Exception as exc:
        logger.debug("Failed to query port-mtu on %s: %s", container, exc)
    return result


def _discover_interface_mtus(
    fcli: FcliClient, topo: ClabTopology,
) -> list[InterfaceMTU]:
    """Collect (port-mtu, ip-mtu) for every routed sub-interface."""
    subifs = fcli.subinterfaces()

    port_mtu_cache: dict[str, dict[str, int]] = {}
    for node in topo.srlinux_nodes:
        container = topo.container_name(node)
        port_mtu_cache[node] = _get_port_mtu(container)

    entries: list[InterfaceMTU] = []
    for row in subifs:
        node = row.get("Node", "")
        parent = row.get("Itf", "")
        subif = row.get("Subitf", "")
        ip_mtu = row.get("ip-mtu")
        if not ip_mtu or not isinstance(ip_mtu, (int, float)):
            continue
        ip_mtu = int(ip_mtu)

        port_mtu = port_mtu_cache.get(node, {}).get(parent, 0)
        if not port_mtu:
            continue

        vlan_tagged = isinstance(row.get("vlan"), int)

        entries.append(InterfaceMTU(
            node=node, interface=parent, subinterface=subif,
            port_mtu=port_mtu, ip_mtu=ip_mtu, vlan_tagged=vlan_tagged,
        ))
    return entries


# ---------------------------------------------------------------------------
# Dynamic parametrization
# ---------------------------------------------------------------------------


def _get_clab_topology(metafunc) -> ClabTopology | None:
    if "clab_topology" not in metafunc.fixturenames:
        return None
    from tests.conftest import _build_topology_from_metafunc
    return _build_topology_from_metafunc(metafunc)


def pytest_generate_tests(metafunc):
    # MTU gateway tests
    if "mtu_gw_att" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            params = [
                pytest.param(
                    att,
                    id=f"{att.client_container}({att.client_ip})->{att.gateway}[{att.bridge_domain}]",
                )
                for att in topo.client_attachments
                if att.gateway
            ]
            metafunc.parametrize("mtu_gw_att", params)

    # MTU L2 intra-BD tests
    if "mtu_l2_a" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            by_bd: dict[str, list[ClientAttachment]] = defaultdict(list)
            for att in topo.client_attachments:
                by_bd[att.bridge_domain].append(att)
            params = []
            for bd, members in sorted(by_bd.items()):
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        params.append(pytest.param(
                            a, b,
                            id=f"{bd}:{a.client_container}->{b.client_container}",
                        ))
            metafunc.parametrize("mtu_l2_a,mtu_l2_b", params)

    # MTU L3 inter-subnet tests
    if "mtu_l3_a" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            by_vrf: dict[str, dict[str, ClientAttachment]] = defaultdict(dict)
            for att in topo.client_attachments:
                if att.router:
                    key = f"{att.client_container}:{att.bridge_domain}"
                    if key not in by_vrf[att.router]:
                        by_vrf[att.router][key] = att
            params = []
            seen = set()
            for vrf, bd_map in sorted(by_vrf.items()):
                members = list(bd_map.values())
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        if a.bridge_domain != b.bridge_domain:
                            pair_key = tuple(sorted([
                                f"{a.client_container}:{a.client_ip}",
                                f"{b.client_container}:{b.client_ip}",
                            ]))
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)
                            params.append(pytest.param(
                                a, b,
                                id=f"{vrf}:{a.client_container}({a.bridge_domain})->{b.client_container}({b.bridge_domain})",
                            ))
            metafunc.parametrize("mtu_l3_a,mtu_l3_b", params)


# ---------------------------------------------------------------------------
# MTU probing
# ---------------------------------------------------------------------------


def _ping_with_size(
    container: str, target: str, payload: int, *, src_ip: str = "",
) -> bool:
    """Single ping with DF bit set at the given payload size."""
    cmd = [
        "docker", "exec", container,
        "ping", "-c", "1", "-W", "2", "-s", str(payload), "-M", "do",
    ]
    if src_ip:
        cmd.extend(["-I", src_ip])
    cmd.append(target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def _discover_path_mtu(
    container: str, target: str, *, src_ip: str = "",
) -> int:
    """
    Binary search for the maximum ICMP payload that traverses the path
    without fragmentation (DF bit set).

    Returns the max payload size in bytes. The on-wire frame size is
    payload + 28 (20 IP + 8 ICMP header).
    """
    client_mtu = _get_client_link_mtu(container)
    hi = client_mtu - 28  # IP + ICMP headers
    lo = 56  # default ping payload
    best = 0

    # Quick check: does the maximum work?
    if _ping_with_size(container, target, hi, src_ip=src_ip):
        return hi

    # Quick check: does the minimum work?
    if not _ping_with_size(container, target, lo, src_ip=src_ip):
        return 0

    while lo <= hi:
        mid = (lo + hi) // 2
        if _ping_with_size(container, target, mid, src_ip=src_ip):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


def _get_client_link_mtu(container: str) -> int:
    """Return the lowest data-interface MTU on a client container.

    Checks all ``eth*`` and ``bond*`` interfaces (skipping ``eth0`` which is
    the management link) and returns the minimum MTU found, since that is the
    ceiling for user traffic.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container, "ip", "-j", "link", "show"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return 1500
        import json as _json
        links = _json.loads(result.stdout)
        mtus: list[int] = []
        for link in links:
            name = link.get("ifname", "")
            if name == "lo" or name == "eth0":
                continue
            if name.startswith(("eth", "bond")):
                mtus.append(link.get("mtu", 1500))
        return min(mtus) if mtus else 1500
    except Exception:
        return 1500


# ---------------------------------------------------------------------------
# Port-MTU vs IP-MTU configuration check
# ---------------------------------------------------------------------------


class TestMTUFabricConfig:
    """Verify port-mtu >= ip-mtu + Ethernet framing on every interface."""

    @pytest.mark.connectivity
    def test_port_mtu_covers_ip_mtu(
        self,
        fcli: FcliClient,
        clab_topology: ClabTopology,
        record_property,
    ):
        """
        For every routed sub-interface, the parent interface's port-mtu
        must be at least ip-mtu + 14B (Ethernet) + 4B (VLAN tag, if
        the interface is VLAN-tagged).
        """
        entries = _discover_interface_mtus(fcli, clab_topology)
        record_property("interfaces_checked", len(entries))

        violations: list[str] = []
        for e in entries:
            if e.port_mtu < e.required_port_mtu:
                tag = " +4B VLAN" if e.vlan_tagged else ""
                violations.append(
                    f"{e.node} {e.subinterface}: port-mtu={e.port_mtu}B < "
                    f"ip-mtu({e.ip_mtu}) + {ETH_HEADER}B ETH{tag} = "
                    f"{e.required_port_mtu}B"
                )

        record_property("violations", len(violations))
        assert not violations, (
            f"{len(violations)} interface(s) where port-mtu cannot carry "
            f"the configured ip-mtu:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )
        logger.info(
            "Port-MTU check passed: %d interfaces, all port-mtu >= ip-mtu + framing",
            len(entries),
        )


# ---------------------------------------------------------------------------
# MTU tests -- gateway reachability with jumbo frames
# ---------------------------------------------------------------------------


class TestMTUGateway:
    """Verify jumbo frames reach the IRB anycast gateway (single hop)."""

    @pytest.mark.connectivity
    def test_gateway_mtu(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        mtu_gw_att: ClientAttachment,
        record_property,
    ):
        record_property("source", f"{mtu_gw_att.client_container} ({mtu_gw_att.client_ip})")
        record_property("destination", f"{mtu_gw_att.gateway} (IRB gateway)")
        record_property("bridge_domain", mtu_gw_att.bridge_domain)

        client_mtu = _get_client_link_mtu(mtu_gw_att.client_container)
        record_property("client_link_mtu", f"{client_mtu}B")

        max_payload = _discover_path_mtu(
            mtu_gw_att.client_container, mtu_gw_att.gateway,
            src_ip=mtu_gw_att.client_ip,
        )
        frame_size = max_payload + 28
        record_property("actual_path_mtu", f"{frame_size}B")

        assert max_payload >= 1472, (
            f"Gateway path MTU too low: {frame_size}B "
            f"({mtu_gw_att.client_container} -> {mtu_gw_att.gateway}). "
            f"Expected at least 1500B frames."
        )
        logger.info(
            "Gateway MTU: %s -> %s = %dB (client link MTU: %dB)",
            mtu_gw_att.client_container, mtu_gw_att.gateway,
            frame_size, client_mtu,
        )


# ---------------------------------------------------------------------------
# MTU tests -- L2 intra-BD across leaves
# ---------------------------------------------------------------------------


class TestMTUL2IntraBD:
    """Verify the fabric supports each client's link MTU for L2 cross-leaf paths."""

    @pytest.mark.connectivity
    def test_l2_path_mtu(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        mtu_l2_a: ClientAttachment,
        mtu_l2_b: ClientAttachment,
        record_property,
    ):
        client_mtu_a = _get_client_link_mtu(mtu_l2_a.client_container)
        client_mtu_b = _get_client_link_mtu(mtu_l2_b.client_container)
        client_mtu = min(client_mtu_a, client_mtu_b)

        record_property("bridge_domain", mtu_l2_a.bridge_domain)
        record_property("source", f"{mtu_l2_a.client_container} ({mtu_l2_a.client_ip}, link MTU {client_mtu_a}B)")
        record_property("destination", f"{mtu_l2_b.client_container} ({mtu_l2_b.client_ip}, link MTU {client_mtu_b}B)")
        record_property("client_link_mtu", f"{client_mtu}B")

        max_payload = _discover_path_mtu(
            mtu_l2_a.client_container, mtu_l2_b.client_ip,
            src_ip=mtu_l2_a.client_ip,
        )
        frame_size = max_payload + 28
        record_property("actual_path_mtu", f"{frame_size}B")

        expected_payload = client_mtu - 28
        assert max_payload >= expected_payload, (
            f"L2 cross-leaf path MTU too low: actual {frame_size}B vs "
            f"client link MTU {client_mtu}B. "
            f"Path: {mtu_l2_a.client_container} -> {mtu_l2_b.client_container} "
            f"in {mtu_l2_a.bridge_domain}"
        )

        logger.info(
            "L2 MTU [%s]: %s -> %s = %dB (client link MTU=%dB)",
            mtu_l2_a.bridge_domain,
            mtu_l2_a.client_container, mtu_l2_b.client_container,
            frame_size, client_mtu,
        )


# ---------------------------------------------------------------------------
# MTU tests -- L3 inter-subnet across VRFs
# ---------------------------------------------------------------------------


class TestMTUL3InterSubnet:
    """Verify the fabric supports each client's link MTU for L3 cross-leaf paths."""

    @pytest.mark.connectivity
    def test_l3_path_mtu(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        mtu_l3_a: ClientAttachment,
        mtu_l3_b: ClientAttachment,
        record_property,
    ):
        client_mtu_a = _get_client_link_mtu(mtu_l3_a.client_container)
        client_mtu_b = _get_client_link_mtu(mtu_l3_b.client_container)
        client_mtu = min(client_mtu_a, client_mtu_b)

        record_property("source", f"{mtu_l3_a.client_container} ({mtu_l3_a.client_ip}, link MTU {client_mtu_a}B) [{mtu_l3_a.bridge_domain}]")
        record_property("destination", f"{mtu_l3_b.client_container} ({mtu_l3_b.client_ip}, link MTU {client_mtu_b}B) [{mtu_l3_b.bridge_domain}]")
        record_property("router", mtu_l3_a.router)
        record_property("client_link_mtu", f"{client_mtu}B")

        max_payload = _discover_path_mtu(
            mtu_l3_a.client_container, mtu_l3_b.client_ip,
            src_ip=mtu_l3_a.client_ip,
        )
        frame_size = max_payload + 28
        record_property("actual_path_mtu", f"{frame_size}B")

        expected_payload = client_mtu - 28
        assert max_payload >= expected_payload, (
            f"L3 cross-leaf path MTU too low: actual {frame_size}B vs "
            f"client link MTU {client_mtu}B. "
            f"Path: {mtu_l3_a.client_container}({mtu_l3_a.bridge_domain}) -> "
            f"{mtu_l3_b.client_container}({mtu_l3_b.bridge_domain})"
        )

        logger.info(
            "L3 MTU [%s]: %s(%s) -> %s(%s) = %dB (client link MTU=%dB)",
            mtu_l3_a.router,
            mtu_l3_a.client_container, mtu_l3_a.bridge_domain,
            mtu_l3_b.client_container, mtu_l3_b.bridge_domain,
            frame_size, client_mtu,
        )


# ---------------------------------------------------------------------------
# Load-balancing tests
# ---------------------------------------------------------------------------


class TestECMPLoadBalancing:
    """
    Verify ECMP load-balancing by generating multiple bidirectional iperf3
    flows between clients on different leaves, then checking that spine
    uplinks all carry traffic.

    The test does NOT assert throughput values (SR Linux container throughput
    is capped).  It only checks that traffic is *present* on multiple spine
    interfaces, confirming that hashing distributes flows across ECMP paths.
    """

    @pytest.mark.connectivity
    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_ecmp_spine_distribution(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        record_property,
    ):
        if len(clab_topology.spine_nodes) < 2:
            pytest.skip("Need at least 2 spines to verify ECMP distribution")
        if len(clab_topology.client_attachments) < 2:
            pytest.skip("Need at least 2 client attachments for traffic flows")

        flows = _build_flow_pairs(clab_topology)
        if not flows:
            pytest.skip("Cannot build sufficient flow pairs for load-balancing test")

        record_property("flow_count", len(flows))
        record_property("spines", ", ".join(clab_topology.spine_nodes))

        servers_started: list[tuple[str, int]] = []
        port = 9200

        try:
            # Start iperf3 servers on all dst containers (forward + reverse)
            for src, dst in flows:
                start_server(dst.client_container, bind_ip=dst.client_ip, port=port)
                servers_started.append((dst.client_container, port))
                start_server(src.client_container, bind_ip=src.client_ip, port=port + 100)
                servers_started.append((src.client_container, port + 100))
                port += 1

            # Capture baseline ifstats on spines
            baseline = _spine_ifstats(fcli, clab_topology)
            record_property("baseline_stats", _format_ifstats(baseline))

            # Launch all iperf3 clients (forward + reverse) concurrently
            client_procs: list[subprocess.Popen] = []
            flow_port = 9200
            flow_duration = 10

            for src, dst in flows:
                # Forward: src -> dst
                cmd_fwd = [
                    "docker", "exec", src.client_container,
                    "iperf3", "-c", dst.client_ip, "-B", src.client_ip,
                    "-p", str(flow_port), "-t", str(flow_duration), "-P", "2",
                ]
                client_procs.append(subprocess.Popen(
                    cmd_fwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
                logger.info(
                    "Flow: %s(%s) -> %s(%s) port %d",
                    src.client_container, src.client_ip,
                    dst.client_container, dst.client_ip, flow_port,
                )

                # Reverse: dst -> src
                cmd_rev = [
                    "docker", "exec", dst.client_container,
                    "iperf3", "-c", src.client_ip, "-B", dst.client_ip,
                    "-p", str(flow_port + 100), "-t", str(flow_duration), "-P", "2",
                ]
                client_procs.append(subprocess.Popen(
                    cmd_rev, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
                logger.info(
                    "Flow: %s(%s) -> %s(%s) port %d (reverse)",
                    dst.client_container, dst.client_ip,
                    src.client_container, src.client_ip, flow_port + 100,
                )
                flow_port += 1

            # Wait for flows to generate traffic, then sample ifstats
            time.sleep(3)
            active = _spine_ifstats(fcli, clab_topology)
            record_property("active_stats", _format_ifstats(active))

            for proc in client_procs:
                try:
                    proc.wait(timeout=flow_duration + 10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        finally:
            for container, p in servers_started:
                kill_server(container, p)

        # Analyze: check that multiple spine interfaces carried traffic
        TRAFFIC_THRESHOLD_KBPS = 5.0

        active_spine_intfs: dict[str, list[str]] = defaultdict(list)
        for spine in clab_topology.spine_nodes:
            for intf, stats in active.get(spine, {}).items():
                if intf == "mgmt0":
                    continue
                base = baseline.get(spine, {}).get(intf, {})
                base_total = base.get("in-Kbps", 0) + base.get("out-Kbps", 0)
                act_total = stats.get("in-Kbps", 0) + stats.get("out-Kbps", 0)

                if (act_total - base_total) > TRAFFIC_THRESHOLD_KBPS:
                    active_spine_intfs[spine].append(intf)

        for spine in clab_topology.spine_nodes:
            intfs = active_spine_intfs.get(spine, [])
            record_property(
                f"{spine}_active_interfaces",
                f"{len(intfs)} ({', '.join(intfs)})" if intfs else "0 (no traffic above baseline)",
            )

        spines_with_traffic = [
            s for s in clab_topology.spine_nodes
            if active_spine_intfs.get(s)
        ]
        record_property("spines_with_traffic", len(spines_with_traffic))

        assert len(spines_with_traffic) >= 2, (
            f"Only {len(spines_with_traffic)}/{len(clab_topology.spine_nodes)} "
            f"spines carried traffic above baseline. "
            f"ECMP load-balancing may not be working. "
            f"Active: {dict(active_spine_intfs)}"
        )

        for spine in clab_topology.spine_nodes:
            intfs = active_spine_intfs.get(spine, [])
            logger.info(
                "%s: %d interfaces with traffic: %s",
                spine, len(intfs), intfs,
            )

    @pytest.mark.connectivity
    @pytest.mark.timeout(60)
    def test_no_interface_discards(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        record_property,
    ):
        """No discards on any fabric interface.

        Checks for in/out discards which indicate packets being dropped
        (e.g. due to buffer overflow, policy, or MTU issues).  Small
        transient error counts on veth interfaces are normal in
        containerlab and are tolerated.
        """
        stats = fcli.ifstats(interval=3)
        discards = []
        for entry in stats:
            node = entry.get("Node", "")
            intf = entry.get("interface", "")
            if intf == "mgmt0":
                continue
            in_disc = entry.get("in-disc", 0)
            out_disc = entry.get("out-disc", 0)
            if in_disc or out_disc:
                discards.append(
                    f"{node}/{intf}: in-disc={in_disc} out-disc={out_disc}"
                )

        record_property("interfaces_checked", len(stats))
        record_property("interfaces_with_discards", len(discards))
        if discards:
            record_property("discard_details", "; ".join(discards))

        assert not discards, (
            f"{len(discards)} interface(s) with discards:\n"
            + "\n".join(f"  - {d}" for d in discards)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_flow_pairs(
    topo: ClabTopology,
) -> list[tuple[ClientAttachment, ClientAttachment]]:
    """
    Build a set of flow pairs that maximize path diversity.

    Pairs clients on different containers within the same VRF to ensure
    traffic crosses the spine layer.
    """
    by_vrf_container: dict[str, dict[str, ClientAttachment]] = defaultdict(dict)
    for att in topo.client_attachments:
        if att.router:
            by_vrf_container[att.router][att.client_container] = att

    pairs: list[tuple[ClientAttachment, ClientAttachment]] = []
    seen: set[tuple[str, str]] = set()

    for vrf, container_map in by_vrf_container.items():
        containers = sorted(container_map.keys())
        for i, c1 in enumerate(containers):
            for c2 in containers[i + 1:]:
                key = (c1, c2)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((container_map[c1], container_map[c2]))

    return pairs


def _spine_ifstats(
    fcli: FcliClient, topo: ClabTopology,
) -> dict[str, dict[str, dict]]:
    """Collect ifstats for all spine nodes."""
    result: dict[str, dict[str, dict]] = {}
    for spine in topo.spine_nodes:
        stats = fcli.ifstats(node=spine, interval=5)
        intf_map: dict[str, dict] = {}
        for entry in stats:
            intf_map[entry.get("interface", "")] = entry
        result[spine] = intf_map
    return result


def _format_ifstats(stats: dict[str, dict[str, dict]]) -> str:
    """Format ifstats for record_property display."""
    parts = []
    for spine, intfs in sorted(stats.items()):
        for intf, data in sorted(intfs.items()):
            if intf == "mgmt0":
                continue
            parts.append(
                f"{spine}/{intf}: in={data.get('in-Kbps', 0):.0f}K "
                f"out={data.get('out-Kbps', 0):.0f}K"
            )
    return "; ".join(parts)
