"""Unit tests for EDA generator routing-policy emission.

The 3-stage design auto-creates default underlay routing policies with
``internal=True``. These are handled natively by EDA's Fabric reconciler
and must NOT be emitted as standalone ``Policy``/``PrefixSet`` CRs, nor
referenced by ``Fabric.spec.underlayProtocol.bgp.exportPolicy``.

User-declared policies (``internal=False``) are emitted as CRs and may be
referenced by the Fabric CR or consumed by service CRs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.models import (
    PolicyAction,
    PolicyMatch,
    PolicyStatementIntent,
    PrefixEntry,
    PrefixSetIntent,
    RoutingPolicyIntent,
)
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


# ---------------------------------------------------------------------------
# Default (internal) policies are NOT emitted for the 3-stage design.
# Reference: validated-designs/.../3-stage-evpn-vxlan-clab-with-eda/fabric.yaml
# has no Policy/PrefixSet CRs and no exportPolicy/importPolicy on Fabric.
# ---------------------------------------------------------------------------


class TestInternalDefaultsNotEmitted:
    def test_no_prefix_set_cr(self, crs):
        assert [c for c in crs if c["kind"] == "PrefixSet"] == []

    def test_no_policy_cr(self, crs):
        assert [c for c in crs if c["kind"] == "Policy"] == []

    def test_fabric_has_no_bgp_policy_refs(self, crs):
        fabric = next(c for c in crs if c["kind"] == "Fabric")
        bgp = fabric["spec"]["underlayProtocol"]["bgp"]
        assert "exportPolicy" not in bgp
        assert "importPolicy" not in bgp


# ---------------------------------------------------------------------------
# CR-shape validation: build models directly (internal=False) and check
# that _cr_prefix_set / _cr_policy produce the expected JSON.
# ---------------------------------------------------------------------------


class TestPrefixSetCR:
    def test_exact_mask_range_maps_to_exact_flag(self):
        ps = PrefixSetIntent(
            name="ps-exact",
            prefixes=[PrefixEntry(ip_prefix="10.0.0.0/24", mask_length_range="exact")],
        )
        cr = _cr_prefix_set(ps, "eda", "test")
        assert cr["spec"]["prefix"] == [{"prefix": "10.0.0.0/24", "exact": True}]

    def test_range_encoding(self):
        ps = PrefixSetIntent(
            name="ps-range",
            prefixes=[PrefixEntry(ip_prefix="192.168.254.0/24", mask_length_range="32..32")],
        )
        cr = _cr_prefix_set(ps, "eda", "test")
        assert cr["spec"]["prefix"] == [
            {"prefix": "192.168.254.0/24", "startRange": 32, "endRange": 32}
        ]


def _sample_export_policy() -> RoutingPolicyIntent:
    accept = PolicyAction(result="accept", set_local_preference=100)
    return RoutingPolicyIntent(
        name="user-export",
        default_action="reject",
        statements=[
            PolicyStatementIntent(
                name="10",
                match=PolicyMatch(prefix_set="user-ps", protocol="local"),
                action=accept,
            ),
            PolicyStatementIntent(
                name="25",
                match=PolicyMatch(bgp_evpn_route_types=[1]),
                action=accept,
            ),
        ],
    )


class TestPolicyCR:
    def test_protocol_uppercased(self):
        cr = _cr_policy(_sample_export_policy(), "eda", "test")
        stmt10 = next(s for s in cr["spec"]["statement"] if s["name"] == "10")
        assert stmt10["match"]["protocol"] == "LOCAL"
        assert stmt10["match"]["prefixSet"] == "user-ps"

    def test_evpn_route_types_under_match_bgp(self):
        cr = _cr_policy(_sample_export_policy(), "eda", "test")
        stmt25 = next(s for s in cr["spec"]["statement"] if s["name"] == "25")
        assert stmt25["match"]["bgp"]["evpnRouteType"] == [1]

    def test_action_local_preference(self):
        cr = _cr_policy(_sample_export_policy(), "eda", "test")
        stmt10 = next(s for s in cr["spec"]["statement"] if s["name"] == "10")
        assert stmt10["action"]["policyResult"] == "accept"
        assert stmt10["action"]["bgp"]["localPreference"] == 100

    def test_default_action_reject(self):
        cr = _cr_policy(_sample_export_policy(), "eda", "test")
        assert cr["spec"]["defaultAction"]["policyResult"] == "reject"


# ---------------------------------------------------------------------------
# User-declared (non-internal) policies: emission + Fabric wiring.
# ---------------------------------------------------------------------------


class TestUserDeclaredPoliciesEmitted:
    def test_user_policy_in_output(self, intent):
        user_ps = PrefixSetIntent(
            name="user-ps",
            prefixes=[PrefixEntry(ip_prefix="10.0.0.0/24", mask_length_range="exact")],
        )
        user_policy = RoutingPolicyIntent(
            name="user-policy",
            default_action="accept",
            statements=[
                PolicyStatementIntent(
                    name="10",
                    match=PolicyMatch(prefix_set="user-ps"),
                    action=PolicyAction(result="accept"),
                )
            ],
        )
        intent.prefix_sets = list(intent.prefix_sets) + [user_ps]
        intent.routing_policies = list(intent.routing_policies) + [user_policy]

        out = generate(intent)
        ps_names = {c["metadata"]["name"] for c in out if c["kind"] == "PrefixSet"}
        pol_names = {c["metadata"]["name"] for c in out if c["kind"] == "Policy"}
        assert ps_names == {"user-ps"}
        assert pol_names == {"user-policy"}

    def test_fabric_references_only_user_declared_policies(self, intent):
        user_policy = RoutingPolicyIntent(
            name="user-export",
            default_action="accept",
            statements=[],
        )
        intent.routing_policies = list(intent.routing_policies) + [user_policy]
        intent.fabric_export_policies = list(intent.fabric_export_policies) + ["user-export"]

        out = generate(intent)
        fabric = next(c for c in out if c["kind"] == "Fabric")
        bgp = fabric["spec"]["underlayProtocol"]["bgp"]
        # internal default 'ebgp-isl-export-policy-dc1' filtered out; only user-export remains.
        assert bgp["exportPolicy"] == ["user-export"]
        # import_policy still references only an internal default → filtered to None.
        assert "importPolicy" not in bgp


class TestEmissionOrder:
    """When user policies are declared, PrefixSet/Policy come before Fabric."""

    def test_user_prefix_set_before_policy_before_fabric(self, intent):
        user_ps = PrefixSetIntent(name="user-ps", prefixes=[])
        user_policy = RoutingPolicyIntent(name="user-policy", statements=[])
        intent.prefix_sets = list(intent.prefix_sets) + [user_ps]
        intent.routing_policies = list(intent.routing_policies) + [user_policy]

        out = generate(intent)
        kinds = [c["kind"] for c in out]
        ps_idx = kinds.index("PrefixSet")
        pol_idx = kinds.index("Policy")
        fabric_idx = kinds.index("Fabric")
        assert ps_idx < pol_idx < fabric_idx
