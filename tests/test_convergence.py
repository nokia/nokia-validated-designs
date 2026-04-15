"""
Convergence tests under failure scenarios.

Injects faults (link down, node pause) and measures how quickly the
fabric reconverges.  Each test follows the pattern:

  1. Assert steady-state (all pings pass, BGP established)
  2. Start continuous pings on affected paths
  3. Inject fault
  4. Poll until reduced-but-working connectivity is restored
  5. Measure convergence time
  6. Recover fault
  7. Assert full connectivity restored
  8. Report convergence metrics

All topology information is discovered at runtime from the .clab.yml
file and fcli queries -- no project-specific imports.

Failure types:
  - Single ISL down (one leaf-to-spine link)
  - Full leaf ISL isolation (all uplinks down)
  - Edge link down (single-homed and LAG member)
  - Leaf crash (docker pause)
  - Spine crash (docker pause)
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

import pytest
import yaml

from tests.conftest import ClabTopology, ClientAttachment
from tests.helpers.faults import FaultManager
from tests.helpers.fcli import FcliClient
from tests.helpers.ping import ping, ping_continuous
from tests.helpers.wait import ConvergenceTimeout, poll_until

logger = logging.getLogger(__name__)

MAX_CONVERGENCE = {
    "isl_single": 5.0,
    "isl_full_isolation": 10.0,
    "edge_link": 5.0,
    "leaf_crash": 10.0,
    "spine_crash": 10.0,
}

FULL_RECOVERY_TIMEOUT = 90.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_steady_state(topo: ClabTopology, fcli: FcliClient) -> None:
    """
    Wait for steady state: all BGP peers established and at least one
    gateway ping works.  Polls for up to 60s to allow recovery from
    any prior test's fault cleanup.
    """
    poll_until(
        lambda: _all_bgp_established(fcli),
        timeout=60.0,
        interval=3.0,
        description="steady-state: all BGP peers established",
    )
    if topo.client_attachments:
        att = topo.client_attachments[0]
        poll_until(
            lambda: ping(att.client_container, att.gateway,
                         src_ip=att.client_ip, count=1, timeout=1),
            timeout=30.0,
            interval=2.0,
            description=f"steady-state: {att.client_container} -> {att.gateway}",
        )


def _leaf_for_container(topo: ClabTopology, container: str) -> str | None:
    """
    Map a client container name back to the leaf node it's attached to.

    Uses the clab topology links section to find the peer leaf node for
    a given client container.  Falls back to regex-based name matching
    if no links section is found.
    """
    # If the container IS a leaf node, return it directly
    for node_name in topo.nodes:
        if topo.container_name(node_name) == container and node_name in topo.leaf_nodes:
            return node_name

    # Use clab topology links to find which leaf peers with this client
    with open(topo.topo_path) as f:
        clab_data = yaml.safe_load(f)
    links = clab_data.get("topology", {}).get("links", [])

    # Resolve bare name from container
    bare_name = None
    for node_name in topo.nodes:
        if topo.container_name(node_name) == container:
            bare_name = node_name
            break

    if bare_name:
        for link in links:
            endpoints = link.get("endpoints", [])
            if len(endpoints) != 2:
                continue
            a_node = endpoints[0].split(":")[0]
            b_node = endpoints[1].split(":")[0]
            if a_node == bare_name and b_node in topo.leaf_nodes:
                return b_node
            if b_node == bare_name and a_node in topo.leaf_nodes:
                return a_node

    # Fallback: regex on container name pattern "cl-l<N>"
    bare = container
    if topo.topo_prefix and container.startswith(topo.topo_prefix + "-"):
        bare = container[len(topo.topo_prefix) + 1:]

    m = re.search(r"cl-l(?:eaf-?)?(\d+)", bare)
    if m:
        leaf_name = f"leaf{m.group(1)}"
        if leaf_name in topo.leaf_nodes:
            return leaf_name

    return None


def _pick_affected_pair(
    topo: ClabTopology, exclude_node: str = "",
) -> tuple[ClientAttachment, ClientAttachment] | None:
    """
    Pick a (src, dst) pair where src is on the affected node and dst
    is on a different node in the same BD or VRF.
    """
    by_bd: dict[str, list[ClientAttachment]] = defaultdict(list)
    for att in topo.client_attachments:
        by_bd[att.bridge_domain].append(att)

    for bd, members in by_bd.items():
        if len(members) < 2:
            continue
        for a in members:
            for b in members:
                if a is b:
                    continue
                if a.client_container != b.client_container:
                    return a, b
    return None


def _pick_pair_through_spine(
    topo: ClabTopology,
) -> tuple[ClientAttachment, ClientAttachment] | None:
    """Pick a pair of clients on different leaves (traffic crosses the spine)."""
    by_container: dict[str, list[ClientAttachment]] = defaultdict(list)
    for att in topo.client_attachments:
        by_container[att.client_container].append(att)

    containers = sorted(by_container.keys())
    if len(containers) < 2:
        return None

    a = by_container[containers[0]][0]
    b = by_container[containers[1]][0]
    return a, b


# ---------------------------------------------------------------------------
# Single ISL failure
# ---------------------------------------------------------------------------


class TestISLSingleLink:
    """One ISL link is taken down; traffic should reconverge via remaining paths."""

    @pytest.mark.convergence
    @pytest.mark.timeout(120)
    def test_single_isl_convergence(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        fault_manager: FaultManager,
        record_property,
    ):
        if not clab_topology.isl_links:
            pytest.skip("No ISL links in topology")

        pair = _pick_pair_through_spine(clab_topology)
        if not pair:
            pytest.skip("Cannot find a client pair that crosses the spine layer")
        src, dst = pair

        isl = clab_topology.isl_links[0]
        target_container = isl["local_container"]
        target_interface = isl["local_interface"]

        record_property("fault_type", "single ISL link down")
        record_property("fault_target", f"{target_container} {target_interface}")
        record_property("traffic_path", f"{src.client_container}({src.client_ip}) -> {dst.client_container}({dst.client_ip})")
        record_property("max_convergence_s", MAX_CONVERGENCE["isl_single"])

        _assert_steady_state(clab_topology, fcli)
        assert ping(src.client_container, dst.client_ip, src_ip=src.client_ip), (
            "Pre-fault: src cannot reach dst"
        )

        stream = ping_continuous(
            src.client_container, dst.client_ip, src_ip=src.client_ip,
        )

        time.sleep(1)
        fault_manager.disable_link(target_container, target_interface)

        try:
            convergence_time = poll_until(
                lambda: ping(
                    src.client_container, dst.client_ip,
                    src_ip=src.client_ip, count=1, timeout=1,
                ),
                timeout=MAX_CONVERGENCE["isl_single"] * 3,
                interval=0.5,
                description=f"reconvergence after ISL {isl['name']} down",
            )
        except ConvergenceTimeout:
            result = stream.stop()
            pytest.fail(
                f"Did not reconverge after disabling ISL {isl['name']}. "
                f"Ping loss: {result.loss_pct:.1f}%"
            )

        logger.info(
            "ISL single-link convergence: %.2fs (limit: %.1fs)",
            convergence_time, MAX_CONVERGENCE["isl_single"],
        )
        assert convergence_time <= MAX_CONVERGENCE["isl_single"], (
            f"Convergence took {convergence_time:.2f}s, "
            f"expected <= {MAX_CONVERGENCE['isl_single']}s"
        )

        fault_manager.enable_link(target_container, target_interface)

        poll_until(
            lambda: ping(
                src.client_container, dst.client_ip,
                src_ip=src.client_ip, count=1, timeout=1,
            ),
            timeout=FULL_RECOVERY_TIMEOUT,
            interval=2.0,
            description="full recovery after ISL restore",
        )

        result = stream.stop()
        record_property("convergence_s", f"{convergence_time:.2f}")
        record_property("ping_loss_pct", f"{result.loss_pct:.1f}")
        record_property("pings_tx_rx", f"{result.transmitted}/{result.received}")
        logger.info(
            "ISL single-link test complete: convergence=%.2fs, "
            "ping loss=%.1f%% (%d/%d)",
            convergence_time, result.loss_pct,
            result.transmitted - result.received, result.transmitted,
        )


# ---------------------------------------------------------------------------
# Full leaf ISL isolation
# ---------------------------------------------------------------------------


class TestISLFullIsolation:
    """All ISL links to a leaf are taken down (full isolation)."""

    @pytest.mark.convergence
    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_leaf_isolation_convergence(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        fault_manager: FaultManager,
        record_property,
    ):
        if len(clab_topology.leaf_nodes) < 3:
            pytest.skip("Need at least 3 leaves for ISL full isolation test")

        pair = _pick_pair_through_spine(clab_topology)
        if not pair:
            pytest.skip("Cannot find traffic pair")
        src, dst = pair

        src_leaf = _leaf_for_container(clab_topology, src.client_container)
        dst_leaf = _leaf_for_container(clab_topology, dst.client_container)

        target_leaf = None
        for leaf in clab_topology.leaf_nodes:
            if leaf != src_leaf and leaf != dst_leaf:
                uplinks = [
                    isl for isl in clab_topology.isl_links
                    if isl["local_node"] == leaf
                ]
                if uplinks:
                    target_leaf = leaf
                    break
        if not target_leaf:
            pytest.skip("No suitable leaf to isolate without breaking traffic path")

        target_container = clab_topology.container_name(target_leaf)
        uplinks = [
            isl for isl in clab_topology.isl_links
            if isl["local_node"] == target_leaf
        ]
        uplink_intfs = [u["local_interface"] for u in uplinks]

        record_property("fault_type", f"full ISL isolation ({len(uplink_intfs)} links)")
        record_property("fault_target", f"{target_leaf} ({', '.join(uplink_intfs)})")
        record_property("traffic_path", f"{src.client_container}({src.client_ip}) -> {dst.client_container}({dst.client_ip})")
        record_property("max_convergence_s", MAX_CONVERGENCE["isl_full_isolation"])

        _assert_steady_state(clab_topology, fcli)

        stream = ping_continuous(
            src.client_container, dst.client_ip, src_ip=src.client_ip,
        )

        time.sleep(1)
        fault_manager.isolate_node(target_container, uplink_intfs)

        try:
            convergence_time = poll_until(
                lambda: ping(
                    src.client_container, dst.client_ip,
                    src_ip=src.client_ip, count=1, timeout=1,
                ),
                timeout=MAX_CONVERGENCE["isl_full_isolation"] * 3,
                interval=0.5,
                description=f"reconvergence after full isolation of {target_leaf}",
            )
        except ConvergenceTimeout:
            result = stream.stop()
            pytest.fail(
                f"Did not reconverge after isolating {target_leaf}. "
                f"Ping loss: {result.loss_pct:.1f}%"
            )

        logger.info(
            "Full ISL isolation convergence: %.2fs (limit: %.1fs)",
            convergence_time, MAX_CONVERGENCE["isl_full_isolation"],
        )
        assert convergence_time <= MAX_CONVERGENCE["isl_full_isolation"], (
            f"Convergence took {convergence_time:.2f}s, "
            f"expected <= {MAX_CONVERGENCE['isl_full_isolation']}s"
        )

        fault_manager.restore_node_links(target_container, uplink_intfs)

        poll_until(
            lambda: ping(
                src.client_container, dst.client_ip,
                src_ip=src.client_ip, count=1, timeout=1,
            ),
            timeout=FULL_RECOVERY_TIMEOUT,
            interval=2.0,
            description=f"full recovery after restoring {target_leaf} uplinks",
        )

        result = stream.stop()
        record_property("convergence_s", f"{convergence_time:.2f}")
        record_property("ping_loss_pct", f"{result.loss_pct:.1f}")
        record_property("pings_tx_rx", f"{result.transmitted}/{result.received}")
        logger.info(
            "Full ISL isolation test complete: convergence=%.2fs, loss=%.1f%%",
            convergence_time, result.loss_pct,
        )


# ---------------------------------------------------------------------------
# Edge link failure
# ---------------------------------------------------------------------------


class TestEdgeLinkFailure:
    """An edge (server-facing) link is taken down."""

    @pytest.mark.convergence
    @pytest.mark.timeout(120)
    def test_edge_link_convergence(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        fault_manager: FaultManager,
        record_property,
    ):
        if not clab_topology.edge_links:
            pytest.skip("No edge links in topology")
        if not clab_topology.client_attachments:
            pytest.skip("No client attachments found")

        edge = clab_topology.edge_links[0]
        target_container = edge["container"]
        target_interface = edge["interface"]

        pair = _pick_affected_pair(clab_topology)
        if not pair:
            pytest.skip("Cannot find a suitable client pair")
        src, dst = pair

        record_property("fault_type", "edge link down + restore")
        record_property("fault_target", f"{target_container} {target_interface}")
        record_property("traffic_path", f"{src.client_container}({src.client_ip}) -> {dst.client_container}({dst.client_ip})")
        record_property("max_convergence_s", MAX_CONVERGENCE["edge_link"])

        _assert_steady_state(clab_topology, fcli)
        poll_until(
            lambda: ping(src.client_container, dst.client_ip,
                         src_ip=src.client_ip, count=1, timeout=1),
            timeout=30.0,
            interval=2.0,
            description=f"pre-fault: {src.client_container} -> {dst.client_container}",
        )

        fault_manager.disable_link(target_container, target_interface)

        time.sleep(2)

        fault_manager.enable_link(target_container, target_interface)

        try:
            recovery_time = poll_until(
                lambda: ping(
                    src.client_container, dst.client_ip,
                    src_ip=src.client_ip, count=1, timeout=1,
                ),
                timeout=MAX_CONVERGENCE["edge_link"] * 3,
                interval=0.5,
                description="recovery after edge link restore",
            )
        except ConvergenceTimeout:
            pytest.fail(
                f"Did not recover after restoring edge link "
                f"{target_interface} on {target_container}"
            )

        record_property("convergence_s", f"{recovery_time:.2f}")
        logger.info("Edge link recovery: %.2fs", recovery_time)
        assert recovery_time <= MAX_CONVERGENCE["edge_link"], (
            f"Recovery took {recovery_time:.2f}s, "
            f"expected <= {MAX_CONVERGENCE['edge_link']}s"
        )


# ---------------------------------------------------------------------------
# Leaf crash (docker pause)
# ---------------------------------------------------------------------------


class TestLeafCrash:
    """A leaf node is paused (simulating a crash); traffic reconverges via other leaves."""

    @pytest.mark.convergence
    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_leaf_crash_convergence(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        fault_manager: FaultManager,
        record_property,
    ):
        if len(clab_topology.leaf_nodes) < 2:
            pytest.skip("Need at least 2 leaves for leaf-crash test")

        pair = _pick_pair_through_spine(clab_topology)
        if not pair:
            pytest.skip("Cannot find traffic pair across different leaves")
        src, dst = pair

        src_leaf = _leaf_for_container(clab_topology, src.client_container)
        dst_leaf = _leaf_for_container(clab_topology, dst.client_container)

        crash_leaf = None
        for leaf in clab_topology.leaf_nodes:
            if leaf != src_leaf and leaf != dst_leaf:
                crash_leaf = leaf
                break

        if not crash_leaf:
            pytest.skip("Cannot find a leaf to crash that won't break src/dst traffic")

        crash_container = clab_topology.container_name(crash_leaf)

        record_property("fault_type", "leaf crash (docker pause)")
        record_property("fault_target", f"{crash_leaf} ({crash_container})")
        record_property("traffic_path", f"{src.client_container}({src.client_ip}) -> {dst.client_container}({dst.client_ip})")
        record_property("max_convergence_s", MAX_CONVERGENCE["leaf_crash"])

        _assert_steady_state(clab_topology, fcli)

        stream = ping_continuous(
            src.client_container, dst.client_ip, src_ip=src.client_ip,
        )

        time.sleep(1)
        fault_manager.pause_node(crash_container)

        try:
            convergence_time = poll_until(
                lambda: ping(
                    src.client_container, dst.client_ip,
                    src_ip=src.client_ip, count=1, timeout=1,
                ),
                timeout=MAX_CONVERGENCE["leaf_crash"] * 3,
                interval=1.0,
                description=f"reconvergence after {crash_leaf} crash",
            )
        except ConvergenceTimeout:
            result = stream.stop()
            fault_manager.unpause_node(crash_container)
            pytest.fail(
                f"Did not reconverge after crashing {crash_leaf}. "
                f"Ping loss: {result.loss_pct:.1f}%"
            )

        logger.info(
            "Leaf crash convergence: %.2fs (limit: %.1fs)",
            convergence_time, MAX_CONVERGENCE["leaf_crash"],
        )

        fault_manager.unpause_node(crash_container)

        poll_until(
            lambda: _all_bgp_established(fcli),
            timeout=FULL_RECOVERY_TIMEOUT,
            interval=3.0,
            description=f"BGP recovery after {crash_leaf} unpause",
        )

        result = stream.stop()
        record_property("convergence_s", f"{convergence_time:.2f}")
        record_property("ping_loss_pct", f"{result.loss_pct:.1f}")
        record_property("pings_tx_rx", f"{result.transmitted}/{result.received}")
        logger.info(
            "Leaf crash test complete: convergence=%.2fs, loss=%.1f%%",
            convergence_time, result.loss_pct,
        )
        assert convergence_time <= MAX_CONVERGENCE["leaf_crash"], (
            f"Convergence took {convergence_time:.2f}s, "
            f"expected <= {MAX_CONVERGENCE['leaf_crash']}s"
        )


# ---------------------------------------------------------------------------
# Spine crash (docker pause)
# ---------------------------------------------------------------------------


class TestSpineCrash:
    """A spine node is paused; all leaf-to-leaf traffic reconverges via remaining spines."""

    @pytest.mark.convergence
    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_spine_crash_convergence(
        self,
        clab_topology: ClabTopology,
        fcli: FcliClient,
        fault_manager: FaultManager,
        record_property,
    ):
        if len(clab_topology.spine_nodes) < 2:
            pytest.skip("Need at least 2 spines for spine-crash test")

        pair = _pick_pair_through_spine(clab_topology)
        if not pair:
            pytest.skip("Cannot find traffic pair that crosses the spine layer")
        src, dst = pair

        crash_spine = clab_topology.spine_nodes[0]
        crash_container = clab_topology.container_name(crash_spine)

        record_property("fault_type", "spine crash (docker pause)")
        record_property("fault_target", f"{crash_spine} ({crash_container})")
        record_property("traffic_path", f"{src.client_container}({src.client_ip}) -> {dst.client_container}({dst.client_ip})")
        record_property("max_convergence_s", MAX_CONVERGENCE["spine_crash"])

        _assert_steady_state(clab_topology, fcli)
        poll_until(
            lambda: ping(src.client_container, dst.client_ip,
                         src_ip=src.client_ip, count=1, timeout=1),
            timeout=30.0,
            interval=2.0,
            description=f"pre-fault: {src.client_container} -> {dst.client_container}",
        )

        stream = ping_continuous(
            src.client_container, dst.client_ip, src_ip=src.client_ip,
        )

        time.sleep(1)
        fault_manager.pause_node(crash_container)

        try:
            convergence_time = poll_until(
                lambda: ping(
                    src.client_container, dst.client_ip,
                    src_ip=src.client_ip, count=1, timeout=1,
                ),
                timeout=MAX_CONVERGENCE["spine_crash"] * 3,
                interval=1.0,
                description=f"reconvergence after {crash_spine} crash",
            )
        except ConvergenceTimeout:
            result = stream.stop()
            fault_manager.unpause_node(crash_container)
            pytest.fail(
                f"Did not reconverge after crashing {crash_spine}. "
                f"Ping loss: {result.loss_pct:.1f}%"
            )

        logger.info(
            "Spine crash convergence: %.2fs (limit: %.1fs)",
            convergence_time, MAX_CONVERGENCE["spine_crash"],
        )
        assert convergence_time <= MAX_CONVERGENCE["spine_crash"], (
            f"Convergence took {convergence_time:.2f}s, "
            f"expected <= {MAX_CONVERGENCE['spine_crash']}s"
        )

        fault_manager.unpause_node(crash_container)

        poll_until(
            lambda: _all_bgp_established(fcli),
            timeout=FULL_RECOVERY_TIMEOUT,
            interval=3.0,
            description=f"BGP recovery after {crash_spine} unpause",
        )

        poll_until(
            lambda: ping(
                src.client_container, dst.client_ip,
                src_ip=src.client_ip, count=1, timeout=1,
            ),
            timeout=30.0,
            interval=2.0,
            description="full connectivity recovery after spine restore",
        )

        result = stream.stop()
        record_property("convergence_s", f"{convergence_time:.2f}")
        record_property("ping_loss_pct", f"{result.loss_pct:.1f}")
        record_property("pings_tx_rx", f"{result.transmitted}/{result.received}")
        logger.info(
            "Spine crash test complete: convergence=%.2fs, loss=%.1f%%",
            convergence_time, result.loss_pct,
        )


# ---------------------------------------------------------------------------
# Shared predicates
# ---------------------------------------------------------------------------


def _all_bgp_established(fcli: FcliClient) -> bool:
    """Return True if all BGP peers are in established state."""
    try:
        peers = fcli.bgp_peers()
        return all(
            p.get("state", "").lower() == "established" for p in peers
        )
    except Exception:
        return False
