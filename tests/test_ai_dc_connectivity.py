"""
AI DC rail-optimized fabric validation.

Verifies that traffic is actually carried where the design says it should be,
on a live containerlab deployment of the rail-optimized AI DC:

  Backend (RoCEv2 IP fabric)
    - every GPU NIC is addressed inside the rail its leaf offers, with the
      VLAN tagging the leaf expects
    - each NIC reaches its rail gateway
    - rail peers on the same leaf reach each other (leaf-local routing)
    - rail peers on different leaves of a stripe reach each other (via the
      stripe spines)
    - same GPU rank in different stripes reaches each other (via the
      stripe-connector spines)
    - remote rail prefixes are installed with one path per spine, and traffic
      between rails is spread over all of them
    - PFC and ECN are in place so the fabric is lossless for RoCEv2

  Frontend (EVPN-VXLAN storage fabric)
    - GPU servers and storage nodes share one subnet in the storage bridge
      domain and reach each other in both directions
    - the bridge domain is learned on both leaves and stretched over VXLAN
    - every dual-homed LAG is all-active on the fabric side with both members
      collecting and distributing on the host side

Topology is discovered at runtime from the live fabric -- rail prefixes come
from the leaves themselves and the cabling from the .clab.yml -- so the suite
follows a resized fabric (more stripes, rails or servers) without edits.

Usage::

    uv run pytest tests/test_ai_dc_connectivity.py -m ai_dc \\
        --clab-topo validated-designs/ai-dc/rail-optimized/build/dc1.clab.yml
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import time
from collections import defaultdict

import pytest

from tests.conftest import ClabTopology
from tests.helpers.ai_dc import (
    AiDcFabric,
    RailAttachment,
    StorageAttachment,
    discover_ai_dc,
)
from tests.helpers.fcli import FcliClient
from tests.helpers.host import bond_members, host_interfaces
from tests.helpers.iperf import kill_server, start_server
from tests.helpers.ping import ping_retry, ping_size
from tests.helpers.srl_cli import SrlCliError, qos_state, routes

logger = logging.getLogger(__name__)

# IPv6 header (40) + ICMPv6 header (8); IPv4 equivalent is 20 + 8.
ICMP6_OVERHEAD = 48
ICMP4_OVERHEAD = 28

ROCEV2_PFC_PRIORITY = 3


# ---------------------------------------------------------------------------
# Fabric discovery (shared between collection and fixtures)
# ---------------------------------------------------------------------------


_fabric_cache: dict[str, AiDcFabric] = {}


def _fabric_for(config) -> AiDcFabric | None:
    """Discover the AI DC fabric, or return None when there is nothing to test."""
    topo_file = config.getoption("clab_topo", default=None)
    if not topo_file:
        return None
    key = str(topo_file)
    if key in _fabric_cache:
        return _fabric_cache[key]

    from tests.conftest import _discover_topology

    try:
        topo = _discover_topology(
            topo_file, gnmi_port=config.getoption("gnmi_port", default=None),
        )
        fcli = FcliClient(topo_path=topo.topo_path, gnmi_port=topo.gnmi_port)
        fabric = discover_ai_dc(topo, fcli)
    except Exception as exc:
        logger.warning("AI DC discovery failed: %s", exc)
        return None

    _fabric_cache[key] = fabric
    return fabric


@pytest.fixture(scope="session")
def ai_dc(request, clab_topology: ClabTopology) -> AiDcFabric:
    """The discovered AI DC fabric."""
    fabric = _fabric_for(request.config)
    if fabric is None or (not fabric.rails and not fabric.storage):
        pytest.skip("No AI DC rail-optimized fabric found in the running topology")
    return fabric


def _rail_id(rail: RailAttachment) -> str:
    return f"{rail.label}@{rail.leaf}[{rail.rail_id}]"


def _pair_id(a, b) -> str:
    return f"{a.label}->{b.label}"


def pytest_generate_tests(metafunc):
    """Parametrize every check from the live rail matrix and storage BD."""
    fabric = _fabric_for(metafunc.config)

    if "rail" in metafunc.fixturenames:
        params = (
            [pytest.param(r, id=_rail_id(r)) for r in fabric.rails] if fabric else []
        )
        metafunc.parametrize("rail", params)

    for fixture, builder in (
        ("same_leaf_pair", "same_leaf_pairs"),
        ("intra_stripe_pair", "intra_stripe_pairs"),
        ("cross_stripe_pair", "cross_stripe_rank_pairs"),
    ):
        if fixture in metafunc.fixturenames:
            pairs = getattr(fabric, builder)() if fabric else []
            metafunc.parametrize(
                fixture,
                [
                    pytest.param(
                        (a, b),
                        id=f"{_pair_id(a, b)}[{a.rail_id}->{b.rail_id}]",
                    )
                    for a, b in pairs
                ],
            )

    if "storage_member" in metafunc.fixturenames:
        params = (
            [pytest.param(s, id=s.label) for s in fabric.storage] if fabric else []
        )
        metafunc.parametrize("storage_member", params)

    for fixture, builder in (
        ("gpu_storage_pair", "gpu_to_storage_pairs"),
        ("storage_pair", "storage_mesh_pairs"),
        ("gpu_pair", "gpu_mesh_pairs"),
    ):
        if fixture in metafunc.fixturenames:
            pairs = getattr(fabric, builder)() if fabric else []
            metafunc.parametrize(
                fixture,
                [pytest.param((a, b), id=_pair_id(a, b)) for a, b in pairs],
            )

    if "backend_node" in metafunc.fixturenames:
        nodes = fabric.backend_leaves + fabric.backend_spines if fabric else []
        metafunc.parametrize("backend_node", [pytest.param(n, id=n) for n in nodes])

    if "backend_leaf" in metafunc.fixturenames:
        leaves = fabric.backend_leaves if fabric else []
        metafunc.parametrize("backend_leaf", [pytest.param(n, id=n) for n in leaves])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _rail_endpoint(rail: RailAttachment, record_property) -> str:
    """Resolve a rail NIC's address, skipping the test when the host is unconfigured."""
    address = rail.host_address()
    if not address:
        pytest.skip(
            f"{rail.label} has no address in {rail.prefix} "
            f"(see TestBackendRailAddressing)"
        )
    record_property(f"{rail.label}", f"{address} on {rail.rail_id} via {rail.leaf}")
    return address


