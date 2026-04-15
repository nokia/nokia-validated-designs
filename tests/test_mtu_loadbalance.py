"""
MTU and load-balancing tests.

MTU tests:
  Verify end-to-end jumbo frame support by querying the fabric's
  configured MTU and probing the actual data-plane path MTU.

  The tests query fcli for the overlay (mac-vrf / ip-vrf) and underlay
  (default NI) MTU values, compute the expected cross-leaf path MTU
  accounting for VXLAN overhead, then validate with DF-bit pings.

  Key assertion: if the overlay is configured for jumbo (>1500B) but
  the underlay ISL MTU is only 1500B, cross-leaf jumbo frames will
  be silently dropped.  The test detects this mismatch.

Load-balancing tests:
  Generate multiple bidirectional iperf3 flows across the fabric and
  verify via fcli ifstats that traffic is distributed across spine
  uplinks.  Absolute throughput is irrelevant (SR Linux container
  throughput is capped); only the traffic distribution matters.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass

import pytest

from tests.conftest import ClabTopology, ClientAttachment
from tests.helpers.fcli import FcliClient
from tests.helpers.iperf import kill_server, start_server

logger = logging.getLogger(__name__)

# VXLAN encapsulation overhead:
# 14 outer Ethernet + 20 outer IP + 8 UDP + 8 VXLAN + 14 inner Ethernet = 64B
# Conservative estimate; some implementations use 50B (without inner Ethernet
# in the overhead calculation).  We use 50 for the IP-level overhead since
# the inner Ethernet header is part of the inner frame that the overlay MTU
# already accounts for.
VXLAN_OVERHEAD = 50


@dataclass
class FabricMTU:
    """Discovered MTU configuration from the live fabric."""

    underlay_mtu: int = 0       # default NI sub-interface MTU (ISL path)
    overlay_l2_mtu: int = 0     # mac-vrf sub-interface MTU
    overlay_l3_mtu: int = 0     # ip-vrf sub-interface MTU (excluding mgmt)
    expected_cross_leaf: int = 0 # max inner IP frame that fits through VXLAN
    jumbo_capable: bool = False  # True if underlay can carry jumbo VXLAN frames
    mismatch: bool = False       # True if overlay expects jumbo but underlay can't


def _discover_fabric_mtu(fcli: FcliClient) -> FabricMTU:
    """Query fcli to discover the fabric's configured MTU values."""
    ni_data = fcli.network_instances()

    underlay_mtus: list[int] = []
    overlay_l2_mtus: list[int] = []
    overlay_l3_mtus: list[int] = []

    for entry in ni_data:
        ni_type = entry.get("type", "")
        ni_name = entry.get("NI", "")
        mtu = entry.get("mtu")
        if not mtu or not isinstance(mtu, (int, float)):
            continue
        mtu = int(mtu)

        if ni_type == "default":
            underlay_mtus.append(mtu)
        elif ni_type == "mac-vrf":
            overlay_l2_mtus.append(mtu)
        elif ni_type == "ip-vrf" and ni_name != "mgmt":
            overlay_l3_mtus.append(mtu)

    result = FabricMTU()
    if underlay_mtus:
        result.underlay_mtu = min(underlay_mtus)
    if overlay_l2_mtus:
        result.overlay_l2_mtu = max(overlay_l2_mtus)
    if overlay_l3_mtus:
        result.overlay_l3_mtu = max(overlay_l3_mtus)

    if result.underlay_mtu > 0:
        result.expected_cross_leaf = result.underlay_mtu - VXLAN_OVERHEAD
    result.jumbo_capable = result.underlay_mtu > 1500
    required_underlay = max(result.overlay_l2_mtu, result.overlay_l3_mtu) + VXLAN_OVERHEAD
    result.mismatch = (
        result.underlay_mtu > 0
        and result.underlay_mtu < required_underlay
    )
    return result


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
# Underlay / overlay MTU mismatch detection
# ---------------------------------------------------------------------------


