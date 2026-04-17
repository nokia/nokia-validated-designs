"""Unit tests for automation.generators.ansible_generator."""

import pytest
import yaml
from pathlib import Path
from typing import Any

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.core.models import (
    ConfigletConfigEntry,
    ConfigletIntent,
    DefaultMtuIntent,
    FabricIntent,
)
from automation.generators.ansible_generator import (
    _jspath_to_jsonrpc,
    _resolve_configlets,
    _resolve_default_mtus,
    _resolve_placement,
    _build_leaf_host_vars,
    _build_spine_host_vars,
    generate,
)


DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


@pytest.fixture()
def three_stage_intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


# ---------------------------------------------------------------------------
# jspath → JSON-RPC path conversion
# ---------------------------------------------------------------------------


class TestJspathConversion:
    def test_simple_path(self):
        assert _jspath_to_jsonrpc(".system.information") == "/system/information"

    def test_already_slash_path(self):
        assert _jspath_to_jsonrpc("/system/information") == "/system/information"

    def test_predicate_path(self):
        result = _jspath_to_jsonrpc(
            '.network-instance{.name=="default"}.protocols.bgp'
        )
        assert result == "/network-instance[name=default]/protocols/bgp"

    def test_predicate_no_quotes(self):
        result = _jspath_to_jsonrpc(
            ".network-instance{.name==default}.protocols.bgp"
        )
        assert result == "/network-instance[name=default]/protocols/bgp"

    def test_nested_path(self):
        result = _jspath_to_jsonrpc(".system.name.host-name")
        assert result == "/system/name/host-name"


# ---------------------------------------------------------------------------
# Configlet resolution
# ---------------------------------------------------------------------------


class TestResolveConfiglets:
    def test_extras_only(self, three_stage_intent):
        """Only configlets with origin='extras' should be resolved."""
        # Add a non-extras configlet
        three_stage_intent.configlets.append(
            ConfigletIntent(
                name="design-configlet",
                origin="3-stage",
                endpoints=["leaf1"],
                configs=[
                    ConfigletConfigEntry(
                        path=".system.information",
                        config='{"description": "test"}',
                    )
                ],
            )
        )
        result = _resolve_configlets(three_stage_intent)
        # Design configlets should be excluded
        for node_overrides in result.values():
            for entry in node_overrides:
                assert entry["path"] != "/system/information" or entry["value"] != {
                    "description": "test"
                }

    def test_selector_matches_nodes(self, three_stage_intent):
        """Configlets targeting by selector should resolve to matching nodes."""
        three_stage_intent.configlets.append(
            ConfigletIntent(
                name="leaf-configlet",
                origin="extras",
                endpoint_selector=["eda.nokia.com/role=leaf"],
                configs=[
                    ConfigletConfigEntry(
                        path=".system.name.host-name",
                        config='{"host-name": "test"}',
                    )
                ],
            )
        )
        result = _resolve_configlets(three_stage_intent)
        leaf_names = {n.name for n in three_stage_intent.nodes if n.role == "leaf"}
        spine_names = {n.name for n in three_stage_intent.nodes if n.role == "spine"}
        # All leaves should have the override
        for leaf in leaf_names:
            assert leaf in result
        # No spines should have it
        for spine in spine_names:
            # Spine might be in result from other configlets, but not this one
            if spine in result:
                paths = [e["path"] for e in result[spine]]
                assert "/system/name/host-name" not in paths

    def test_endpoint_targeting(self, three_stage_intent):
        """Configlets targeting specific endpoints should resolve correctly."""
        node_name = three_stage_intent.nodes[0].name
        three_stage_intent.configlets.append(
            ConfigletIntent(
                name="specific-node",
                origin="extras",
                endpoints=[node_name],
                configs=[
                    ConfigletConfigEntry(
                        path=".system.information",
                        config='{"location": "lab"}',
                    )
                ],
            )
        )
        result = _resolve_configlets(three_stage_intent)
        assert node_name in result
        paths = [e["path"] for e in result[node_name]]
        assert "/system/information" in paths