def _storage_endpoint(member: StorageAttachment, record_property) -> str:
    address = member.host_address()
    if not address:
        pytest.skip(
            f"{member.label} has no storage address "
            f"(see TestStorageAddressing)"
        )
    record_property(f"{member.label}", f"{address} in {member.bridge_domain}")
    return address


def _assert_bidirectional(a_container, a_ip, b_container, b_ip, context: str) -> None:
    failures = []
    if not ping_retry(a_container, b_ip, src_ip=a_ip):
        failures.append(f"{a_container}({a_ip}) -> {b_container}({b_ip})")
    if not ping_retry(b_container, a_ip, src_ip=b_ip):
        failures.append(f"{b_container}({b_ip}) -> {a_container}({a_ip})")
    assert not failures, (
        f"{context} unreachable:\n" + "\n".join(f"  - {f}" for f in failures)
    )


# ---------------------------------------------------------------------------
# Backend: rail addressing
# ---------------------------------------------------------------------------


class TestBackendRailAddressing:
    """
    Each GPU NIC is attached to its rail the way the leaf expects.

    A rail is a routed IPv6 subnet on one leaf port.  If the server holds no
    address in that subnet, or holds it on an untagged interface while the leaf
    expects a VLAN tag, the NIC is silently off the fabric -- so this runs
    before any reachability test and explains the rest of the failures.
    """

    @pytest.mark.ai_dc
    def test_rail_nic_attached(self, ai_dc: AiDcFabric, rail: RailAttachment, record_property):
        record_property("rail", f"{rail.rail_id} {rail.prefix}")
        record_property("fabric_side", f"{rail.leaf} {rail.subinterface} ({rail.gateway})")
        record_property(
            "expected_host",
            f"{rail.expected_host_ip} on {rail.server_nic}"
            + (f" tagged vlan {rail.vlan}" if rail.vlan else " untagged"),
        )

        ifaces = host_interfaces(rail.server_container)
        assert rail.server_nic in ifaces, (
            f"{rail.label}: NIC {rail.server_nic} cabled to "
            f"{rail.leaf}:{rail.leaf_port} does not exist on the server"
        )

        iface = rail.host_interface()
        if iface is None:
            candidates = ", ".join(sorted(ifaces)) or "none"
            assert False, (
                f"{rail.label}: {rail.leaf} {rail.subinterface} expects VLAN "
                f"{rail.vlan} tagged frames, but the server has no VLAN "
                f"{rail.vlan} interface on {rail.server_nic}. "
                f"Existing interfaces: {candidates}"
            )

        record_property("host_side", f"{iface.name} mtu={iface.mtu} {iface.oper_state}")
        found = iface.addresses_in(rail.prefix)
        assert found, (
            f"{rail.label}: {iface.name} has no address in rail {rail.prefix} "
            f"(expected {rail.expected_host_ip}). "
            f"Configured: {', '.join(iface.addresses) or 'none'}"
        )
        assert iface.is_up, f"{rail.label}: {iface.name} is {iface.oper_state}, not up"

    @pytest.mark.ai_dc
    def test_rail_host_mtu_fits_fabric(
        self, ai_dc: AiDcFabric, rail: RailAttachment, record_property,
    ):
        """
        The host must not offer a larger MTU than the rail carries.

        A server sending 9000-byte frames into a 4136-byte rail loses exactly
        the large RDMA transfers the fabric was built for, while small packets
        keep working -- the failure mode this check exists to catch early.
        """
        iface = rail.host_interface()
        if iface is None:
            pytest.skip(f"{rail.label} has no interface on rail {rail.rail_id}")
        record_property("fabric_ip_mtu", f"{rail.ip_mtu}B")
        record_property("host_mtu", f"{iface.name} {iface.mtu}B")
        assert rail.ip_mtu > 0, f"{rail.leaf} {rail.subinterface} reports no ip-mtu"
        assert iface.mtu <= rail.ip_mtu, (
            f"{rail.label}: {iface.name} MTU {iface.mtu}B exceeds the rail "
            f"ip-mtu {rail.ip_mtu}B on {rail.leaf} {rail.subinterface}; "
            f"frames above {rail.ip_mtu}B will be dropped by the fabric"
        )


