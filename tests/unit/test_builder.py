"""Unit tests for design builders (topology.yaml → FabricIntent)."""

import pytest
from pathlib import Path

from automation.core.fabric_builder import build_intent, DESIGN_BUILDERS
from automation.core.schema_validator import load_inputs
from automation.core.models import FabricIntent


DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


class TestFabricBuilder:
    def test_registry_contains_known_designs(self):
        assert "3-stage-evpn-vxlan" in DESIGN_BUILDERS
        assert "unconstrained-3-stage" in DESIGN_BUILDERS

    def test_unknown_design_raises(self):
        with pytest.raises(ValueError, match="Unknown design"):
            build_intent({"design": "nonexistent"}, {})

    def test_missing_design_field_raises(self):
        with pytest.raises(ValueError, match="must contain a 'design' field"):
            build_intent({}, {})


class TestThreeStageBuilder:
    @pytest.fixture()
    def intent(self) -> FabricIntent:
        design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
        if not design_dir.exists():
            pytest.skip("3-stage-evpn-vxlan design not found")
        topo, svc = load_inputs(design_dir)
        return build_intent(topo, svc)

    def test_design_name(self, intent):
        assert intent.design == "3-stage-evpn-vxlan"

    def test_fabric_name(self, intent):
        assert intent.fabric_name == "dc1"

    def test_node_count(self, intent):
        leafs = [n for n in intent.nodes if n.role == "leaf"]
        spines = [n for n in intent.nodes if n.role == "spine"]
        assert len(leafs) == 8
        assert len(spines) == 2

    def test_links_exist(self, intent):
        assert len(intent.links) > 0
        assert len(intent.links) == 8 * 2  # each leaf connects to each spine

    def test_bridge_domains(self, intent):
        assert len(intent.bridge_domains) == 6
        names = {bd.name for bd in intent.bridge_domains}
        assert "macvrf-v10" in names

    def test_routers(self, intent):
        assert len(intent.routers) == 2
        names = {r.name for r in intent.routers}
        assert "vrf1" in names
        assert "vrf2" in names

    def test_irb_interfaces(self, intent):
        assert len(intent.irb_interfaces) == 6

    def test_vlans(self, intent):
        assert len(intent.vlans) == 7

    def test_edge_interfaces(self, intent):
        assert len(intent.edge_interfaces) > 0

    def test_lags(self, intent):
        assert len(intent.lags) == 3

    def test_static_routes(self, intent):
        assert len(intent.static_routes) == 1

    def test_eda_settings(self, intent):
        assert intent.eda.namespace == "eda"

    def test_configlets(self, intent):
        assert len(intent.configlets) >= 1
        names = {c.name for c in intent.configlets}
        assert "contact-info" in names

    def test_default_mtus(self, intent):
        assert len(intent.default_mtus) >= 1

    def test_node_asns_assigned(self, intent):
        for node in intent.nodes:
            assert node.asn > 0

    def test_system0_ips_unique(self, intent):
        ips = [n.system0_ipv4 for n in intent.nodes]
        assert len(ips) == len(set(ips))