# ---------------------------------------------------------------------------
# DefaultMTU resolution
# ---------------------------------------------------------------------------


class TestResolveDefaultMtus:
    def test_selector_resolution(self, three_stage_intent):
        """MTU with node_selector should resolve to matching nodes."""
        three_stage_intent.default_mtus.append(
            DefaultMtuIntent(
                name="test-mtu",
                interface_mtu=9232,
                node_selector=["eda.nokia.com/role=leaf"],
            )
        )
        result = _resolve_default_mtus(three_stage_intent)
        leaf_names = {n.name for n in three_stage_intent.nodes if n.role == "leaf"}
        for leaf in leaf_names:
            assert leaf in result
            assert result[leaf]["interface_mtu"] == 9232

    def test_explicit_nodes(self, three_stage_intent):
        """MTU with explicit node names should only target those nodes."""
        node_name = three_stage_intent.nodes[0].name
        three_stage_intent.default_mtus.append(
            DefaultMtuIntent(
                name="single-node-mtu",
                layer3_mtu=9198,
                nodes=[node_name],
            )
        )
        result = _resolve_default_mtus(three_stage_intent)
        assert node_name in result
        assert result[node_name]["layer3_mtu"] == 9198


# ---------------------------------------------------------------------------
# Service placement
# ---------------------------------------------------------------------------


class TestResolvePlacement:
    def test_leaves_get_irb_interfaces(self, three_stage_intent):
        """Leaves should receive IRB interface placements."""
        placement = _resolve_placement(three_stage_intent)
        leaf_names = {n.name for n in three_stage_intent.nodes if n.role == "leaf"}
        leaves_with_irbs = {
            name for name in leaf_names
            if name in placement and placement[name].irb_interfaces
        }
        assert len(leaves_with_irbs) > 0

    def test_spines_no_irb_interfaces(self, three_stage_intent):
        """Spines should not receive IRB interface placements."""
        placement = _resolve_placement(three_stage_intent)
        spine_names = {n.name for n in three_stage_intent.nodes if n.role == "spine"}
        for spine in spine_names:
            if spine in placement:
                assert len(placement[spine].irb_interfaces) == 0


# ---------------------------------------------------------------------------
# Host vars structure
# ---------------------------------------------------------------------------