# ---------------------------------------------------------------------------
# Backend: rail reachability
# ---------------------------------------------------------------------------


class TestBackendRailReachability:
    """Rail traffic is carried leaf-local, across a stripe, and between stripes."""

    @pytest.mark.ai_dc
    def test_rail_gateway(self, ai_dc: AiDcFabric, rail: RailAttachment, record_property):
        """Each GPU NIC reaches the rail gateway on its leaf."""
        source = _rail_endpoint(rail, record_property)
        record_property("destination", f"{rail.gateway} ({rail.leaf} {rail.subinterface})")
        assert ping_retry(rail.server_container, rail.gateway, src_ip=source), (
            f"{rail.label} ({source}) cannot reach its rail gateway "
            f"{rail.gateway} on {rail.leaf} {rail.subinterface}"
        )

    @pytest.mark.ai_dc
    def test_same_leaf_rail_peers(
        self, ai_dc: AiDcFabric, same_leaf_pair, record_property,
    ):
        """Two GPUs on one rail leaf reach each other without leaving the leaf."""
        a, b = same_leaf_pair
        record_property("path", f"{a.leaf} (leaf-local routing in {a.vrf})")
        a_ip = _rail_endpoint(a, record_property)
        b_ip = _rail_endpoint(b, record_property)
        _assert_bidirectional(
            a.server_container, a_ip, b.server_container, b_ip,
            f"rail peers on {a.leaf}",
        )

    @pytest.mark.ai_dc
    def test_intra_stripe_rail_peers(
        self, ai_dc: AiDcFabric, intra_stripe_pair, record_property,
    ):
        """Different rails of one stripe reach each other through the spines."""
        a, b = intra_stripe_pair
        record_property(
            "path",
            f"{a.leaf} -> {', '.join(ai_dc.backend_spines) or 'spines'} -> {b.leaf}",
        )
        record_property("stripe", f"stripe {a.stripe}")
        a_ip = _rail_endpoint(a, record_property)
        b_ip = _rail_endpoint(b, record_property)
        _assert_bidirectional(
            a.server_container, a_ip, b.server_container, b_ip,
            f"rails {a.rail_id} and {b.rail_id} within stripe {a.stripe}",
        )

    @pytest.mark.ai_dc
    def test_cross_stripe_rank_peers(
        self, ai_dc: AiDcFabric, cross_stripe_pair, record_property,
    ):
        """The same GPU rank in different stripes reaches itself across the connector."""
        a, b = cross_stripe_pair
        record_property("gpu_rank", a.rank)
        record_property("stripes", f"{a.stripe} -> {b.stripe}")
        record_property(
            "path",
            f"{a.leaf} -> {', '.join(ai_dc.backend_spines) or 'spines'} -> {b.leaf}",
        )
        a_ip = _rail_endpoint(a, record_property)
        b_ip = _rail_endpoint(b, record_property)
        _assert_bidirectional(
            a.server_container, a_ip, b.server_container, b_ip,
            f"GPU rank {a.rank} between stripe {a.stripe} and stripe {b.stripe}",
        )

    @pytest.mark.ai_dc
    def test_rail_jumbo_frames(
        self, ai_dc: AiDcFabric, cross_stripe_pair, record_property,
    ):
        """The full rail MTU survives the cross-stripe path, unfragmented."""
        a, b = cross_stripe_pair
        a_ip = _rail_endpoint(a, record_property)
        b_ip = _rail_endpoint(b, record_property)

        a_iface, b_iface = a.host_interface(), b.host_interface()
        host_mtu = min(i.mtu for i in (a_iface, b_iface) if i and i.mtu)
        mtu = min(host_mtu, a.ip_mtu, b.ip_mtu)
        payload = mtu - ICMP6_OVERHEAD

        record_property("probe", f"{payload}B payload = {mtu}B frame, DF set")
        record_property("rail_ip_mtu", f"{a.ip_mtu}B / {b.ip_mtu}B")
        assert ping_size(a.server_container, b_ip, payload, src_ip=a_ip), (
            f"{mtu}B frames do not survive {a.label} -> {b.label} "
            f"(rail ip-mtu {a.ip_mtu}B/{b.ip_mtu}B, host MTU {host_mtu}B); "
            f"large RDMA transfers will be dropped"
        )


# ---------------------------------------------------------------------------
# Backend: ECMP
# ---------------------------------------------------------------------------


