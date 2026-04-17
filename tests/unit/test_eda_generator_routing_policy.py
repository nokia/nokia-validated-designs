"""Unit tests for EDA generator routing-policy emission.

Verifies that the EDA generator produces correctly-shaped ``PrefixSet`` and
``Policy`` CRs and wires the ``Fabric`` CR to reference them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.generators.eda_generator import (
    _cr_policy,
    _cr_prefix_set,
    generate,
)

DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


@pytest.fixture()
def intent():
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def crs(intent):
    return generate(intent)


class TestPrefixSetCR:
    def test_prefix_set_emitted(self, crs):
        ps = [c for c in crs if c["kind"] == "PrefixSet"]
        assert len(ps) == 1
        assert ps[0]["metadata"]["name"] == "prefixset-dc1"

    def test_prefix_set_range_encoding(self, crs):
        ps = next(c for c in crs if c["kind"] == "PrefixSet")
        prefixes = ps["spec"]["prefix"]
        assert prefixes == [
            {
                "prefix": "192.168.254.0/24",
                "startRange": 32,
                "endRange": 32,
            }
        ]

    def test_exact_mask_range_maps_to_exact_flag(self, intent):
        from automation.core.models import PrefixEntry, PrefixSetIntent

        ps = PrefixSetIntent(
            name="ps-exact",
            prefixes=[PrefixEntry(ip_prefix="10.0.0.0/24", mask_length_range="exact")],
        )
        cr = _cr_prefix_set(ps, "eda", "test")
        assert cr["spec"]["prefix"] == [{"prefix": "10.0.0.0/24", "exact": True}]


class TestPolicyCR:
    def test_two_policies_emitted(self, crs):
        policies = [c for c in crs if c["kind"] == "Policy"]
        names = {c["metadata"]["name"] for c in policies}
        assert names == {"ebgp-isl-export-policy-dc1", "ebgp-isl-import-policy-dc1"}

    def test_protocol_uppercased(self, crs):
        export = next(
            c
            for c in crs
            if c["kind"] == "Policy"
            and c["metadata"]["name"] == "ebgp-isl-export-policy-dc1"
        )
        stmt10 = next(s for s in export["spec"]["statement"] if s["name"] == "10")
        assert stmt10["match"]["protocol"] == "LOCAL"
        assert stmt10["match"]["prefixSet"] == "prefixset-dc1"

    def test_evpn_route_types_under_match_bgp(self, crs):
        export = next(
            c
            for c in crs
            if c["kind"] == "Policy"
            and c["metadata"]["name"] == "ebgp-isl-export-policy-dc1"
        )
        stmt25 = next(s for s in export["spec"]["statement"] if s["name"] == "25")
        assert stmt25["match"]["bgp"]["evpnRouteType"] == [1]

    def test_action_local_preference(self, crs):
        export = next(
            c
            for c in crs
            if c["kind"] == "Policy"
            and c["metadata"]["name"] == "ebgp-isl-export-policy-dc1"
        )
        stmt10 = next(s for s in export["spec"]["statement"] if s["name"] == "10")
        assert stmt10["action"]["policyResult"] == "accept"
        assert stmt10["action"]["bgp"]["localPreference"] == 100

    def test_default_action_reject(self, crs):
        export = next(
            c
            for c in crs
            if c["kind"] == "Policy"
            and c["metadata"]["name"] == "ebgp-isl-export-policy-dc1"
        )
        assert export["spec"]["defaultAction"]["policyResult"] == "reject"


class TestFabricReferencesPolicies:
    def test_fabric_export_import_policy_set(self, crs):
        fabric = next(c for c in crs if c["kind"] == "Fabric")
        bgp = fabric["spec"]["underlayProtocol"]["bgp"]
        assert bgp["exportPolicy"] == ["ebgp-isl-export-policy-dc1"]
        assert bgp["importPolicy"] == ["ebgp-isl-import-policy-dc1"]


class TestEmissionOrder:
    """PrefixSets must be emitted before Policies, and both before Fabric."""

    def test_prefix_set_before_policy(self, crs):
        kinds = [c["kind"] for c in crs]
        ps_idx = kinds.index("PrefixSet")
        pol_idx = kinds.index("Policy")
        fabric_idx = kinds.index("Fabric")
        assert ps_idx < pol_idx < fabric_idx
