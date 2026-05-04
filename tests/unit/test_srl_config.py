"""Unit tests for the srl_config filter plugin's _resolve_services logic."""

import pytest

from automation.generators.ansible_filter_plugins.srl_config import (
    _labels_match,
    _resolve_services,
    _split_by_op,
    srl_config,
)


# ---------------------------------------------------------------------------
# _labels_match
# ---------------------------------------------------------------------------


class TestLabelsMatch:
    def test_exact_match(self):
        assert _labels_match(
            ["eda.nokia.com/role=leaf"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_no_match(self):
        assert not _labels_match(
            ["eda.nokia.com/role=leaf"],
            {"eda.nokia.com/role": "spine"},
        )

    def test_or_semantics(self):
        assert _labels_match(
            ["eda.nokia.com/role=spine", "eda.nokia.com/role=leaf"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_empty_selectors(self):
        assert not _labels_match([], {"eda.nokia.com/role": "leaf"})

    def test_empty_labels(self):
        assert not _labels_match(["eda.nokia.com/role=leaf"], {})

    def test_whitespace_tolerance(self):
        assert _labels_match(
            [" eda.nokia.com/role = leaf "],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_multiple_labels(self):
        labels = {
            "eda.nokia.com/role": "leaf",
            "eda.nokia.com/tagged-v10": "enabled",
        }
        assert _labels_match(["eda.nokia.com/tagged-v10=enabled"], labels)


# ---------------------------------------------------------------------------
# _resolve_services
# ---------------------------------------------------------------------------


def _make_hv(
    *,
    node_labels=None,
    edges=None,
    lags=None,
    vlans=None,
    bridge_domains=None,
    routers=None,
    irb_interfaces=None,
    extra=None,
):
    """Build a minimal hostvars dict for testing."""
    hv = {}
    if node_labels is not None:
        hv["node"] = {"hostname": "leaf1", "labels": node_labels}
    else:
        hv["node"] = {"hostname": "leaf1"}
    if edges is not None:
        hv["edge_interfaces"] = edges
    if lags is not None:
        hv["lags"] = lags
    if vlans is not None:
        hv["vlans"] = vlans
    if bridge_domains is not None:
        hv["bridge_domains"] = bridge_domains
    if routers is not None:
        hv["routers"] = routers
    if irb_interfaces is not None:
        hv["irb_interfaces"] = irb_interfaces
    if extra:
        hv.update(extra)
    return hv


class TestResolveServicesBackwardCompat:
    def test_passthrough_without_vlans(self):
        """Without vlans key, hostvars pass through unchanged (legacy format)."""
        hv = {
            "node": {"hostname": "leaf1"},
            "bridge_domains": [
                {"name": "bd1", "vni": 100, "evi": 1, "access": [{"interface": "e-1/1", "vlan": "10"}]},
            ],
            "routers": [{"name": "vrf1", "vni": 500, "evi": 50}],
        }
        result = _resolve_services(hv)
        assert result is hv
        assert result["bridge_domains"] == hv["bridge_domains"]

    def test_passthrough_preserves_all_keys(self):
        hv = {
            "node": {"hostname": "leaf1"},
            "underlay_interfaces": [{"name": "e-1/31"}],
            "bridge_domains": [{"name": "bd1", "vni": 100, "evi": 1}],
        }
        result = _resolve_services(hv)
        assert "underlay_interfaces" in result


class TestResolveServicesVlanMatching:
    def test_vlan_matches_edge_interface(self):
        """VLAN selector matching should attach access entries to bridge domains."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            edges=[
                {"name": "ethernet-1/1", "labels": {"eda.nokia.com/tagged-v10": "enabled"}},
            ],
            vlans=[
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
            ],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 1
        bd = result["bridge_domains"][0]
        assert bd["name"] == "macvrf-v10"
        assert "access" in bd
        assert len(bd["access"]) == 1
        assert bd["access"][0]["interface"] == "ethernet-1/1"
        assert bd["access"][0]["vlan"] == "10"

    def test_vlan_matches_lag(self):
        """VLAN selector should also match LAG labels."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            lags=[
                {"name": "lag1", "labels": {"eda.nokia.com/tagged-v10": "enabled"}},
            ],
            vlans=[
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
            ],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 1
        bd = result["bridge_domains"][0]
        assert bd["access"][0]["interface"] == "lag1"

    def test_no_match_excludes_bd(self):
        """BDs with no matching access and no IRB should be excluded."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            edges=[
                {"name": "ethernet-1/1", "labels": {"eda.nokia.com/other": "enabled"}},
            ],
            vlans=[
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
            ],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 0

    def test_multiple_edges_same_bd(self):
        """Multiple edges matching the same BD produce multiple access entries."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            edges=[
                {"name": "ethernet-1/1", "labels": {"eda.nokia.com/tagged-v10": "enabled"}},
                {"name": "ethernet-1/2", "labels": {"eda.nokia.com/tagged-v10": "enabled"}},
            ],
            vlans=[
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
            ],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 1
        assert len(result["bridge_domains"][0]["access"]) == 2


class TestResolveServicesRouterFiltering:
    def test_router_matches_node_selector(self):
        """Only routers matching node labels should be included."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            vlans=[],
            bridge_domains=[],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
                {"name": "vrf2", "vni": 10600, "evi": 600, "node_selector": ["eda.nokia.com/role=spine"]},
            ],
        )
        result = _resolve_services(hv)
        router_names = {r["name"] for r in result["routers"]}
        assert "vrf1" in router_names
        assert "vrf2" not in router_names

    def test_router_no_selector_excluded(self):
        """Routers without node_selector should be excluded."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            vlans=[],
            bridge_domains=[],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500},
            ],
        )
        result = _resolve_services(hv)
        assert len(result["routers"]) == 0