class TestBackendEcmp:
    """Rail traffic between leaves uses every spine, not just one."""

    @pytest.mark.ai_dc
    def test_remote_rails_use_all_spines(
        self, ai_dc: AiDcFabric, backend_leaf: str, record_property,
    ):
        """
        Every learned rail prefix has one next-hop per spine.

        With a non-blocking backend, a rail prefix installed with fewer paths
        than there are spines means the fabric will bottleneck an all-reduce
        onto a single uplink even though the capacity is cabled.
        """
        expected = len(ai_dc.backend_spines)
        if expected < 2:
            pytest.skip("Need at least 2 backend spines to verify ECMP")

        try:
            table = routes(ai_dc.container_of(backend_leaf), ai_dc.rail_vrf)
        except SrlCliError as exc:
            pytest.fail(f"Cannot read {ai_dc.rail_vrf} route table on {backend_leaf}: {exc}")

        learned = [r for r in table if r.route_type == "bgp" and r.active]
        record_property("spines", f"{expected} ({', '.join(ai_dc.backend_spines)})")
        record_property("learned_rail_prefixes", len(learned))
        assert learned, (
            f"{backend_leaf} has no learned rail prefixes in {ai_dc.rail_vrf}; "
            f"remote rails are unreachable"
        )

        underpaved = [
            f"{r.prefix}: {r.ecmp_paths} path(s) via {', '.join(r.next_hop_interfaces)}"
            for r in learned if r.ecmp_paths < expected
        ]
        record_property("prefixes_below_spine_count", len(underpaved))
        assert not underpaved, (
            f"{len(underpaved)}/{len(learned)} rail prefix(es) on {backend_leaf} "
            f"install fewer than {expected} paths:\n"
            + "\n".join(f"  - {u}" for u in underpaved)
        )

    @pytest.mark.ai_dc
    @pytest.mark.slow
    @pytest.mark.timeout(300)
    def test_rail_traffic_spread_over_spines(
        self, ai_dc: AiDcFabric, fcli: FcliClient, record_property,
    ):
        """
        Cross-stripe rail flows show up on every spine.

        Only the distribution matters -- containerised SR Linux caps
        throughput, so absolute rates are meaningless.
        """
        if len(ai_dc.backend_spines) < 2:
            pytest.skip("Need at least 2 backend spines to verify ECMP")

        flows = _select_flows(ai_dc.cross_stripe_rank_pairs())
        if len(flows) < 2:
            pytest.skip("Need at least 2 cross-stripe rail pairs to spread traffic")

        endpoints = []
        for a, b in flows:
            a_ip, b_ip = a.host_address(), b.host_address()
            if a_ip and b_ip:
                endpoints.append((a, a_ip, b, b_ip))
        if len(endpoints) < 2:
            pytest.skip("Not enough addressed rail pairs to generate flows")

        record_property("flows", len(endpoints))
        record_property("spines", ", ".join(ai_dc.backend_spines))

        baseline = _spine_ifstats(fcli, ai_dc.backend_spines)
        active = _run_flows(endpoints, fcli, ai_dc.backend_spines)

        busy = _spines_with_traffic(baseline, active, ai_dc.backend_spines)
        for spine in ai_dc.backend_spines:
            intfs = busy.get(spine, [])
            record_property(
                f"{spine}_active_interfaces",
                f"{len(intfs)} ({', '.join(intfs)})" if intfs else "0",
            )
        assert len(busy) == len(ai_dc.backend_spines), (
            f"Only {len(busy)}/{len(ai_dc.backend_spines)} spine(s) carried rail "
            f"traffic: {dict(busy) or 'none'}. Rail traffic is not spread over "
            f"all ECMP paths"
        )


# ---------------------------------------------------------------------------
# Backend: RoCEv2 losslessness
# ---------------------------------------------------------------------------