class TestHostVars:
    def test_leaf_has_node_section(self, three_stage_intent):
        leaf = next(n for n in three_stage_intent.nodes if n.role == "leaf")
        placement = _resolve_placement(three_stage_intent)
        svc = placement.get(leaf.name)
        if svc is None:
            from automation.generators.ansible_generator import _NodeServices
            svc = _NodeServices()
        hv = _build_leaf_host_vars(leaf, three_stage_intent, svc)
        assert "node" in hv
        assert hv["node"]["hostname"] == leaf.name
        assert hv["node"]["role"] == "leaf"
        assert hv["node"]["asn"] == leaf.asn

    def test_leaf_has_node_labels(self, three_stage_intent):
        leaf = next(n for n in three_stage_intent.nodes if n.role == "leaf")
        placement = _resolve_placement(three_stage_intent)
        svc = placement.get(leaf.name)
        if svc is None:
            from automation.generators.ansible_generator import _NodeServices
            svc = _NodeServices()
        hv = _build_leaf_host_vars(leaf, three_stage_intent, svc)
        assert "labels" in hv["node"]
        assert hv["node"]["labels"]["eda.nokia.com/role"] == "leaf"

    def test_leaf_has_irb_interfaces(self, three_stage_intent):
        leaf = next(n for n in three_stage_intent.nodes if n.role == "leaf")
        placement = _resolve_placement(three_stage_intent)
        svc = placement.get(leaf.name)
        if svc is None:
            from automation.generators.ansible_generator import _NodeServices
            svc = _NodeServices()
        hv = _build_leaf_host_vars(leaf, three_stage_intent, svc)
        if svc.irb_interfaces:
            assert "irb_interfaces" in hv
            for irb in hv["irb_interfaces"]:
                assert "bridge_domain" in irb
                assert "router" in irb

    def test_leaf_no_bridge_domains_in_host_vars(self, three_stage_intent):
        """Bridge domains should be in group_vars, not host_vars."""
        leaf = next(n for n in three_stage_intent.nodes if n.role == "leaf")
        placement = _resolve_placement(three_stage_intent)
        svc = placement.get(leaf.name)
        if svc is None:
            from automation.generators.ansible_generator import _NodeServices
            svc = _NodeServices()
        hv = _build_leaf_host_vars(leaf, three_stage_intent, svc)
        assert "bridge_domains" not in hv
        assert "routers" not in hv

    def test_spine_has_node_section(self, three_stage_intent):
        spine = next(n for n in three_stage_intent.nodes if n.role == "spine")
        hv = _build_spine_host_vars(spine, three_stage_intent)
        assert "node" in hv
        assert hv["node"]["hostname"] == spine.name
        assert hv["node"]["role"] == "spine"
        assert hv["node"]["asn"] == spine.asn

    def test_spine_has_underlay_interfaces(self, three_stage_intent):
        spine = next(n for n in three_stage_intent.nodes if n.role == "spine")
        hv = _build_spine_host_vars(spine, three_stage_intent)
        assert "underlay_interfaces" in hv
        assert len(hv["underlay_interfaces"]) > 0
        for intf in hv["underlay_interfaces"]:
            assert "name" in intf
            assert "peer_asn" in intf

    def test_leaf_has_underlay_interfaces(self, three_stage_intent):
        leaf = next(n for n in three_stage_intent.nodes if n.role == "leaf")
        placement = _resolve_placement(three_stage_intent)
        svc = placement.get(leaf.name)
        if svc is None:
            from automation.generators.ansible_generator import _NodeServices
            svc = _NodeServices()
        hv = _build_leaf_host_vars(leaf, three_stage_intent, svc)
        assert "underlay_interfaces" in hv


# ---------------------------------------------------------------------------
# Spine overrides (MTU + configlets)
# ---------------------------------------------------------------------------


class TestSpineOverrides:
    def test_mtu_applied_to_spine(self, three_stage_intent):
        """DefaultMTU targeting spines should be included in spine host_vars."""
        spine = next(n for n in three_stage_intent.nodes if n.role == "spine")
        three_stage_intent.default_mtus.append(
            DefaultMtuIntent(
                name="spine-mtu",
                interface_mtu=9232,
                node_selector=["eda.nokia.com/role=spine"],
            )
        )
        result = _resolve_default_mtus(three_stage_intent)
        assert spine.name in result
        assert result[spine.name]["interface_mtu"] == 9232

    def test_configlet_applied_to_spine(self, three_stage_intent):
        """Extras configlets targeting spines should resolve to spine nodes."""
        spine = next(n for n in three_stage_intent.nodes if n.role == "spine")
        three_stage_intent.configlets.append(
            ConfigletIntent(
                name="spine-override",
                origin="extras",
                endpoint_selector=["eda.nokia.com/role=spine"],
                configs=[
                    ConfigletConfigEntry(
                        path=".system.information",
                        config='{"contact": "noc@example.com"}',
                    )
                ],
            )
        )
        result = _resolve_configlets(three_stage_intent)
        assert spine.name in result
        paths = [e["path"] for e in result[spine.name]]
        assert "/system/information" in paths


# ---------------------------------------------------------------------------
# End-to-end generate
# ---------------------------------------------------------------------------