class TestNodeOverrides:
    """Test per-node overrides in the 3-stage-evpn-vxlan design."""

    @pytest.fixture()
    def topo_and_svc(self):
        design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
        if not design_dir.exists():
            pytest.skip("3-stage-evpn-vxlan design not found")
        return load_inputs(design_dir)

    def test_nodes_override_mgmt_ip(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [{"name": "leaf1", "mgmt_ipv4": "172.21.21.50"}]
        intent = build_intent(topo, svc)
        leaf1 = next(n for n in intent.nodes if n.name == "leaf1")
        assert leaf1.mgmt_ipv4 == "172.21.21.50"

    def test_nodes_override_labels_merge(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [
            {"name": "leaf1", "labels": {"custom-key": "custom-value"}},
        ]
        intent = build_intent(topo, svc)
        leaf1 = next(n for n in intent.nodes if n.name == "leaf1")
        assert leaf1.labels["custom-key"] == "custom-value"
        assert leaf1.labels["eda.nokia.com/role"] == "leaf"
        assert leaf1.labels["eda.nokia.com/name"] == "leaf1"

    def test_nodes_override_platform(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [{"name": "leaf1", "platform": "7220 IXR-D2L"}]
        intent = build_intent(topo, svc)
        leaf1 = next(n for n in intent.nodes if n.name == "leaf1")
        assert leaf1.platform == "7220 IXR-D2L"
        leaf2 = next(n for n in intent.nodes if n.name == "leaf2")
        assert leaf2.platform == topo["leafs"]["platform"]

    def test_nodes_override_version(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [{"name": "spine1", "version": "25.10.3"}]
        intent = build_intent(topo, svc)
        spine1 = next(n for n in intent.nodes if n.name == "spine1")
        assert spine1.version == "25.10.3"

    def test_nodes_override_unknown_name_raises(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [{"name": "leaf99", "version": "25.10.3"}]
        with pytest.raises(ValueError, match="unknown node 'leaf99'"):
            build_intent(topo, svc)

    def test_nodes_duplicate_mgmt_ip_raises(self, topo_and_svc):
        topo, svc = topo_and_svc
        topo["nodes"] = [
            {"name": "leaf1", "mgmt_ipv4": "172.21.21.12"},
        ]
        with pytest.raises(ValueError, match="Duplicate management IP"):
            build_intent(topo, svc)

    def test_nodes_base_range_overlap_raises(self, topo_and_svc):
        from automation.designs.three_stage_evpn_vxlan import _build_nodes

        with pytest.raises(ValueError, match="Duplicate management IP"):
            _build_nodes(
                spine_cfg={
                    "count": 2,
                    "platform": "7220 IXR-D3L",
                    "version": "25.10.2",
                    "mgmt_base_ipv4": "172.21.21.11",
                },
                leaf_cfg={
                    "count": 4,
                    "platform": "7220 IXR-D3L",
                    "version": "25.10.2",
                    "mgmt_base_ipv4": "172.21.21.11",
                },
                spine_asn=65500,
                leaf_asn_start=65411,
                system0_prefix="192.168.254.0/24",
            )

    def test_no_overrides_still_validates(self, topo_and_svc):
        """Without nodes key, existing behavior is preserved."""
        topo, svc = topo_and_svc
        topo.pop("nodes", None)
        intent = build_intent(topo, svc)
        mgmt_ips = [n.mgmt_ipv4 for n in intent.nodes if n.mgmt_ipv4]
        assert len(mgmt_ips) == len(set(mgmt_ips))


class TestNameTemplate:
    """Test configurable node name templates."""

    def _build(self, *, leaf_template=None, spine_template=None, leaf_count=3, spine_count=2):
        from automation.designs.three_stage_evpn_vxlan import _build_nodes

        leaf_cfg = {
            "count": leaf_count,
            "platform": "7220 IXR-D3L",
            "version": "25.10.2",
            "mgmt_base_ipv4": "172.21.21.11",
        }
        spine_cfg = {
            "count": spine_count,
            "platform": "7220 IXR-D3L",
            "version": "25.10.2",
            "mgmt_base_ipv4": "172.21.21.101",
        }
        if leaf_template is not None:
            leaf_cfg["name_template"] = leaf_template
        if spine_template is not None:
            spine_cfg["name_template"] = spine_template

        return _build_nodes(
            spine_cfg=spine_cfg,
            leaf_cfg=leaf_cfg,
            spine_asn=65500,
            leaf_asn_start=65411,
            system0_prefix="192.168.254.0/24",
        )

    def test_default_names(self):
        """Without name_template, nodes use leaf{i} / spine{i}."""
        nodes = self._build()
        leaf_names = [n.name for n in nodes if n.role == "leaf"]
        spine_names = [n.name for n in nodes if n.role == "spine"]
        assert leaf_names == ["leaf1", "leaf2", "leaf3"]
        assert spine_names == ["spine1", "spine2"]

    def test_custom_leaf_template(self):
        nodes = self._build(leaf_template="dc1-leaf-{i}")
        leaf_names = [n.name for n in nodes if n.role == "leaf"]
        assert leaf_names == ["dc1-leaf-1", "dc1-leaf-2", "dc1-leaf-3"]

    def test_custom_spine_template(self):
        nodes = self._build(spine_template="dc1-spine-{i}")
        spine_names = [n.name for n in nodes if n.role == "spine"]
        assert spine_names == ["dc1-spine-1", "dc1-spine-2"]

    def test_leading_zeros(self):
        nodes = self._build(leaf_template="leaf{i:02}", leaf_count=8)
        leaf_names = [n.name for n in nodes if n.role == "leaf"]
        assert leaf_names == [
            "leaf01", "leaf02", "leaf03", "leaf04",
            "leaf05", "leaf06", "leaf07", "leaf08",
        ]

    def test_leading_zeros_three_digits(self):
        nodes = self._build(spine_template="spine{i:03}", spine_count=2)
        spine_names = [n.name for n in nodes if n.role == "spine"]
        assert spine_names == ["spine001", "spine002"]

    def test_name_label_matches_generated_name(self):
        nodes = self._build(leaf_template="dc1-lf{i:02}")
        for node in nodes:
            if node.role == "leaf":
                assert node.labels["eda.nokia.com/name"] == node.name

    def test_name_label_matches_spine_template(self):
        nodes = self._build(spine_template="dc1-sp{i}")
        for node in nodes:
            if node.role == "spine":
                assert node.labels["eda.nokia.com/name"] == node.name

    def test_duplicate_names_raises(self):
        """Template without {i} produces duplicates and should raise."""
        with pytest.raises(ValueError, match="Duplicate node name"):
            self._build(leaf_template="same-name", leaf_count=2)

    def test_overrides_work_with_custom_names(self):
        from automation.designs.three_stage_evpn_vxlan import _build_nodes

        nodes = _build_nodes(
            spine_cfg={
                "count": 1,
                "platform": "7220 IXR-D3L",
                "version": "25.10.2",
                "mgmt_base_ipv4": "172.21.21.101",
                "name_template": "sp-{i:02}",
            },
            leaf_cfg={
                "count": 2,
                "platform": "7220 IXR-D3L",
                "version": "25.10.2",
                "mgmt_base_ipv4": "172.21.21.11",
                "name_template": "lf-{i:02}",
            },
            spine_asn=65500,
            leaf_asn_start=65411,
            system0_prefix="192.168.254.0/24",
            node_overrides=[
                {"name": "lf-01", "mgmt_ipv4": "172.21.21.50"},
            ],
        )
        lf01 = next(n for n in nodes if n.name == "lf-01")
        assert lf01.mgmt_ipv4 == "172.21.21.50"

    def test_overrides_unknown_custom_name_raises(self):
        from automation.designs.three_stage_evpn_vxlan import _build_nodes

        with pytest.raises(ValueError, match="unknown node 'leaf1'"):
            _build_nodes(
                spine_cfg={
                    "count": 1,
                    "platform": "7220 IXR-D3L",
                    "version": "25.10.2",
                    "mgmt_base_ipv4": "172.21.21.101",
                },
                leaf_cfg={
                    "count": 2,
                    "platform": "7220 IXR-D3L",
                    "version": "25.10.2",
                    "mgmt_base_ipv4": "172.21.21.11",
                    "name_template": "lf-{i}",
                },
                spine_asn=65500,
                leaf_asn_start=65411,
                system0_prefix="192.168.254.0/24",
                node_overrides=[
                    {"name": "leaf1", "version": "25.10.3"},
                ],
            )


class TestExtrasMerge:
    """Test extras merge functions for all resource types."""

    @pytest.fixture()
    def base_intent(self) -> FabricIntent:
        design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
        if not design_dir.exists():
            pytest.skip("3-stage-evpn-vxlan design not found")
        topo, svc = load_inputs(design_dir)
        return build_intent(topo, svc)

    def test_merge_extras_routers_override(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_routers

        original = list(base_intent.routers)
        extras = [{"name": "vrf1", "node_selector": ["eda.nokia.com/role=spine"]}]
        result = _merge_extras_routers(original, extras)
        vrf1 = next(r for r in result if r.name == "vrf1")
        assert "eda.nokia.com/role=spine" in vrf1.node_selector
        assert len(result) == len(original)

    def test_merge_extras_routers_append(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_routers

        original = list(base_intent.routers)
        extras = [{"name": "vrf-new", "vni": 99999, "evi": 999, "node_selector": ["eda.nokia.com/role=leaf"]}]
        result = _merge_extras_routers(original, extras)
        assert len(result) == len(original) + 1
        new_r = next(r for r in result if r.name == "vrf-new")
        assert new_r.vni == 99999

    def test_merge_extras_vlans_override(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_vlans

        original = list(base_intent.vlans)
        extras = [{"name": "tagged-v10", "vlan_id": "100"}]
        result = _merge_extras_vlans(original, extras)
        v10 = next(v for v in result if v.name == "tagged-v10")
        assert v10.vlan_id == "100"
        assert len(result) == len(original)

    def test_merge_extras_vlans_append(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_vlans

        original = list(base_intent.vlans)
        extras = [{"name": "new-vlan", "bridge_domain": "macvrf-v10", "vlan_id": "200", "interface_selector": ["foo=bar"]}]
        result = _merge_extras_vlans(original, extras)
        assert len(result) == len(original) + 1

    def test_merge_extras_routed_interfaces_append(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_routed_interfaces

        original = list(base_intent.routed_interfaces)
        extras = [{
            "name": "leaf2-uplink",
            "interface": "leaf2-ethernet-1-4",
            "router": "vrf1",
            "vlan_id": "null",
            "ip_mtu": 9000,
            "ipv4_addresses": [{"ipPrefix": "172.16.101.0/31", "primary": True}],
        }]
        result = _merge_extras_routed_interfaces(original, extras)
        assert len(result) == len(original) + 1
        new_ri = next(ri for ri in result if ri.name == "leaf2-uplink")
        assert new_ri.ip_mtu == 9000

    def test_merge_extras_routed_interfaces_override(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_routed_interfaces

        original = list(base_intent.routed_interfaces)
        extras = [{"name": "leaf-s5", "ip_mtu": 9000}]
        result = _merge_extras_routed_interfaces(original, extras)
        assert len(result) == len(original)
        ri = next(r for r in result if r.name == "leaf-s5")
        assert ri.ip_mtu == 9000

    def test_merge_extras_static_routes_append(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_static_routes

        original = list(base_intent.static_routes)
        extras = [{
            "name": "default-route",
            "router": "vrf2",
            "nodes": ["leaf1"],
            "prefixes": ["0.0.0.0/0"],
            "nexthop_group": {"nexthops": [{"ipPrefix": "172.16.40.1"}]},
        }]
        result = _merge_extras_static_routes(original, extras)
        assert len(result) == len(original) + 1

    def test_merge_extras_static_routes_override(self, base_intent):
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_static_routes

        original = list(base_intent.static_routes)
        extras = [{"name": "static-s5", "prefixes": ["10.0.0.0/8"]}]
        result = _merge_extras_static_routes(original, extras)
        assert len(result) == len(original)
        sr = next(s for s in result if s.name == "static-s5")
        assert "10.0.0.0/8" in sr.prefixes

    def test_bridge_domain_mac_fields_forwarded(self, base_intent):
        """Verify extras mac overrides survive into the intent."""
        from automation.designs.three_stage_evpn_vxlan import _merge_extras_bridge_domains

        original = list(base_intent.bridge_domains)
        extras = [{"name": "macvrf-v10", "mac_learning": False, "mac_aging": 600}]
        result = _merge_extras_bridge_domains(original, extras)
        bd = next(b for b in result if b.name == "macvrf-v10")
        assert bd.mac_learning is False
        assert bd.mac_aging == 600


class TestUnconstrainedBuilder:
    @pytest.fixture()
    def intent(self) -> FabricIntent:
        design_dir = DESIGNS_ROOT / "unconstrained-3-stage"
        if not design_dir.exists():
            pytest.skip("unconstrained-3-stage design not found")
        topo, svc = load_inputs(design_dir)
        return build_intent(topo, svc)

    def test_design_name(self, intent):
        assert intent.design == "unconstrained-3-stage"

    def test_nodes_from_explicit_list(self, intent):
        assert len(intent.nodes) > 0
        for node in intent.nodes:
            assert node.name
            assert node.role in ("leaf", "spine")

    def test_links_from_explicit_list(self, intent):
        assert len(intent.links) > 0