class TestResolveServicesIrbAttachment:
    def test_irb_attached_to_bd(self):
        """IRB interfaces should be attached to their bridge domains."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            edges=[
                {"name": "ethernet-1/1", "labels": {"eda.nokia.com/tagged-v10": "enabled"}},
            ],
            vlans=[
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
            ],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
            irb_interfaces=[
                {
                    "bridge_domain": "macvrf-v10",
                    "router": "vrf1",
                    "ipv4": "172.16.10.254/24",
                    "proxy_arp": True,
                    "arp_timeout": 280,
                },
            ],
        )
        result = _resolve_services(hv)
        bd = result["bridge_domains"][0]
        assert "irb" in bd
        assert bd["irb"]["ipv4"] == "172.16.10.254/24"
        assert bd["irb"]["proxy_arp"] is True
        assert bd["router"] == "vrf1"
        assert "bridge_domain" not in bd["irb"]
        assert "router" not in bd["irb"]

    def test_irb_without_matching_router_excluded(self):
        """IRB referencing a router not on this node should not be attached."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "spine"},
            vlans=[],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
            irb_interfaces=[
                {
                    "bridge_domain": "macvrf-v10",
                    "router": "vrf1",
                    "ipv4": "172.16.10.254/24",
                },
            ],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 0

    def test_bd_included_via_irb_only(self):
        """A BD with no access but with an IRB on this node should be included."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            edges=[],
            vlans=[],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
            irb_interfaces=[
                {
                    "bridge_domain": "macvrf-v10",
                    "router": "vrf1",
                    "ipv4": "172.16.10.254/24",
                },
            ],
        )
        result = _resolve_services(hv)
        assert len(result["bridge_domains"]) == 1
        bd = result["bridge_domains"][0]
        assert "access" not in bd
        assert "irb" in bd

    def test_vlans_key_removed_after_resolution(self):
        """The vlans key should be removed from resolved hostvars."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            vlans=[],
            bridge_domains=[],
            routers=[],
        )
        result = _resolve_services(hv)
        assert "vlans" not in result

    def test_original_hv_not_mutated(self):
        """_resolve_services should not mutate the original dict."""
        hv = _make_hv(
            node_labels={"eda.nokia.com/role": "leaf"},
            vlans=[],
            bridge_domains=[
                {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            ],
            routers=[
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
        )
        original_routers = list(hv["routers"])
        _resolve_services(hv)
        assert hv["routers"] == original_routers
        assert "vlans" in hv


class TestResolveServicesEndToEnd:
    """Simulate the full group_vars + host_vars merge and resolution."""

    def test_full_resolution(self):
        """Simulate Ansible merging group_vars and host_vars, then resolving."""
        group_vars = {
            "bridge_domains": [
                {"name": "macvrf-v10", "vni": 100101, "evi": 10, "mac_duplication": {"enabled": True}},
                {"name": "macvrf-v20", "vni": 100201, "evi": 20, "mac_duplication": {"enabled": True}},
            ],
            "routers": [
                {"name": "vrf1", "vni": 10500, "evi": 500, "node_selector": ["eda.nokia.com/role=leaf"]},
            ],
            "vlans": [
                {
                    "name": "tagged-v10",
                    "bridge_domain": "macvrf-v10",
                    "vlan_id": "10",
                    "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
                },
                {
                    "name": "tagged-v20",
                    "bridge_domain": "macvrf-v20",
                    "vlan_id": "20",
                    "interface_selector": ["eda.nokia.com/tagged-v20=enabled"],
                },
            ],
        }
        host_vars = {
            "node": {
                "hostname": "leaf1",
                "role": "leaf",
                "router_id": "192.168.254.11",
                "asn": 65411,
                "labels": {
                    "eda.nokia.com/role": "leaf",
                    "eda.nokia.com/name": "leaf1",
                },
            },
            "edge_interfaces": [
                {
                    "name": "ethernet-1/1",
                    "encap": "tagged",
                    "labels": {
                        "eda.nokia.com/tagged-v10": "enabled",
                        "eda.nokia.com/tagged-v20": "enabled",
                    },
                },
            ],
            "irb_interfaces": [
                {
                    "bridge_domain": "macvrf-v10",
                    "router": "vrf1",
                    "ipv4": "172.16.10.254/24",
                    "proxy_arp": True,
                    "arp_timeout": 280,
                },
                {
                    "bridge_domain": "macvrf-v20",
                    "router": "vrf1",
                    "ipv4": "172.16.20.254/24",
                    "proxy_arp": True,
                    "arp_timeout": 280,
                },
            ],
        }

        # Simulate Ansible's variable merge (host_vars override group_vars)
        merged = {**group_vars, **host_vars}

        result = _resolve_services(merged)

        assert len(result["bridge_domains"]) == 2
        assert len(result["routers"]) == 1
        assert "vlans" not in result

        bd_names = {bd["name"] for bd in result["bridge_domains"]}
        assert bd_names == {"macvrf-v10", "macvrf-v20"}

        for bd in result["bridge_domains"]:
            assert "access" in bd
            assert "irb" in bd
            assert bd["router"] == "vrf1"


# ---------------------------------------------------------------------------
# _split_by_op
# ---------------------------------------------------------------------------


class TestSplitByOp:
    def test_update_on_interface_parent_stays_in_update(self):
        # Regression: when the builder emits /interface[name=X] as op:update
        # (edge interface parent, underlay, system0, LAG members), _split_by_op
        # must leave it in the update bucket. Promoting to replace wipes the
        # subinterface children the NI still references, which the device
        # rejects at commit time with "subinterface X.Y not found".
        entries = [
            {
                "path": "/interface[name=ethernet-1/6]",
                "value": {"admin-state": "enable", "vlan-tagging": True},
                "op": "update",
            },
        ]
        buckets = _split_by_op(entries)
        assert buckets["update"] == [
            {
                "path": "/interface[name=ethernet-1/6]",
                "value": {"admin-state": "enable", "vlan-tagging": True},
            }
        ]
        assert buckets["replace"] == []
        assert buckets["delete"] == []

    def test_replace_on_interface_parent_goes_to_replace(self):
        entries = [
            {
                "path": "/interface[name=lag1]",
                "value": {"admin-state": "enable"},
                "op": "replace",
            },
        ]
        buckets = _split_by_op(entries)
        assert buckets["replace"] and not buckets["update"]

    def test_replace_on_non_infra_path_goes_to_replace(self):
        entries = [
            {
                "path": "/network-instance[name=macvrf-v60]",
                "value": {"type": "mac-vrf"},
                "op": "replace",
            },
            {
                "path": "/routing-policy",
                "value": {},
                "op": "replace",
            },
        ]
        buckets = _split_by_op(entries)
        paths = [e["path"] for e in buckets["replace"]]
        assert "/network-instance[name=macvrf-v60]" in paths
        assert "/routing-policy" in paths
        assert buckets["update"] == []

    def test_delete_bucket(self):
        entries = [{"path": "/interface[name=x]/subinterface[index=10]", "op": "delete"}]
        buckets = _split_by_op(entries)
        assert buckets["delete"] == [{"path": "/interface[name=x]/subinterface[index=10]"}]
        assert buckets["update"] == [] and buckets["replace"] == []

    def test_missing_op_falls_back_to_prefix_heuristic(self):
        entries = [
            {"path": "/interface[name=e-1/1]", "value": {"a": 1}},
            {"path": "/system/name", "value": {"host-name": "leaf1"}},
        ]
        buckets = _split_by_op(entries)
        assert buckets["replace"][0]["path"] == "/interface[name=e-1/1]"
        assert buckets["update"][0]["path"] == "/system/name"


# ---------------------------------------------------------------------------
# End-to-end: removing a VLAN must not cascade-wipe the edge interface
# ---------------------------------------------------------------------------


def _intent_with_edge_and_bd(*, include_vlan: bool) -> dict:
    """Build a minimal merged-hostvars dict resembling the 3-stage design.

    leaf1 has edge ``ethernet-1/6`` labelled for ``untagged-v60``. The VLAN
    resource bound to ``macvrf-v60`` is only present when ``include_vlan``
    is True — this simulates the operator removing the VLAN from group_vars.

    The BD has an IRB on this leaf so it survives ``_resolve_services``
    even when the VLAN is absent — which is exactly the live failure mode
    that produced the "subinterface ethernet-1/6.4096 not found" error.
    """
    vlans = []
    if include_vlan:
        vlans.append({
            "name": "untagged-v60",
            "bridge_domain": "macvrf-v60",
            "vlan_id": "untagged",
            "interface_selector": ["eda.nokia.com/untagged-v60=enabled"],
        })

    return {
        "node": {
            "hostname": "leaf1",
            "role": "leaf",
            "router_id": "192.168.254.11",
            "asn": 65411,
            "labels": {"eda.nokia.com/role": "leaf"},
        },
        "edge_interfaces": [
            {
                "name": "ethernet-1/6",
                "encap": "dot1q",
                "labels": {"eda.nokia.com/untagged-v60": "enabled"},
            },
        ],
        "bridge_domains": [
            {"name": "macvrf-v60", "vni": 100601, "evi": 60},
        ],
        "routers": [
            {
                "name": "vrf1",
                "vni": 10500,
                "evi": 500,
                "node_selector": ["eda.nokia.com/role=leaf"],
            },
        ],
        "irb_interfaces": [
            {
                "bridge_domain": "macvrf-v60",
                "router": "vrf1",
                "ipv4": "172.16.60.254/24",
                "anycast_gw": True,
                "proxy_arp": True,
                "arp_timeout": 280,
            },
        ],
        "vlans": vlans,
    }


class TestVlanRemovalDoesNotCascade:
    def test_edge_interface_update_survives_vlan_removal(self):
        # When the operator removes untagged-v60 but keeps macvrf-v60 in the
        # intent, the builder must still emit:
        #   - /interface[name=ethernet-1/6] as an UPDATE (merge), not a replace
        #     that would wipe any surviving subinterfaces on the device
        #   - /network-instance[name=macvrf-v60] in the replace bucket
        hv = _intent_with_edge_and_bd(include_vlan=False)
        out = srl_config(hv, sw_version="25.10.1", phase="services")

        edge_update = [
            e for e in out["update"]
            if e["path"] == "/interface[name=ethernet-1/6]"
        ]
        edge_replace = [
            e for e in out["replace"]
            if e["path"] == "/interface[name=ethernet-1/6]"
        ]
        assert len(edge_update) == 1, (
            "edge interface parent must stay in update bucket "
            "so children (subinterfaces) are not wiped"
        )
        assert edge_replace == []

        macvrf_paths = [
            e["path"] for e in out["replace"]
            if e["path"] == "/network-instance[name=macvrf-v60]"
        ]
        assert macvrf_paths, "macvrf-v60 must still be declared in replace bucket"

    def test_edge_interface_update_also_survives_with_vlan(self):
        # Sanity: with the VLAN present, the edge parent is still an update
        # (children subif[4096] is emitted separately as op:replace).
        hv = _intent_with_edge_and_bd(include_vlan=True)
        out = srl_config(hv, sw_version="25.10.1", phase="services")

        edge_update = [
            e for e in out["update"]
            if e["path"] == "/interface[name=ethernet-1/6]"
        ]
        subif = [
            e for e in out["replace"]
            if e["path"] == "/interface[name=ethernet-1/6]/subinterface[index=4096]"
        ]
        assert len(edge_update) == 1
        assert len(subif) == 1


# ---------------------------------------------------------------------------
# Stable irb0 / vxlan0 subinterface indexing
# ---------------------------------------------------------------------------


def _intent_with_bds_and_irbs(*, include_v10_irb: bool) -> dict:
    """Leaf-like hv with IRBs on macvrf-v10 and macvrf-v20.

    Dropping the v10 IRB simulates the operator removing the IRB tied to
    macvrf-v10 / vrf1 from group_vars. macvrf-v10 is dropped from the
    resolved hv (no IRB on this node, and the edge label does not bind
    it) while macvrf-v20's index must stay stable.
    """
    return {
        "node": {
            "hostname": "leaf1",
            "role": "leaf",
            "router_id": "192.168.254.11",
            "asn": 65411,
            "labels": {"eda.nokia.com/role": "leaf"},
        },
        "edge_interfaces": [
            {
                "name": "ethernet-1/1",
                "encap": "dot1q",
                "labels": {"eda.nokia.com/tagged-v20": "enabled"},
            },
        ],
        "bridge_domains": [
            {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            {"name": "macvrf-v20", "vni": 100201, "evi": 20},
        ],
        "routers": [
            {
                "name": "vrf1",
                "vni": 10500,
                "evi": 500,
                "node_selector": ["eda.nokia.com/role=leaf"],
            },
        ],
        "irb_interfaces": (
            [
                {
                    "bridge_domain": "macvrf-v10",
                    "router": "vrf1",
                    "ipv4": "172.16.10.254/24",
                    "anycast_gw": True,
                },
                {
                    "bridge_domain": "macvrf-v20",
                    "router": "vrf1",
                    "ipv4": "172.16.20.254/24",
                    "anycast_gw": True,
                },
            ]
            if include_v10_irb
            else [
                {
                    "bridge_domain": "macvrf-v20",
                    "router": "vrf1",
                    "ipv4": "172.16.20.254/24",
                    "anycast_gw": True,
                },
            ]
        ),
        "vlans": [
            {
                "name": "tagged-v20",
                "bridge_domain": "macvrf-v20",
                "vlan_id": "20",
                "interface_selector": ["eda.nokia.com/tagged-v20=enabled"],
            },
        ],
    }


def _macvrf_members(out: dict, bd_name: str) -> list[str]:
    """Extract the interface members SR Linux will bind to a mac-vrf NI."""
    path = f"/network-instance[name={bd_name}]"
    entries = [e for e in out["replace"] if e["path"] == path]
    assert len(entries) == 1, f"expected one NI replace for {bd_name}"
    return [i["name"] for i in entries[0]["value"]["interface"]]


class TestStableSubinterfaceIndexing:
    def test_irb_index_stable_when_other_bd_removed(self):
        # Regression: removing the IRB for macvrf-v10 must not reshuffle
        # macvrf-v20's irb0.X index. Before the fix, positional allocation
        # made v20 jump from irb0.1 -> irb0.0, which SRL rejects with:
        #   "subinterface irb0.0 is already bound to macvrf-v10"
        before = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=True),
            sw_version="25.10.1", phase="services",
        )
        after = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=False),
            sw_version="25.10.1", phase="services",
        )

        def irb_index_for(out: dict, bd_name: str) -> int:
            for member in _macvrf_members(out, bd_name):
                if member.startswith("irb0."):
                    return int(member.split(".", 1)[1])
            raise AssertionError(f"no irb0.* member found on {bd_name}")

        assert irb_index_for(before, "macvrf-v20") == irb_index_for(after, "macvrf-v20")

    def test_irb_index_equals_evi(self):
        out = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=True),
            sw_version="25.10.1", phase="services",
        )
        # EVI 10 -> irb0.10, EVI 20 -> irb0.20
        assert "irb0.10" in _macvrf_members(out, "macvrf-v10")
        assert "irb0.20" in _macvrf_members(out, "macvrf-v20")

    def test_vxlan_index_stable_when_other_service_removed(self):
        before = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=True),
            sw_version="25.10.1", phase="services",
        )
        after = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=False),
            sw_version="25.10.1", phase="services",
        )

        def vxlan_index_for(out: dict, bd_name: str) -> int:
            path = f"/network-instance[name={bd_name}]"
            entries = [e for e in out["replace"] if e["path"] == path]
            members = [i["name"] for i in entries[0]["value"]["vxlan-interface"]]
            vx = next(m for m in members if m.startswith("vxlan0."))
            return int(vx.split(".", 1)[1])

        assert vxlan_index_for(before, "macvrf-v20") == vxlan_index_for(
            after, "macvrf-v20"
        )

    def test_vxlan_index_equals_vni(self):
        out = srl_config(
            _intent_with_bds_and_irbs(include_v10_irb=True),
            sw_version="25.10.1", phase="services",
        )
        # VNI is used directly as the vxlan-interface index.
        for tun in out["replace"]:
            if tun["path"].startswith("/tunnel-interface[name=vxlan0]/vxlan-interface"):
                idx = int(tun["path"].split("index=", 1)[1].rstrip("]"))
                assert idx == tun["value"]["ingress"]["vni"]


