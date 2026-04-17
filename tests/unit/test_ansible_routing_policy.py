"""Unit tests for the Ansible routing-policy pipeline.

Verifies that:
  - ``_bgp_gv()`` emits the intent-derived ``routing_policy`` block.
  - The SRL builders render the same ``/routing-policy`` JSON-RPC payload
    whether fed via intent-derived host_vars or the legacy hardcoded path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.generators.ansible_filter_plugins.srl_builders import (
    default as srl_default,
    v25_3,
    v26,
)
from automation.generators.ansible_generator import _bgp_gv

DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


@pytest.fixture()
def intent():
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def new_hv(intent):
    """host_vars-like dict derived from the intent (modern path)."""
    gv = _bgp_gv(intent, multipath_max_paths=2)
    rp = gv["routing_policy"]
    return {
        "fabric_name": intent.fabric_name,
        "system0_prefix": intent.system0_prefix,
        "routing_policy": rp,
    }


@pytest.fixture()
def legacy_hv(intent):
    """host_vars-like dict *without* the new intent-derived keys (fallback path)."""
    return {
        "fabric_name": intent.fabric_name,
        "system0_prefix": intent.system0_prefix,
        "routing_policy": {"prefix_set": f"prefixset-{intent.fabric_name}"},
    }


class TestBgpGroupVars:
    def test_emits_policy_names(self, intent):
        gv = _bgp_gv(intent, multipath_max_paths=2)
        rp = gv["routing_policy"]
        assert rp["prefix_set"] == "prefixset-dc1"
        assert rp["export_policy"] == "ebgp-isl-export-policy-dc1"
        assert rp["import_policy"] == "ebgp-isl-import-policy-dc1"

    def test_emits_full_policy_definitions(self, intent):
        gv = _bgp_gv(intent, multipath_max_paths=2)
        rp = gv["routing_policy"]
        assert len(rp["prefix_sets"]) == 1
        assert rp["prefix_sets"][0]["name"] == "prefixset-dc1"
        assert len(rp["policies"]) == 2
        assert {p["name"] for p in rp["policies"]} == {
            "ebgp-isl-export-policy-dc1",
            "ebgp-isl-import-policy-dc1",
        }


@pytest.mark.parametrize(
    "builder", [srl_default, v25_3, v26], ids=["24.10", "25.3", "26.x"]
)
class TestIntentOutputMatchesLegacy:
    def test_routing_policy_byte_parity(self, builder, new_hv, legacy_hv):
        """Intent-derived render must be byte-identical to the legacy render."""
        assert builder.build_routing_policy_updates(
            new_hv
        ) == builder.build_routing_policy_updates(legacy_hv)


class TestVersionSpecificShapes:
    def test_v24_flat_prefix_set(self, new_hv):
        out = srl_default.build_routing_policy_updates(new_hv)
        stmt10 = out[0]["value"]["policy"][0]["statement"][0]
        assert stmt10["match"] == {"prefix-set": "prefixset-dc1", "protocol": "local"}
        assert stmt10["action"]["bgp"]["local-preference"] == {"set": 100}

    def test_v25_nested_prefix_set(self, new_hv):
        out = v25_3.build_routing_policy_updates(new_hv)
        stmt10 = out[0]["value"]["policy"][0]["statement"][0]
        assert stmt10["match"] == {
            "prefix": {"prefix-set": "prefixset-dc1"},
            "protocol": "local",
        }
        assert stmt10["action"]["bgp"]["local-preference"] == {"set": 100}

    def test_v26_local_pref_shape(self, new_hv):
        out = v26.build_routing_policy_updates(new_hv)
        stmt10 = out[0]["value"]["policy"][0]["statement"][0]
        assert stmt10["action"]["bgp"]["local-preference"] == {
            "value": 100,
            "operation": "set",
        }


class TestLegacyFallbackStillWorks:
    """The SRL builders are also called outside NVD — the legacy hardcoded
    path must still produce the 3-stage EVPN policy when ``policies`` /
    ``prefix_sets`` are absent from host_vars."""

    def test_legacy_hv_produces_expected_policies(self, legacy_hv):
        out = srl_default.build_routing_policy_updates(legacy_hv)
        policy_names = [p["name"] for p in out[0]["value"]["policy"]]
        assert policy_names == [
            "ebgp-isl-export-policy-dc1",
            "ebgp-isl-import-policy-dc1",
        ]