class TestGroupVarsServices:
    def test_group_vars_has_bridge_domains(self, three_stage_intent, tmp_path):
        """group_vars/leafs.yml should contain shared bridge domain definitions."""
        generate(three_stage_intent, output_dir=tmp_path)
        gv = yaml.safe_load((tmp_path / "group_vars" / "leafs.yml").read_text())
        assert "bridge_domains" in gv
        assert len(gv["bridge_domains"]) == len(three_stage_intent.bridge_domains)
        for bd in gv["bridge_domains"]:
            assert "name" in bd
            assert "vni" in bd
            assert "evi" in bd
            assert "access" not in bd
            assert "irb" not in bd

    def test_group_vars_has_routers(self, three_stage_intent, tmp_path):
        """group_vars/leafs.yml should contain shared router definitions."""
        generate(three_stage_intent, output_dir=tmp_path)
        gv = yaml.safe_load((tmp_path / "group_vars" / "leafs.yml").read_text())
        assert "routers" in gv
        assert len(gv["routers"]) == len(three_stage_intent.routers)
        for r in gv["routers"]:
            assert "name" in r
            assert "vni" in r

    def test_group_vars_has_vlans(self, three_stage_intent, tmp_path):
        """group_vars/leafs.yml should contain shared VLAN definitions."""
        generate(three_stage_intent, output_dir=tmp_path)
        gv = yaml.safe_load((tmp_path / "group_vars" / "leafs.yml").read_text())
        assert "vlans" in gv
        assert len(gv["vlans"]) == len(three_stage_intent.vlans)
        for v in gv["vlans"]:
            assert "name" in v
            assert "bridge_domain" in v
            assert "vlan_id" in v

    def test_host_vars_no_bridge_domains(self, three_stage_intent, tmp_path):
        """host_vars should not contain bridge_domains (moved to group_vars)."""
        generate(three_stage_intent, output_dir=tmp_path)
        for node in three_stage_intent.nodes:
            if node.role != "leaf":
                continue
            hv_file = tmp_path / "host_vars" / f"{node.name}.yml"
            hv = yaml.safe_load(hv_file.read_text())
            assert "bridge_domains" not in hv
            assert "routers" not in hv


class TestGenerate:
    def test_generates_expected_files(self, three_stage_intent, tmp_path):
        output = generate(three_stage_intent, output_dir=tmp_path)
        assert output == tmp_path
        assert (tmp_path / "inventory.yml").exists()
        assert (tmp_path / "playbook.yml").exists()
        assert (tmp_path / "ansible.cfg").exists()
        assert (tmp_path / "requirements.yml").exists()
        assert (tmp_path / "README.md").exists()
        assert (tmp_path / "group_vars" / "all.yml").exists()
        assert (tmp_path / "group_vars" / "leafs.yml").exists()
        assert (tmp_path / "group_vars" / "spines.yml").exists()
        assert (tmp_path / "host_vars").is_dir()

    def test_inventory_structure(self, three_stage_intent, tmp_path):
        generate(three_stage_intent, output_dir=tmp_path)
        inv = yaml.safe_load((tmp_path / "inventory.yml").read_text())
        assert "all" in inv
        children = inv["all"].get("children", {})
        assert "leafs" in children
        assert "spines" in children

    def test_host_vars_per_node(self, three_stage_intent, tmp_path):
        generate(three_stage_intent, output_dir=tmp_path)
        for node in three_stage_intent.nodes:
            hv_file = tmp_path / "host_vars" / f"{node.name}.yml"
            assert hv_file.exists(), f"Missing host_vars for {node.name}"
            hv = yaml.safe_load(hv_file.read_text())
            assert hv["node"]["hostname"] == node.name

    def test_playbook_has_timestamp(self, three_stage_intent, tmp_path):
        generate(three_stage_intent, output_dir=tmp_path)
        content = (tmp_path / "playbook.yml").read_text()
        assert content.startswith("# Generated by NVD automation")

    def test_readme_has_staleness_warning(self, three_stage_intent, tmp_path):
        generate(three_stage_intent, output_dir=tmp_path)
        readme = (tmp_path / "README.md").read_text()
        assert "Generated at:" in readme
        assert "re-generate" in readme