class TestMTUFabricConfig:
    """Verify that underlay MTU is large enough to carry overlay jumbo frames."""

    @pytest.mark.connectivity
    def test_underlay_supports_overlay_mtu(
        self,
        fcli: FcliClient,
        clab_topology: ClabTopology,
        record_property,
    ):
        """
        The underlay (ISL) MTU must be large enough to carry the overlay
        MTU plus VXLAN encapsulation overhead (~50B).

        If mac-vrfs are configured with 9232B MTU but the default NI
        (underlay) sub-interfaces are 1500B, cross-leaf jumbo frames
        will be silently dropped.
        """
        mtu = _discover_fabric_mtu(fcli)

        record_property("underlay_mtu", f"{mtu.underlay_mtu}B")
        record_property("overlay_l2_mtu (mac-vrf)", f"{mtu.overlay_l2_mtu}B")
        record_property("overlay_l3_mtu (ip-vrf)", f"{mtu.overlay_l3_mtu}B")
        record_property("vxlan_overhead", f"{VXLAN_OVERHEAD}B")
        record_property("expected_cross_leaf_ip_mtu", f"{mtu.expected_cross_leaf}B")
        record_property("jumbo_capable", mtu.jumbo_capable)
        record_property("mismatch_detected", mtu.mismatch)

        max_overlay = max(mtu.overlay_l2_mtu, mtu.overlay_l3_mtu)
        required_underlay = max_overlay + VXLAN_OVERHEAD
        record_property("required_underlay_mtu", f"{required_underlay}B")

        assert mtu.underlay_mtu >= required_underlay, (
            f"Underlay/overlay MTU mismatch: overlay MTU is "
            f"{max_overlay}B (L2={mtu.overlay_l2_mtu}B, L3={mtu.overlay_l3_mtu}B) "
            f"but underlay (ISL) MTU is only {mtu.underlay_mtu}B. "
            f"Required underlay MTU: >= {required_underlay}B "
            f"(overlay {max_overlay} + VXLAN overhead {VXLAN_OVERHEAD}). "
            f"Cross-leaf jumbo frames will be silently dropped."
        )
        logger.info(
            "Underlay MTU %dB supports overlay MTU %dB (required >= %dB)",
            mtu.underlay_mtu, max_overlay, required_underlay,
        )


# ---------------------------------------------------------------------------
# MTU tests -- gateway reachability with jumbo frames
# ---------------------------------------------------------------------------


class TestMTUGateway:
    """Verify jumbo frames reach the IRB anycast gateway (single hop, no VXLAN)."""

    @pytest.mark.connectivity
    def test_gateway_mtu(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        mtu_gw_att: ClientAttachment,
        record_property,
    ):
        mtu = _discover_fabric_mtu(fcli)

        record_property("source", f"{mtu_gw_att.client_container} ({mtu_gw_att.client_ip})")
        record_property("destination", f"{mtu_gw_att.gateway} (IRB gateway)")
        record_property("bridge_domain", mtu_gw_att.bridge_domain)
        record_property("configured_overlay_mtu", f"{mtu.overlay_l2_mtu}B")

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
            "Gateway MTU: %s -> %s = %dB (configured overlay: %dB)",
            mtu_gw_att.client_container, mtu_gw_att.gateway,
            frame_size, mtu.overlay_l2_mtu,
        )


