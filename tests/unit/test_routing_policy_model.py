"""Unit tests for the routing-policy intent models + constrained design defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.models import (
    FabricIntent,
    NodeIntent,
    PolicyAction,
    PolicyMatch,
    PolicyStatementIntent,
    PrefixEntry,
    PrefixSetIntent,
    RoutingPolicyIntent,
)
from automation.core.schema_validator import load_inputs

DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


class TestRoutingPolicyDefaults:
    def test_statement_defaults(self):
        stmt = PolicyStatementIntent(name="10")
        assert stmt.match.prefix_set is None
        assert stmt.match.protocol is None
        assert stmt.match.bgp_evpn_route_types is None
        assert stmt.action.result == "accept"
        assert stmt.action.set_local_preference is None

    def test_routing_policy_defaults_to_reject(self):
        rp = RoutingPolicyIntent(name="p1")
        assert rp.default_action == "reject"
        assert rp.statements == []

    def test_prefix_entry_defaults_to_exact(self):
        p = PrefixEntry(ip_prefix="10.0.0.0/24")
        assert p.mask_length_range == "exact"


class TestRoutingPolicyCrossReferences:
    def _fabric(self, **overrides) -> dict:
        base = {
            "design": "test",
            "fabric_name": "dc1",
            "environment": "containerlab",
            "spine_asn": 65500,
            "leaf_asn_start": 65400,
            "system0_prefix": "192.168.254.0/24",
            "nodes": [
                NodeIntent(
                    name="leaf1",
                    role="leaf",
                    platform="7220 IXR-D3L",
                    version="25.10.2",
                    asn=65401,
                    system0_ipv4="192.168.254.11/32",
                    mgmt_ipv4="172.21.21.11",
                )
            ],
            "links": [],
        }
        base.update(overrides)
        return base

    def test_policy_prefix_set_ref_must_exist(self):
        with pytest.raises(ValueError, match="unknown prefix_set 'missing'"):
            FabricIntent(
                **self._fabric(
                    routing_policies=[
                        RoutingPolicyIntent(
                            name="p",
                            statements=[
                                PolicyStatementIntent(
                                    name="10",
                                    match=PolicyMatch(prefix_set="missing"),
                                )
                            ],
                        )
                    ],
                )
            )

    def test_fabric_export_policy_ref_must_exist(self):
        with pytest.raises(ValueError, match="fabric_export_policies"):
            FabricIntent(
                **self._fabric(fabric_export_policies=["nope"]),
            )

    def test_fabric_import_policy_ref_must_exist(self):
        with pytest.raises(ValueError, match="fabric_import_policies"):
            FabricIntent(
                **self._fabric(fabric_import_policies=["nope"]),
            )

    def test_valid_references_pass(self):
        fi = FabricIntent(
            **self._fabric(
                prefix_sets=[
                    PrefixSetIntent(
                        name="ps",
                        prefixes=[PrefixEntry(ip_prefix="10.0.0.0/24")],
                    )
                ],
                routing_policies=[
                    RoutingPolicyIntent(
                        name="p",
                        statements=[
                            PolicyStatementIntent(
                                name="10",
                                match=PolicyMatch(prefix_set="ps"),
                            )
                        ],
                    )
                ],
                fabric_export_policies=["p"],
                fabric_import_policies=["p"],
            )
        )
        assert fi.fabric_export_policies == ["p"]


class TestConstrainedDesignDefaults:
    @pytest.fixture()
    def intent(self) -> FabricIntent:
        design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
        if not design_dir.exists():
            pytest.skip("3-stage-evpn-vxlan design not found")
        topo, svc = load_inputs(design_dir)
        return build_intent(topo, svc)

    def test_default_prefix_set_generated(self, intent):
        assert any(ps.name == "prefixset-dc1" for ps in intent.prefix_sets)
        ps = next(ps for ps in intent.prefix_sets if ps.name == "prefixset-dc1")
        assert ps.prefixes[0].ip_prefix == "192.168.254.0/24"
        assert ps.prefixes[0].mask_length_range == "32..32"

    def test_default_policies_generated(self, intent):
        names = {rp.name for rp in intent.routing_policies}
        assert "ebgp-isl-export-policy-dc1" in names
        assert "ebgp-isl-import-policy-dc1" in names

    def test_export_policy_statements(self, intent):
        rp = next(
            r for r in intent.routing_policies if r.name == "ebgp-isl-export-policy-dc1"
        )
        assert rp.default_action == "reject"
        stmt_names = [s.name for s in rp.statements]
        assert stmt_names == ["10", "15", "20", "25", "30", "35", "40", "45"]
        # All statements set local-pref=100
        assert all(s.action.set_local_preference == 100 for s in rp.statements)

    def test_import_policy_statements(self, intent):
        rp = next(
            r for r in intent.routing_policies if r.name == "ebgp-isl-import-policy-dc1"
        )
        stmt_names = [s.name for s in rp.statements]
        assert stmt_names == ["10", "25", "30", "35", "40", "45"]

    def test_fabric_policy_refs_set(self, intent):
        assert intent.fabric_export_policies == ["ebgp-isl-export-policy-dc1"]
        assert intent.fabric_import_policies == ["ebgp-isl-import-policy-dc1"]