class TestBackendRoceV2:
    """PFC and ECN are configured so RoCEv2 traffic is not dropped."""

    @pytest.mark.ai_dc
    def test_pfc_enabled_on_fabric_and_gpu_ports(
        self, ai_dc: AiDcFabric, backend_node: str, record_property,
    ):
        container = ai_dc.container_of(backend_node)
        try:
            qos = qos_state(container)
        except SrlCliError as exc:
            pytest.fail(f"Cannot read /qos on {backend_node}: {exc}")

        physical = {
            name: iface for name, iface in qos.interfaces.items()
            if "." not in name
        }
        record_property("qos_interfaces", ", ".join(sorted(physical)) or "none")
        assert physical, f"{backend_node} has no interfaces under /qos"

        without_pfc = sorted(
            name for name, iface in physical.items() if not iface.pfc_enabled
        )
        assert not without_pfc, (
            f"{backend_node}: PFC is not enabled on {', '.join(without_pfc)}; "
            f"RoCEv2 traffic on these ports is lossy"
        )

        profiles = {i.pfc_mapping_profile for i in physical.values()}
        record_property("pfc_mapping_profiles", ", ".join(sorted(profiles)))
        priorities = {
            p for profile in profiles for p in qos.pfc_priorities.get(profile, set())
        }
        record_property("pfc_priorities", ", ".join(str(p) for p in sorted(priorities)))
        assert ROCEV2_PFC_PRIORITY in priorities, (
            f"{backend_node}: PFC priority {ROCEV2_PFC_PRIORITY} (RoCEv2) is not "
            f"enabled in {', '.join(sorted(profiles))}; enabled: {sorted(priorities)}"
        )

    @pytest.mark.ai_dc
    def test_pfc_deadlock_detection(
        self, ai_dc: AiDcFabric, backend_node: str, record_property,
    ):
        """PFC deadlock detection and recovery are armed on every node."""
        container = ai_dc.container_of(backend_node)
        try:
            qos = qos_state(container)
        except SrlCliError as exc:
            pytest.fail(f"Cannot read /qos on {backend_node}: {exc}")

        profiles = {
            i.pfc_mapping_profile for i in qos.interfaces.values()
            if i.pfc_mapping_profile
        }
        missing = sorted(p for p in profiles if p not in qos.deadlock_timers)
        record_property(
            "deadlock_timers",
            "; ".join(
                f"{p}: detect={d}ms recover={r}ms"
                for p, (d, r) in sorted(qos.deadlock_timers.items())
            ) or "none",
        )
        assert profiles, f"{backend_node} has no PFC mapping profile bound"
        assert not missing, (
            f"{backend_node}: PFC deadlock detection is disabled in "
            f"{', '.join(missing)}; a PFC storm would not self-clear"
        )
        unarmed = sorted(
            p for p, (detect, recover) in qos.deadlock_timers.items()
            if detect <= 0 or recover <= 0
        )
        assert not unarmed, (
            f"{backend_node}: deadlock timers are zero in {', '.join(unarmed)}"
        )

    @pytest.mark.ai_dc
    def test_ecn_marking_on_rocev2_queue(
        self, ai_dc: AiDcFabric, backend_node: str, record_property,
    ):
        """
        The RoCEv2 egress queue marks ECN instead of dropping.

        ECN marking is the congestion signal DCQCN uses to throttle senders;
        without it the fabric can only respond to congestion by dropping, and
        an RDMA transfer that loses a packet restarts.
        """
        container = ai_dc.container_of(backend_node)
        try:
            qos = qos_state(container)
        except SrlCliError as exc:
            pytest.fail(f"Cannot read /qos on {backend_node}: {exc}")

        bound = {
            (name, queue, profile)
            for name, iface in qos.interfaces.items()
            for queue, profile in iface.queue_management_profiles.items()
            if profile
        }
        record_property(
            "queue_profiles",
            "; ".join(f"{n} {q} -> {p}" for n, q, p in sorted(bound)) or "none",
        )
        assert bound, (
            f"{backend_node}: no egress queue has a queue-management-profile; "
            f"ECN marking is not configured"
        )

        failures = []
        for name, queue, profile in sorted(bound):
            slopes = qos.ecn_slopes.get(profile, [])
            marking = [s for s in slopes if s.ecn_enabled]
            if not marking:
                failures.append(f"{name} {queue} -> {profile}: ECN not enabled")
                continue
            for slope in marking:
                record_property(
                    f"ecn_{profile}",
                    f"min={slope.min_threshold_percent}% "
                    f"max={slope.max_threshold_percent}% "
                    f"drop={slope.max_drop_probability_percent}%",
                )
                if not 0 < slope.min_threshold_percent < slope.max_threshold_percent <= 100:
                    failures.append(
                        f"{profile}: implausible ECN thresholds "
                        f"{slope.min_threshold_percent}%-{slope.max_threshold_percent}%"
                    )
        assert not failures, (
            f"{backend_node}: ECN marking is not usable:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


# ---------------------------------------------------------------------------
# Frontend: storage bridge domain
# ---------------------------------------------------------------------------


class TestStorageAddressing:
    """GPU servers and storage nodes share one subnet on the storage BD."""

    @pytest.mark.ai_dc
    def test_storage_member_attached(
        self, ai_dc: AiDcFabric, storage_member: StorageAttachment, record_property,
    ):
        record_property("bridge_domain", storage_member.bridge_domain)
        record_property(
            "fabric_side",
            f"{', '.join(storage_member.lags)} "
            + (f"tagged vlan {storage_member.vlan}" if storage_member.tagged else "untagged"),
        )
        record_property("nics", ", ".join(storage_member.nics))

        iface = storage_member.host_interface()
        if iface is None:
            ifaces = ", ".join(sorted(host_interfaces(storage_member.server_container)))
            assert False, (
                f"{storage_member.label}: the fabric expects "
                + (
                    f"VLAN {storage_member.vlan} tagged frames"
                    if storage_member.tagged else "untagged frames"
                )
                + f" on {', '.join(storage_member.nics)}, but no such interface "
                f"exists. Existing interfaces: {ifaces}"
            )

        record_property("host_side", f"{iface.name} mtu={iface.mtu} {iface.oper_state}")
        addresses = iface.global_addresses("inet")
        assert addresses, (
            f"{storage_member.label}: {iface.name} has no IPv4 address for the "
            f"{storage_member.bridge_domain} bridge domain"
        )
        assert iface.is_up, (
            f"{storage_member.label}: {iface.name} is {iface.oper_state}, not up"
        )

    @pytest.mark.ai_dc
    def test_storage_members_share_one_subnet(self, ai_dc: AiDcFabric, record_property):
        """
        Every member of the bridge domain is in the same subnet.

        The storage BD has no IRB, so the servers are each other's only
        gateway: a member in a different subnet is unreachable even though the
        fabric bridges its frames correctly.
        """
        subnets: dict[str, list[str]] = defaultdict(list)
        unaddressed = []
        for member in ai_dc.storage:
            iface = member.host_interface()
            addresses = iface.global_addresses("inet") if iface else []
            if not addresses:
                unaddressed.append(member.label)
                continue
            for cidr in addresses:
                subnets[str(ipaddress.ip_network(cidr, strict=False))].append(
                    f"{member.label}({cidr})"
                )

        record_property("members", len(ai_dc.storage))
        for subnet, members in sorted(subnets.items()):
            record_property(subnet, ", ".join(sorted(members)))

        assert not unaddressed, (
            f"No storage address on: {', '.join(sorted(unaddressed))}"
        )
        assert len(subnets) == 1, (
            f"Storage bridge domain members are split across {len(subnets)} "
            f"subnets; they cannot reach each other:\n"
            + "\n".join(
                f"  - {subnet}: {', '.join(sorted(members))}"
                for subnet, members in sorted(subnets.items())
            )
        )


class TestStorageReachability:
    """Traffic is carried between GPU servers and storage nodes."""

    @pytest.mark.ai_dc
    def test_gpu_to_storage(
        self, ai_dc: AiDcFabric, gpu_storage_pair, record_property,
    ):
        """A GPU server reaches a storage node across the storage BD."""
        gpu, node = gpu_storage_pair
        record_property("bridge_domain", gpu.bridge_domain)
        record_property("encapsulation", f"vlan {gpu.vlan} tagged -> untagged")
        gpu_ip = _storage_endpoint(gpu, record_property)
        node_ip = _storage_endpoint(node, record_property)
        _assert_bidirectional(
            gpu.server_container, gpu_ip, node.server_container, node_ip,
            f"{gpu.label} and {node.label} in {gpu.bridge_domain}",
        )

    @pytest.mark.ai_dc
    def test_storage_node_mesh(self, ai_dc: AiDcFabric, storage_pair, record_property):
        """Storage nodes reach each other across the storage BD."""
        a, b = storage_pair
        record_property("bridge_domain", a.bridge_domain)
        a_ip = _storage_endpoint(a, record_property)
        b_ip = _storage_endpoint(b, record_property)
        _assert_bidirectional(
            a.server_container, a_ip, b.server_container, b_ip,
            f"{a.label} and {b.label} in {a.bridge_domain}",
        )

    @pytest.mark.ai_dc
    def test_gpu_mesh(self, ai_dc: AiDcFabric, gpu_pair, record_property):
        """GPU servers reach each other on the storage BD."""
        a, b = gpu_pair
        record_property("bridge_domain", a.bridge_domain)
        a_ip = _storage_endpoint(a, record_property)
        b_ip = _storage_endpoint(b, record_property)
        _assert_bidirectional(
            a.server_container, a_ip, b.server_container, b_ip,
            f"{a.label} and {b.label} in {a.bridge_domain}",
        )

    @pytest.mark.ai_dc
    def test_storage_jumbo_frames(
        self, ai_dc: AiDcFabric, gpu_storage_pair, record_property,
    ):
        """The host MTU survives the storage path unfragmented."""
        gpu, node = gpu_storage_pair
        gpu_ip = _storage_endpoint(gpu, record_property)
        node_ip = _storage_endpoint(node, record_property)

        gpu_iface, node_iface = gpu.host_interface(), node.host_interface()
        host_mtu = min(i.mtu for i in (gpu_iface, node_iface) if i and i.mtu)
        fabric_mtu = min(m for m in (gpu.l2_mtu, node.l2_mtu) if m) or host_mtu
        payload = host_mtu - ICMP4_OVERHEAD

        record_property("probe", f"{payload}B payload = {host_mtu}B frame, DF set")
        record_property("fabric_l2_mtu", f"{fabric_mtu}B")
        assert host_mtu <= fabric_mtu, (
            f"Host MTU {host_mtu}B exceeds the bridge domain MTU {fabric_mtu}B"
        )
        assert ping_size(gpu.server_container, node_ip, payload, src_ip=gpu_ip), (
            f"{host_mtu}B frames do not survive {gpu.label} -> {node.label} "
            f"(bridge domain MTU {fabric_mtu}B)"
        )


class TestStorageRedundancy:
    """The storage fabric is all-active and the overlay is stretched."""

    @pytest.mark.ai_dc
    def test_dual_homed_to_both_leaves(
        self, ai_dc: AiDcFabric, storage_member: StorageAttachment, record_property,
    ):
        record_property("leaves", ", ".join(storage_member.leaves))
        record_property("subinterfaces", ", ".join(storage_member.subinterfaces))
        assert len(storage_member.leaves) >= 2, (
            f"{storage_member.label} is attached to only "
            f"{', '.join(storage_member.leaves) or 'no leaf'}; losing that leaf "
            f"takes it off the storage network"
        )

    @pytest.mark.ai_dc
    def test_ethernet_segments_all_active(
        self, ai_dc: AiDcFabric, fcli: FcliClient, record_property,
    ):
        """Every dual-homed LAG is an operational all-active ethernet segment."""
        segments = fcli.ethernet_segments()
        record_property("ethernet_segments", len(segments))
        expected_lags = {lag for s in ai_dc.storage for lag in s.lags}
        record_property("dual_homed_lags", len(expected_lags))
        assert segments, "No ethernet segments found despite dual-homed LAGs"

        problems = []
        for segment in segments:
            node = segment.get("Node", "?")
            name = segment.get("name", "?")
            if str(segment.get("oper", "")).lower() != "up":
                problems.append(f"{node}/{name}: oper={segment.get('oper')}")
            if str(segment.get("mh-mode", "")) != "all-active":
                problems.append(f"{node}/{name}: mh-mode={segment.get('mh-mode')}")
            if not segment.get("ni-peers"):
                problems.append(f"{node}/{name}: no ethernet-segment peer")
        assert not problems, (
            f"{len(problems)} ethernet segment problem(s):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    @pytest.mark.ai_dc
    def test_host_bond_members_active(
        self, ai_dc: AiDcFabric, storage_member: StorageAttachment, record_property,
    ):
        """
        Both bond members are collecting and distributing on the host.

        A LACP bond that fell back to one member still passes every ping while
        silently halving storage bandwidth and losing leaf redundancy.
        """
        iface = storage_member.host_interface()
        if iface is None:
            pytest.skip(f"{storage_member.label} has no storage interface")

        ifaces = host_interfaces(storage_member.server_container)
        bond = ifaces.get(iface.lower) if iface.is_vlan else iface
        if bond is None or bond.kind != "bond":
            pytest.skip(f"{storage_member.label} storage interface is not a bond")

        members = bond_members(ifaces, bond.name)
        record_property(
            "bond_members",
            ", ".join(f"{m.name}({m.slave_state or 'unknown'})" for m in members) or "none",
        )
        assert len(members) == len(storage_member.nics), (
            f"{storage_member.label}: {bond.name} has {len(members)} member(s) but "
            f"{len(storage_member.nics)} NIC(s) are cabled to "
            f"{', '.join(storage_member.leaves)}"
        )
        inactive = [m.name for m in members if m.slave_state.upper() != "ACTIVE"]
        assert not inactive, (
            f"{storage_member.label}: {bond.name} member(s) {', '.join(inactive)} "
            f"are not active in the LACP aggregation"
        )

    @pytest.mark.ai_dc
    def test_storage_macs_learned_on_every_leaf(
        self, ai_dc: AiDcFabric, fcli: FcliClient, record_property,
    ):
        """
        Every member's MAC is known on every frontend leaf.

        A member missing from a leaf's bridge table is reachable only through
        flooding, so it works until the flood is suppressed and then stops --
        the failure that a ping sweep alone will not surface.
        """
        if not ai_dc.storage_bd:
            pytest.skip("No storage bridge domain discovered")

        entries = fcli.mac_table(network_instance=ai_dc.storage_bd)
        known: dict[str, dict[str, str]] = defaultdict(dict)
        for entry in entries:
            node = str(entry.get("Node", ""))
            mac = str(entry.get("Address", "")).lower()
            known[node][mac] = str(entry.get("Dest", ""))

        record_property("bridge_domain", ai_dc.storage_bd)
        record_property("mac_entries", len(entries))
        assert entries, f"No MAC entries in {ai_dc.storage_bd}"

        missing = []
        for member in ai_dc.storage:
            iface = member.host_interface()
            if iface is None or not iface.mac:
                continue
            record_property(f"{member.label}_mac", iface.mac)
            for leaf in ai_dc.frontend_leaves:
                if iface.mac not in known.get(leaf, {}):
                    missing.append(f"{member.label} ({iface.mac}) unknown on {leaf}")

        for leaf in ai_dc.frontend_leaves:
            record_property(f"{leaf}_macs", len(known.get(leaf, {})))

        assert not missing, (
            f"{len(missing)} member MAC(s) not in the {ai_dc.storage_bd} bridge "
            f"table:\n" + "\n".join(f"  - {m}" for m in missing)
        )

    @pytest.mark.ai_dc
    def test_storage_vxlan_stretched(
        self, ai_dc: AiDcFabric, fcli: FcliClient, record_property,
    ):
        """Every frontend leaf has a VXLAN tunnel to its peers."""
        missing = []
        for leaf in ai_dc.frontend_leaves:
            tunnels = fcli.vxlan_tunnels(node=leaf)
            record_property(f"{leaf}_vxlan_tunnels", len(tunnels))
            if not tunnels:
                missing.append(leaf)
        assert ai_dc.frontend_leaves, "No frontend leaves discovered"
        assert not missing, (
            f"No VXLAN tunnel on: {', '.join(missing)}; the storage bridge domain "
            f"is not stretched between the frontend leaves"
        )


# ---------------------------------------------------------------------------
# Fabric-wide health
# ---------------------------------------------------------------------------


class TestFabricHealth:
    """Both fabrics are fully converged and forwarding cleanly."""

    @pytest.mark.ai_dc
    def test_bgp_established(self, ai_dc: AiDcFabric, fcli: FcliClient, record_property):
        peers = fcli.bgp_peers()
        record_property("bgp_peers", len(peers))
        down = [p for p in peers if str(p.get("state", "")).lower() != "established"]
        record_property("peers_not_established", len(down))
        assert peers, "No BGP peers found on any node"
        assert not down, (
            f"{len(down)}/{len(peers)} BGP peer(s) not established:\n"
            + "\n".join(
                f"  - {p.get('Node', '?')}: {p.get('1_peer', '?')} "
                f"state={p.get('state', '?')}"
                for p in down
            )
        )

    @pytest.mark.ai_dc
    def test_rail_subinterface_up(
        self, ai_dc: AiDcFabric, rail: RailAttachment, record_property,
    ):
        """The leaf side of every rail is operationally up."""
        record_property("subinterface", f"{rail.leaf} {rail.subinterface}")
        record_property("rail", f"{rail.rail_id} {rail.prefix}")
        assert rail.oper_up, (
            f"{rail.leaf} {rail.subinterface} (rail {rail.rail_id} towards "
            f"{rail.label}) is not operationally up"
        )

    @pytest.mark.ai_dc
    def test_no_interface_discards(
        self, ai_dc: AiDcFabric, fcli: FcliClient, record_property,
    ):
        """
        No fabric interface is discarding packets.

        On a RoCEv2 fabric a discard is not a statistic, it is a stalled
        collective: the transfer that lost the packet has to start over.
        """
        stats = fcli.ifstats(interval=3)
        discards = [
            f"{e.get('Node', '?')}/{e.get('interface', '?')}: "
            f"in-disc={e.get('in-disc', 0)} out-disc={e.get('out-disc', 0)}"
            for e in stats
            if e.get("interface") != "mgmt0"
            and (e.get("in-disc", 0) or e.get("out-disc", 0))
        ]
        record_property("interfaces_checked", len(stats))
        record_property("interfaces_with_discards", len(discards))
        assert not discards, (
            f"{len(discards)} interface(s) discarding packets:\n"
            + "\n".join(f"  - {d}" for d in discards)
        )


# ---------------------------------------------------------------------------
# Traffic generation helpers
# ---------------------------------------------------------------------------


def _select_flows(pairs, limit: int = 4):
    """Pick a spread of pairs across distinct servers and rails."""
    chosen = []
    seen_rails: set[tuple[str, str]] = set()
    for a, b in pairs:
        key = (a.rail_id, b.rail_id)
        if key in seen_rails:
            continue
        seen_rails.add(key)
        chosen.append((a, b))
        if len(chosen) >= limit:
            break
    return chosen


def _run_flows(endpoints, fcli: FcliClient, spines: list[str]):
    """Run bidirectional iperf3 flows and sample spine counters while they run."""
    servers: list[tuple[str, int]] = []
    procs: list[subprocess.Popen] = []
    port = 9300
    duration = 12

    try:
        for a, a_ip, b, b_ip in endpoints:
            start_server(b.server_container, bind_ip=b_ip, port=port)
            servers.append((b.server_container, port))
            start_server(a.server_container, bind_ip=a_ip, port=port + 100)
            servers.append((a.server_container, port + 100))
            port += 1

        port = 9300
        for a, a_ip, b, b_ip in endpoints:
            for src, src_ip, dst_ip, dst_port in (
                (a, a_ip, b_ip, port),
                (b, b_ip, a_ip, port + 100),
            ):
                procs.append(subprocess.Popen(
                    [
                        "docker", "exec", src.server_container,
                        "iperf3", "-c", dst_ip, "-B", src_ip,
                        "-p", str(dst_port), "-t", str(duration), "-P", "4",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
                logger.info(
                    "Flow %s(%s) -> %s port %d", src.label, src_ip, dst_ip, dst_port,
                )
            port += 1

        time.sleep(3)
        active = _spine_ifstats(fcli, spines)

        for proc in procs:
            try:
                proc.wait(timeout=duration + 15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return active
    finally:
        for container, srv_port in servers:
            kill_server(container, srv_port)


def _spine_ifstats(fcli: FcliClient, spines: list[str]) -> dict[str, dict[str, dict]]:
    stats: dict[str, dict[str, dict]] = {}
    for spine in spines:
        stats[spine] = {
            str(entry.get("interface", "")): entry
            for entry in fcli.ifstats(node=spine, interval=5)
        }
    return stats


def _spines_with_traffic(
    baseline: dict[str, dict[str, dict]],
    active: dict[str, dict[str, dict]],
    spines: list[str],
    threshold_kbps: float = 5.0,
) -> dict[str, list[str]]:
    busy: dict[str, list[str]] = {}
    for spine in spines:
        interfaces = []
        for intf, stats in active.get(spine, {}).items():
            if intf == "mgmt0":
                continue
            base = baseline.get(spine, {}).get(intf, {})
            delta = (
                stats.get("in-Kbps", 0) + stats.get("out-Kbps", 0)
                - base.get("in-Kbps", 0) - base.get("out-Kbps", 0)
            )
            if delta > threshold_kbps:
                interfaces.append(intf)
        if interfaces:
            busy[spine] = sorted(interfaces)
    return busy