# ---------------------------------------------------------------------------
# MTU tests -- L2 intra-BD across leaves (VXLAN path)
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
        mtu = _discover_fabric_mtu(fcli)
        client_mtu_a = _get_client_link_mtu(mtu_l2_a.client_container)
        client_mtu_b = _get_client_link_mtu(mtu_l2_b.client_container)
        client_mtu = min(client_mtu_a, client_mtu_b)

        record_property("bridge_domain", mtu_l2_a.bridge_domain)
        record_property("source", f"{mtu_l2_a.client_container} ({mtu_l2_a.client_ip}, link MTU {client_mtu_a}B)")
        record_property("destination", f"{mtu_l2_b.client_container} ({mtu_l2_b.client_ip}, link MTU {client_mtu_b}B)")
        record_property("underlay_mtu", f"{mtu.underlay_mtu}B")
        record_property("overlay_l2_mtu", f"{mtu.overlay_l2_mtu}B")
        record_property("client_link_mtu", f"{client_mtu}B")

        assert mtu.overlay_l2_mtu >= client_mtu, (
            f"Fabric overlay L2 MTU ({mtu.overlay_l2_mtu}B) is smaller than "
            f"client link MTU ({client_mtu}B). The fabric cannot carry the "
            f"client's maximum frames. "
            f"Path: {mtu_l2_a.client_container} -> {mtu_l2_b.client_container} "
            f"in {mtu_l2_a.bridge_domain}"
        )

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
            f"Fabric overlay is {mtu.overlay_l2_mtu}B, underlay is {mtu.underlay_mtu}B. "
            f"Path: {mtu_l2_a.client_container} -> {mtu_l2_b.client_container} "
            f"in {mtu_l2_a.bridge_domain}"
        )

        logger.info(
            "L2 MTU [%s]: %s -> %s = %dB (client=%dB, underlay=%dB, overlay=%dB)",
            mtu_l2_a.bridge_domain,
            mtu_l2_a.client_container, mtu_l2_b.client_container,
            frame_size, client_mtu, mtu.underlay_mtu, mtu.overlay_l2_mtu,
        )


# ---------------------------------------------------------------------------
# MTU tests -- L3 inter-subnet across VRFs (routed VXLAN path)
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
        mtu = _discover_fabric_mtu(fcli)
        client_mtu_a = _get_client_link_mtu(mtu_l3_a.client_container)
        client_mtu_b = _get_client_link_mtu(mtu_l3_b.client_container)
        client_mtu = min(client_mtu_a, client_mtu_b)

        record_property("source", f"{mtu_l3_a.client_container} ({mtu_l3_a.client_ip}, link MTU {client_mtu_a}B) [{mtu_l3_a.bridge_domain}]")
        record_property("destination", f"{mtu_l3_b.client_container} ({mtu_l3_b.client_ip}, link MTU {client_mtu_b}B) [{mtu_l3_b.bridge_domain}]")
        record_property("router", mtu_l3_a.router)
        record_property("underlay_mtu", f"{mtu.underlay_mtu}B")
        record_property("overlay_l3_mtu", f"{mtu.overlay_l3_mtu}B")
        record_property("client_link_mtu", f"{client_mtu}B")

        assert mtu.overlay_l3_mtu >= client_mtu, (
            f"Fabric overlay L3 MTU ({mtu.overlay_l3_mtu}B) is smaller than "
            f"client link MTU ({client_mtu}B). The fabric cannot carry the "
            f"client's maximum frames. "
            f"Path: {mtu_l3_a.client_container}({mtu_l3_a.bridge_domain}) -> "
            f"{mtu_l3_b.client_container}({mtu_l3_b.bridge_domain})"
        )

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
            f"Fabric overlay is {mtu.overlay_l3_mtu}B, underlay is {mtu.underlay_mtu}B. "
            f"Path: {mtu_l3_a.client_container}({mtu_l3_a.bridge_domain}) -> "
            f"{mtu_l3_b.client_container}({mtu_l3_b.bridge_domain})"
        )

        logger.info(
            "L3 MTU [%s]: %s(%s) -> %s(%s) = %dB (client=%dB, underlay=%dB, overlay=%dB)",
            mtu_l3_a.router,
            mtu_l3_a.client_container, mtu_l3_a.bridge_domain,
            mtu_l3_b.client_container, mtu_l3_b.bridge_domain,
            frame_size, client_mtu, mtu.underlay_mtu, mtu.overlay_l3_mtu,
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