# ---------------------------------------------------------------------------
# L2-only bridge domains (no IRB) must not bind a non-existent irb0.<evi>
# ---------------------------------------------------------------------------


def _intent_l2_only_bd() -> dict:
    """Leaf hv with two BDs, only one of which has an IRB.

    macvrf-v10 has both an IRB and access; macvrf-v99 is a pure L2 BD —
    access port present (via the tagged-v99 VLAN/edge label) but no IRB
    intent. Reproduces the field error:
        Error in /network-instance[name=macvrf-v99]/interface[name=irb0.99]:
            subinterface irb0.99 not found
    """
    return {
        "node": {
            "hostname": "leaf1",
            "role": "leaf",
            "router_id": "192.168.254.11",
            "asn": 65411,
            "labels": {"eda.nokia.com/role": "leaf"},
        },
        "edge_interfaces": [
            {
                "name": "ethernet-1/3",
                "encap": "dot1q",
                "labels": {
                    "eda.nokia.com/role": "edge",
                    "eda.nokia.com/tagged-v10": "enabled",
                    "eda.nokia.com/tagged-v99": "enabled",
                },
            },
        ],
        "bridge_domains": [
            {"name": "macvrf-v10", "vni": 100101, "evi": 10},
            {"name": "macvrf-v99", "vni": 10099, "evi": 99},
        ],
        "routers": [
            {
                "name": "vrf1",
                "vni": 10500,
                "evi": 500,
                "node_selector": ["eda.nokia.com/role=leaf"],
            },
        ],
        "irb_interfaces": [
            {
                "bridge_domain": "macvrf-v10",
                "router": "vrf1",
                "ipv4": "172.16.10.254/24",
                "anycast_gw": True,
            },
        ],
        "vlans": [
            {
                "name": "tagged-v10",
                "bridge_domain": "macvrf-v10",
                "vlan_id": "10",
                "interface_selector": ["eda.nokia.com/tagged-v10=enabled"],
            },
            {
                "name": "tagged-v99",
                "bridge_domain": "macvrf-v99",
                "vlan_id": "99",
                "interface_selector": ["eda.nokia.com/tagged-v99=enabled"],
            },
        ],
    }


class TestL2OnlyBridgeDomain:
    """An L2-only BD (access ports, no IRB) must not get irb0.<evi> attached."""

    def test_l2_only_bd_omits_irb_member(self):
        out = srl_config(_intent_l2_only_bd(), sw_version="25.10.1", phase="services")

        v99_members = _macvrf_members(out, "macvrf-v99")
        assert not any(m.startswith("irb0.") for m in v99_members), (
            f"L2-only BD macvrf-v99 must not have an irb0.* member, got {v99_members}"
        )
        assert "ethernet-1/3.99" in v99_members, (
            f"expected access subif ethernet-1/3.99 in {v99_members}"
        )

    def test_irb_attached_bd_still_gets_irb_member(self):
        # Sanity: the fix must not regress the normal "BD-with-IRB" path.
        out = srl_config(_intent_l2_only_bd(), sw_version="25.10.1", phase="services")
        assert "irb0.10" in _macvrf_members(out, "macvrf-v10")
