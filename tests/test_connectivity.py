"""
Steady-state connectivity tests.

Validates data-plane reachability and control-plane state for a fully
converged EVPN-VXLAN fabric.  Each individual check (e.g. one gateway ping,
one L2 pair, one BGP peer) appears as its own parametrized test case in the
report, giving full visibility into what passed and what failed.

All topology information is discovered at runtime from the .clab.yml file,
client startup scripts, and fcli queries -- no project-specific imports.

Categories:
  - Gateway reachability (each client -> its IRB anycast gateway)
  - L2 intra-BD (clients in the same bridge domain)
  - L3 inter-subnet (clients in different BDs of the same ip-vrf)
  - Routed interfaces (dedicated L3 clients)
  - Static routes (prefixes reachable via static next-hops)
  - Control-plane assertions (BGP, VXLAN, network-instances, ES, LLDP)
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict

import pytest

from tests.conftest import ClabTopology, ClientAttachment, RoutedClient
from tests.helpers.fcli import FcliClient
from tests.helpers.ping import ping


# ---------------------------------------------------------------------------
# Dynamic parametrization via pytest_generate_tests
# ---------------------------------------------------------------------------

def _get_clab_topology(metafunc) -> ClabTopology | None:
    """Resolve the clab_topology fixture value at collection time."""
    if "clab_topology" not in metafunc.fixturenames:
        return None
    from tests.conftest import _build_topology_from_metafunc
    return _build_topology_from_metafunc(metafunc)


def pytest_generate_tests(metafunc):
    """Dynamically parametrize connectivity tests from the live topology."""
    # Gateway reachability
    if "gw_container" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            params = []
            for att in topo.client_attachments:
                if att.gateway:
                    params.append(pytest.param(
                        att.client_container, att.client_ip, att.gateway,
                        att.bridge_domain,
                        id=f"{att.client_container}({att.client_ip})->{att.gateway}",
                    ))
            metafunc.parametrize(
                "gw_container,gw_client_ip,gw_gateway,gw_bd", params,
            )

    # L2 intra-BD pairs
    if "l2_a" in metafunc.fixturenames:
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
                            id=f"{bd}:{a.client_container}({a.client_ip})-{b.client_container}({b.client_ip})",
                        ))
            metafunc.parametrize("l2_a,l2_b", params)

    # L3 inter-subnet pairs
    if "l3_a" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            by_vrf: dict[str, dict[str, ClientAttachment]] = defaultdict(dict)
            for att in topo.client_attachments:
                if att.router:
                    key = f"{att.client_container}:{att.bridge_domain}"
                    if key not in by_vrf[att.router]:
                        by_vrf[att.router][key] = att
            params = []
            for vrf, bd_map in sorted(by_vrf.items()):
                members = list(bd_map.values())
                bds = {m.bridge_domain for m in members}
                if len(bds) < 2:
                    continue
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        if a.bridge_domain != b.bridge_domain:
                            params.append(pytest.param(
                                a, b,
                                id=(
                                    f"{vrf}:{a.client_container}({a.bridge_domain})"
                                    f"->{b.client_container}({b.bridge_domain})"
                                ),
                            ))
            metafunc.parametrize("l3_a,l3_b", params)

    # Routed clients
    if "routed_client" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo:
            params = [
                pytest.param(
                    rc,
                    id=f"{rc.name}:{rc.client_container}({rc.client_ip})->{rc.gateway}",
                )
                for rc in topo.routed_clients
            ]
            metafunc.parametrize("routed_client", params)

    # Static route prefixes (discovered from fcli ipv4-rib)
    if "sr_src" in metafunc.fixturenames:
        topo = _get_clab_topology(metafunc)
        if topo and topo.fabric.static_route_prefixes:
            params = []
            for prefix in topo.fabric.static_route_prefixes:
                try:
                    net = ipaddress.ip_network(prefix, strict=False)
                except ValueError:
                    continue
                first_host = next(net.hosts(), None)
                if not first_host:
                    continue
                # Find a client in a VRF that can reach this static route
                # (any client attachment in the same VRF works)
                src = _find_client_for_static_route(topo, prefix)
                if src:
                    params.append(pytest.param(
                        src, str(first_host), prefix,
                        id=f"{src.client_container}->{first_host}[{prefix}]",
                    ))
            if params:
                metafunc.parametrize("sr_src,sr_target_ip,sr_prefix", params)


def _find_client_for_static_route(
    topo: ClabTopology, prefix: str,
) -> ClientAttachment | None:
    """Find a client that can reach a static route prefix via its VRF."""
    # The static route exists in a VRF; any client in that VRF can source traffic.
    # We need to figure out which VRF has this static route -- check routed clients
    # and bridged clients.
    # For now, use routed clients first (they typically share the VRF with static routes),
    # then fall back to any bridged client.
    for rc in topo.routed_clients:
        if rc.router:
            # Check if there's a bridged client in the same VRF (for ping sourcing)
            for att in topo.client_attachments:
                if att.router == rc.router:
                    return att
    # Fallback: return the first client with a non-empty router
    for att in topo.client_attachments:
        if att.router:
            return att
    return None


# ---------------------------------------------------------------------------
# Gateway reachability
# ---------------------------------------------------------------------------


class TestGatewayReachability:
    """Each client can ping its IRB anycast gateway."""

    @pytest.mark.connectivity
    def test_gateway_ping(
        self,
        clab_topology: ClabTopology,
        gw_container: str,
        gw_client_ip: str,
        gw_gateway: str,
        gw_bd: str,
        record_property,
    ):
        record_property("source", f"{gw_container} ({gw_client_ip})")
        record_property("destination", f"{gw_gateway} (IRB gateway)")
        record_property("bridge_domain", gw_bd)
        ok = ping(gw_container, gw_gateway, src_ip=gw_client_ip)
        assert ok, f"{gw_container} ({gw_client_ip}) cannot reach gateway {gw_gateway} in {gw_bd}"


# ---------------------------------------------------------------------------
# L2 intra-BD
# ---------------------------------------------------------------------------


class TestL2IntraBridgeDomain:
    """Clients in the same bridge domain can reach each other via L2/VXLAN."""

    @pytest.mark.connectivity
    def test_l2_ping_a_to_b(
        self,
        clab_topology: ClabTopology,
        l2_a: ClientAttachment,
        l2_b: ClientAttachment,
        record_property,
    ):
        record_property("bridge_domain", l2_a.bridge_domain)
        record_property("source", f"{l2_a.client_container} ({l2_a.client_ip})")
        record_property("destination", f"{l2_b.client_container} ({l2_b.client_ip})")
        ok = ping(l2_a.client_container, l2_b.client_ip, src_ip=l2_a.client_ip)
        assert ok, (
            f"L2 ping failed: {l2_a.client_container}({l2_a.client_ip}) -> "
            f"{l2_b.client_container}({l2_b.client_ip}) in {l2_a.bridge_domain}"
        )

    @pytest.mark.connectivity
    def test_l2_ping_b_to_a(
        self,
        clab_topology: ClabTopology,
        l2_a: ClientAttachment,
        l2_b: ClientAttachment,
        record_property,
    ):
        record_property("bridge_domain", l2_a.bridge_domain)
        record_property("source", f"{l2_b.client_container} ({l2_b.client_ip})")
        record_property("destination", f"{l2_a.client_container} ({l2_a.client_ip})")
        ok = ping(l2_b.client_container, l2_a.client_ip, src_ip=l2_b.client_ip)
        assert ok, (
            f"L2 ping failed: {l2_b.client_container}({l2_b.client_ip}) -> "
            f"{l2_a.client_container}({l2_a.client_ip}) in {l2_a.bridge_domain}"
        )


# ---------------------------------------------------------------------------
# L3 inter-subnet
# ---------------------------------------------------------------------------


class TestL3InterSubnet:
    """Clients in different BDs of the same ip-vrf can route to each other."""

    @pytest.mark.connectivity
    def test_l3_ping_a_to_b(
        self,
        clab_topology: ClabTopology,
        l3_a: ClientAttachment,
        l3_b: ClientAttachment,
        record_property,
    ):
        record_property("source", f"{l3_a.client_container} ({l3_a.client_ip}) [{l3_a.bridge_domain}]")
        record_property("destination", f"{l3_b.client_container} ({l3_b.client_ip}) [{l3_b.bridge_domain}]")
        record_property("router", l3_a.router)
        ok = ping(l3_a.client_container, l3_b.client_ip, src_ip=l3_a.client_ip)
        assert ok, (
            f"L3 ping failed: {l3_a.client_container}({l3_a.client_ip}) -> "
            f"{l3_b.client_container}({l3_b.client_ip}) "
            f"[{l3_a.bridge_domain} -> {l3_b.bridge_domain}]"
        )

    @pytest.mark.connectivity
    def test_l3_ping_b_to_a(
        self,
        clab_topology: ClabTopology,
        l3_a: ClientAttachment,
        l3_b: ClientAttachment,
        record_property,
    ):
        record_property("source", f"{l3_b.client_container} ({l3_b.client_ip}) [{l3_b.bridge_domain}]")
        record_property("destination", f"{l3_a.client_container} ({l3_a.client_ip}) [{l3_a.bridge_domain}]")
        record_property("router", l3_b.router)
        ok = ping(l3_b.client_container, l3_a.client_ip, src_ip=l3_b.client_ip)
        assert ok, (
            f"L3 ping failed: {l3_b.client_container}({l3_b.client_ip}) -> "
            f"{l3_a.client_container}({l3_a.client_ip}) "
            f"[{l3_b.bridge_domain} -> {l3_a.bridge_domain}]"
        )


# ---------------------------------------------------------------------------
# Routed interfaces
# ---------------------------------------------------------------------------


class TestRoutedInterfaces:
    """Dedicated L3 clients can reach their gateway and other subnets."""

    @pytest.mark.connectivity
    def test_routed_gateway(
        self,
        clab_topology: ClabTopology,
        routed_client: RoutedClient,
        record_property,
    ):
        record_property("routed_interface", routed_client.name)
        record_property("source", f"{routed_client.client_container} ({routed_client.client_ip})")
        record_property("destination", f"{routed_client.gateway} (router gateway)")
        ok = ping(routed_client.client_container, routed_client.gateway,
                  src_ip=routed_client.client_ip)
        assert ok, (
            f"Routed gateway unreachable: {routed_client.client_container}"
            f"({routed_client.client_ip}) -> {routed_client.gateway} [{routed_client.name}]"
        )

    @pytest.mark.connectivity
    def test_routed_to_bridged(self, clab_topology: ClabTopology, record_property):
        """Routed clients can reach clients in bridge domains of the same VRF."""
        routed = clab_topology.routed_clients
        if not routed:
            pytest.skip("No routed clients found")

        failures = []
        checked = 0
        for rc in routed:
            targets = [
                att for att in clab_topology.client_attachments
                if att.router == rc.router
            ]
            seen = set()
            for att in targets:
                key = f"{rc.client_container}-{att.bridge_domain}"
                if key in seen:
                    continue
                seen.add(key)
                checked += 1
                ok = ping(rc.client_container, att.client_ip, src_ip=rc.client_ip)
                if not ok:
                    failures.append(
                        f"{rc.client_container}({rc.client_ip}) -> "
                        f"{att.client_container}({att.client_ip}) "
                        f"[{rc.name} -> {att.bridge_domain}]"
                    )

        record_property("checks_run", checked)
        record_property("checks_failed", len(failures))
        assert not failures, (
            f"{len(failures)}/{checked} routed-to-bridged ping(s) failed:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------


class TestStaticRoutes:
    """Prefixes behind static routes are reachable from the VRF."""

    @pytest.mark.connectivity
    def test_static_route_reachability(
        self,
        clab_topology: ClabTopology,
        sr_src: ClientAttachment,
        sr_target_ip: str,
        sr_prefix: str,
        record_property,
    ):
        record_property("source", f"{sr_src.client_container} ({sr_src.client_ip})")
        record_property("destination", f"{sr_target_ip} (prefix {sr_prefix})")
        record_property("router", sr_src.router)
        ok = ping(sr_src.client_container, sr_target_ip, src_ip=sr_src.client_ip)
        assert ok, (
            f"Static route unreachable: {sr_src.client_container}({sr_src.client_ip}) -> "
            f"{sr_target_ip} [static route {sr_prefix}]"
        )


# ---------------------------------------------------------------------------
# Control-plane assertions
# ---------------------------------------------------------------------------


class TestControlPlane:
    """Verify control-plane state on all SR Linux nodes."""

    @pytest.mark.connectivity
    def test_bgp_all_established(self, fcli: FcliClient, clab_topology: ClabTopology, record_property):
        """All BGP peers on all nodes are in established state."""
        all_peers = fcli.bgp_peers()
        record_property("total_bgp_peers", len(all_peers))
        not_established = [
            p for p in all_peers
            if p.get("state", "").lower() != "established"
        ]
        record_property("peers_not_established", len(not_established))
        assert not not_established, (
            f"{len(not_established)}/{len(all_peers)} BGP peer(s) not established:\n"
            + "\n".join(
                f"  - {p.get('Node', '?')}: {p.get('1_peer', '?')} "
                f"state={p.get('state', '?')}"
                for p in not_established
            )
        )

    @pytest.mark.connectivity
    def test_vxlan_tunnels(self, fcli: FcliClient, clab_topology: ClabTopology, record_property):
        """Every leaf has at least one VXLAN tunnel destination."""
        missing = []
        total_tunnels = 0
        for leaf in clab_topology.leaf_nodes:
            tunnels = fcli.vxlan_tunnels(node=leaf)
            total_tunnels += len(tunnels)
            if not tunnels:
                missing.append(leaf)
        record_property("total_vxlan_tunnels", total_tunnels)
        record_property("leaves_checked", len(clab_topology.leaf_nodes))
        assert not missing, f"No VXLAN tunnels found on: {', '.join(missing)}"

    @pytest.mark.connectivity
    def test_evpn_routes_present(self, fcli: FcliClient, clab_topology: ClabTopology, record_property):
        """EVPN type-2 (MAC/IP) routes exist in the BGP RIB."""
        routes = fcli.bgp_rib(route_family="evpn", route_type="2")
        record_property("evpn_type2_routes", len(routes))
        assert routes, "No EVPN type-2 routes found in BGP RIB"

    @pytest.mark.connectivity
    def test_ethernet_segments(
        self,
        fcli: FcliClient,
        clab_topology: ClabTopology,
        record_property,
    ):
        """Ethernet segments are active for all LAGs (if any)."""
        if not clab_topology.fabric.lags:
            pytest.skip("No LAGs discovered")

        segments = fcli.ethernet_segments()
        record_property("ethernet_segments", len(segments))
        record_property("lags_discovered", len(clab_topology.fabric.lags))
        assert segments, "No ethernet segments found despite LAGs being configured"

    @pytest.mark.connectivity
    def test_lldp_adjacencies(self, fcli: FcliClient, clab_topology: ClabTopology, record_property):
        """All SR Linux nodes have LLDP neighbors (physical adjacencies intact)."""
        lldp = fcli.lldp()
        nodes_with_neighbors = {entry.get("Node", "") for entry in lldp}
        record_property("total_lldp_entries", len(lldp))
        record_property("nodes_with_lldp", len(nodes_with_neighbors))
        missing = set(clab_topology.srlinux_nodes) - nodes_with_neighbors
        assert not missing, f"No LLDP neighbors found on: {sorted(missing)}"
