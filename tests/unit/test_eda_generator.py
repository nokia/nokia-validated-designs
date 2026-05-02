"""Unit tests for automation.generators.eda_generator."""

import pytest
from pathlib import Path

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.core.models import FabricIntent
from automation.generators.eda_generator import generate, MANAGED_BY_LABEL, MANAGED_BY_VALUE, NVD_DESIGN_LABEL
from automation.eda_models.registry import BY_KIND


DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


@pytest.fixture()
def three_stage_intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def three_stage_crs(three_stage_intent) -> list[dict]:
    return generate(three_stage_intent)


class TestEdaGenerator:
    def test_generates_crs(self, three_stage_crs):
        assert len(three_stage_crs) > 0

    def test_all_crs_have_required_fields(self, three_stage_crs):
        for cr in three_stage_crs:
            assert "apiVersion" in cr
            assert "kind" in cr
            assert "metadata" in cr
            assert "spec" in cr
            assert "name" in cr["metadata"]
            assert "namespace" in cr["metadata"]

    def test_managed_by_label_present(self, three_stage_crs):
        for cr in three_stage_crs:
            labels = cr["metadata"].get("labels", {})
            assert labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE, (
                f"CR {cr['kind']}/{cr['metadata']['name']} missing managed-by label"
            )

    def test_design_label_uses_intent_design(self, three_stage_crs):
        """Verify origin labels reflect intent.design for CRs without builder-set origin.

        CRs like BridgeDomain/IRBInterface have an ``origin`` field set by
        the builder (e.g. "3-stage", "extras") for provenance tracking within
        a design. Those are propagated as-is. CRs without a builder origin
        should use ``intent.design``.
        """
        builder_origin_kinds = {"BridgeDomain", "IRBInterface", "Configlet"}
        for cr in three_stage_crs:
            labels = cr["metadata"].get("labels", {})
            design_label = labels.get(NVD_DESIGN_LABEL, "")
            if design_label and cr["kind"] not in builder_origin_kinds:
                assert design_label == "3-stage-evpn-vxlan", (
                    f"CR {cr['kind']}/{cr['metadata']['name']} has wrong design label: {design_label}"
                )

    def test_no_duplicate_crs(self, three_stage_crs):
        keys = set()
        for cr in three_stage_crs:
            key = f"{cr['kind']}:{cr['metadata']['name']}"
            assert key not in keys, f"Duplicate CR: {key}"
            keys.add(key)

    def test_api_versions_match_registry(self, three_stage_crs):
        """All generated CRs should use apiVersions from the central registry."""
        for cr in three_stage_crs:
            kind = cr["kind"]
            if kind in BY_KIND:
                expected_av = BY_KIND[kind].api_version
                assert cr["apiVersion"] == expected_av, (
                    f"CR {kind}/{cr['metadata']['name']} has apiVersion "
                    f"{cr['apiVersion']} but registry says {expected_av}"
                )

    def test_namespace_from_intent(self, three_stage_intent):
        three_stage_intent.eda.namespace = "custom-ns"
        crs = generate(three_stage_intent)
        for cr in crs:
            assert cr["metadata"]["namespace"] == "custom-ns", (
                f"CR {cr['kind']}/{cr['metadata']['name']} has wrong namespace"
            )

    def test_expected_kinds(self, three_stage_crs):
        kinds = {cr["kind"] for cr in three_stage_crs}
        expected = {
            "Init", "NodeUser", "NodeProfile", "TopoNode",
            "Interface", "TopoLink", "IndexAllocationPool",
            "IPAllocationPool", "Fabric", "BridgeDomain",
            "Router", "IRBInterface", "VLAN",
        }
        for kind in expected:
            assert kind in kinds, f"Expected kind {kind} not found in generated CRs"

    def test_writes_json_to_output_dir(self, three_stage_intent, tmp_path):
        crs = generate(three_stage_intent, output_dir=tmp_path)
        assert (tmp_path / "eda_transaction.json").exists()
        assert len(crs) > 0


class TestEdaGeneratorDesignAgnostic:
    """Verify the generator correctly uses intent.design for labeling."""

    def test_unconstrained_design_label(self):
        design_dir = DESIGNS_ROOT / "unconstrained-3-stage"
        if not design_dir.exists():
            pytest.skip("unconstrained-3-stage design not found")
        topo, svc = load_inputs(design_dir)
        intent = build_intent(topo, svc)
        crs = generate(intent)
        for cr in crs:
            labels = cr["metadata"].get("labels", {})
            design_label = labels.get(NVD_DESIGN_LABEL, "")
            if design_label:
                assert design_label == "unconstrained-3-stage"


class TestRouteTargetOverrides:
    """Verify explicit export/import route targets land in Router + BridgeDomain CRs."""

    def test_router_cr_carries_overridden_rts(self, three_stage_intent):
        three_stage_intent.routers[0].export_target = "target:1:777"
        three_stage_intent.routers[0].import_target = "target:2:888"
        router_name = three_stage_intent.routers[0].name
        crs = generate(three_stage_intent)
        r_cr = next(
            cr for cr in crs
            if cr["kind"] == "Router" and cr["metadata"]["name"] == router_name
        )
        assert r_cr["spec"]["exportTarget"] == "target:1:777"
        assert r_cr["spec"]["importTarget"] == "target:2:888"

    def test_router_cr_omits_rts_when_unset(self, three_stage_intent):
        # Pick a router without explicit RTs (fresh fixture).
        router_name = three_stage_intent.routers[0].name
        crs = generate(three_stage_intent)
        r_cr = next(
            cr for cr in crs
            if cr["kind"] == "Router" and cr["metadata"]["name"] == router_name
        )
        assert "exportTarget" not in r_cr["spec"]
        assert "importTarget" not in r_cr["spec"]

    def test_bridge_domain_cr_carries_overridden_rts(self, three_stage_intent):
        bd = three_stage_intent.bridge_domains[0]
        bd.export_target = "target:1:777"
        bd.import_target = "target:2:888"
        crs = generate(three_stage_intent)
        bd_cr = next(
            cr for cr in crs
            if cr["kind"] == "BridgeDomain" and cr["metadata"]["name"] == bd.name
        )
        assert bd_cr["spec"]["exportTarget"] == "target:1:777"
        assert bd_cr["spec"]["importTarget"] == "target:2:888"

    def test_bridge_domain_cr_omits_rts_when_unset(self, three_stage_intent):
        bd_name = three_stage_intent.bridge_domains[0].name
        crs = generate(three_stage_intent)
        bd_cr = next(
            cr for cr in crs
            if cr["kind"] == "BridgeDomain" and cr["metadata"]["name"] == bd_name
        )
        assert "exportTarget" not in bd_cr["spec"]
        assert "importTarget" not in bd_cr["spec"]
