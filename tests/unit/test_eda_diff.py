"""Unit tests for the content-aware EDA diff (creates/updates/deletes/unchanged)."""

from __future__ import annotations

import argparse
import copy

from automation import deploy as deploy_mod
from automation.core.models import FabricIntent
from automation.eda_models.profiles import DEFAULT_EDA_VERSION
from automation.executors.eda import (
    EdaClient,
    cr_matches_live_state,
)


def _cr(kind: str, name: str, spec: dict, labels: dict | None = None) -> dict:
    return {
        "apiVersion": "services.eda.nokia.com/v1alpha1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "eda",
            "labels": labels or {"eda.nokia.com/managed-by": "nvd-automation"},
        },
        "spec": spec,
    }


def _live(cr: dict, **spec_extra) -> dict:
    """Turn a desired CR into what EDA would hand back on a GET."""
    live = copy.deepcopy(cr)
    live["_kind"] = cr["kind"]
    live["_apiVersion"] = cr["apiVersion"]
    live["metadata"].update(
        {
            "resourceVersion": "4711",
            "generation": 3,
            "creationTimestamp": "2026-08-18T12:00:00Z",
            "uid": "8f1e0d6c-0000-4000-8000-000000000000",
        }
    )
    live["status"] = {"health": "up", "operationalState": "up"}
    live["spec"].update(spec_extra)
    return live


class TestCrMatchesLiveState:
    def test_identical_cr_matches(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101, "evi": 10})
        assert cr_matches_live_state(cr, _live(cr))

    def test_server_managed_metadata_and_status_ignored(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr)
        assert "resourceVersion" in live["metadata"] and "status" in live
        assert cr_matches_live_state(cr, live)

    def test_eda_populated_spec_defaults_ignored(self):
        """Fields the generator never declared are EDA's business."""
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr, macLearning=True, macAging=300, type="EVPNVXLAN")
        assert cr_matches_live_state(cr, live)

    def test_declared_field_with_different_value_is_a_change(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr)
        live["spec"]["vni"] = 100102
        assert not cr_matches_live_state(cr, live)

    def test_declared_field_missing_from_live_is_a_change(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101, "evi": 10})
        live = _live(cr)
        del live["spec"]["evi"]
        assert not cr_matches_live_state(cr, live)

    def test_missing_label_is_a_change(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr)
        live["metadata"]["labels"] = {}
        assert not cr_matches_live_state(cr, live)

    def test_extra_live_label_is_not_a_change(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr)
        live["metadata"]["labels"]["eda.nokia.com/something-else"] = "x"
        assert cr_matches_live_state(cr, live)

    def test_reordered_scalar_list_is_not_a_change(self):
        cr = _cr(
            "VLAN",
            "untagged-v10",
            {"interfaceSelector": ["a=1", "b=2"], "bridgeDomain": "macvrf-v10"},
        )
        live = _live(cr)
        live["spec"]["interfaceSelector"] = ["b=2", "a=1"]
        assert cr_matches_live_state(cr, live)

    def test_scalar_list_membership_change_is_a_change(self):
        cr = _cr("VLAN", "untagged-v10", {"interfaceSelector": ["a=1", "b=2"]})
        live = _live(cr)
        live["spec"]["interfaceSelector"] = ["a=1", "c=3"]
        assert not cr_matches_live_state(cr, live)

    def test_object_list_length_change_is_a_change(self):
        cr = _cr(
            "Policy",
            "p1",
            {"statements": [{"name": "10"}, {"name": "20"}]},
        )
        live = _live(cr)
        live["spec"]["statements"] = [{"name": "10"}]
        assert not cr_matches_live_state(cr, live)

    def test_object_list_with_eda_defaults_matches(self):
        cr = _cr("Policy", "p1", {"statements": [{"name": "10"}]})
        live = _live(cr)
        live["spec"]["statements"] = [{"name": "10", "action": {"result": "accept"}}]
        assert cr_matches_live_state(cr, live)

    def test_nodeuser_password_is_not_compared(self):
        """EDA stores the credential hashed and never returns the plaintext."""
        cr = _cr("NodeUser", "admin", {"username": "admin", "password": "NokiaSrl1!"})
        live = _live(cr)
        live["spec"]["password"] = "$6$rounds=…$hashed"
        assert cr_matches_live_state(cr, live)

    def test_nodeuser_username_is_still_compared(self):
        cr = _cr("NodeUser", "admin", {"username": "admin", "password": "x"})
        live = _live(cr)
        live["spec"]["username"] = "someone-else"
        assert not cr_matches_live_state(cr, live)

    def test_nodeprofile_enriched_fields_are_not_compared(self):
        """_enrich_node_profiles overwrites these from EDA's reference profile."""
        cr = _cr(
            "NodeProfile",
            "clab-srlinux-25.10.1",
            {
                "version": "25.10.1",
                "yang": "https://generated/srlinux-25.10.1.zip",
                "versionMatch": "v25\\.10\\.1.*",
                "port": 57410,
            },
        )
        live = _live(cr)
        live["spec"]["yang"] = "https://eda-asvr/real-profile.zip"
        live["spec"]["versionMatch"] = "something-else"
        assert cr_matches_live_state(cr, live)

    def test_nodeprofile_other_fields_still_compared(self):
        cr = _cr("NodeProfile", "np", {"version": "25.10.1", "port": 57410})
        live = _live(cr)
        live["spec"]["port"] = 57400
        assert not cr_matches_live_state(cr, live)


class TestComputeDiff:
    def _client(self) -> EdaClient:
        return EdaClient(url="https://eda.example.com")

    def test_converged_state_plans_no_operations(self):
        desired = [
            _cr("BridgeDomain", "macvrf-v10", {"vni": 100101}),
            _cr("Router", "vrf1", {"type": "EVPNVXLAN"}),
        ]
        current = [_live(cr, macLearning=True) for cr in desired]

        plan = self._client().compute_diff(desired, current)

        assert plan.total_ops == 0
        assert len(plan.unchanged) == 2
        assert plan.creates == [] and plan.updates == [] and plan.deletes == []

    def test_missing_resource_is_a_create(self):
        desired = [_cr("BridgeDomain", "macvrf-v10", {"vni": 100101})]

        plan = self._client().compute_diff(desired, [])

        assert len(plan.creates) == 1
        assert plan.total_ops == 1

    def test_changed_resource_is_an_update(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        live = _live(cr)
        live["spec"]["vni"] = 999

        plan = self._client().compute_diff([cr], [live])

        assert len(plan.updates) == 1
        assert plan.unchanged == []

    def test_stale_resource_is_a_delete(self):
        cr = _cr("BridgeDomain", "macvrf-v10", {"vni": 100101})
        stale = _live(_cr("BridgeDomain", "macvrf-v99", {"vni": 100199}))

        plan = self._client().compute_diff([cr], [_live(cr), stale])

        assert len(plan.deletes) == 1
        assert plan.deletes[0]["name"] == "macvrf-v99"
        assert plan.deletes[0]["kind"] == "BridgeDomain"
        assert len(plan.unchanged) == 1

    def test_counts_cover_every_desired_cr(self):
        """apply() submits creates + updates + unchanged, so none may be lost."""
        unchanged = _cr("BridgeDomain", "bd-same", {"vni": 1})
        changed = _cr("BridgeDomain", "bd-diff", {"vni": 2})
        new = _cr("BridgeDomain", "bd-new", {"vni": 3})
        changed_live = _live(changed)
        changed_live["spec"]["vni"] = 22

        desired = [unchanged, changed, new]
        plan = self._client().compute_diff(desired, [_live(unchanged), changed_live])

        counts = plan.counts()
        assert counts == {
            "creates": 1,
            "updates": 1,
            "deletes": 0,
            "unchanged": 1,
        }
        assert len(plan.creates + plan.updates + plan.unchanged) == len(desired)


class TestFailOnDiff:
    """--diff exit-code contract: 0 converged, 2 drift (with --fail-on-diff)."""

    def _intent(self) -> FabricIntent:
        return FabricIntent(
            design="3-stage-evpn-vxlan",
            fabric_name="fabric",
            environment="containerlab",
            spine_asn=65100,
            leaf_asn_start=65101,
            system0_prefix="10.0.0.0/24",
        )

    def _run(self, monkeypatch, current: list[dict], fail_on_diff: bool) -> tuple:
        monkeypatch.setattr(
            EdaClient, "authenticate", lambda self, *a, **kw: True, raising=False
        )
        monkeypatch.setattr(
            EdaClient, "get_managed_resources", lambda self, **kw: current
        )
        args = argparse.Namespace(
            eda_url="https://eda.example.com",
            eda_user="admin",
            eda_password="admin",
            eda_version=DEFAULT_EDA_VERSION,
            fail_on_diff=fail_on_diff,
        )
        desired = [_cr("BridgeDomain", "macvrf-v10", {"vni": 100101})]
        summary: dict = {"errors": []}
        rc = deploy_mod._run_diff(args, self._intent(), desired, summary)
        return rc, summary

    def test_converged_exits_zero(self, monkeypatch):
        live = _live(_cr("BridgeDomain", "macvrf-v10", {"vni": 100101}))
        rc, summary = self._run(monkeypatch, [live], fail_on_diff=True)
        assert rc == 0
        assert summary["unchanged"] == 1
        assert summary["creates"] == summary["updates"] == summary["deletes"] == 0

    def test_drift_exits_two(self, monkeypatch):
        rc, summary = self._run(monkeypatch, [], fail_on_diff=True)
        assert rc == 2
        assert summary["creates"] == 1
        assert summary["success"] is True

    def test_drift_without_flag_exits_zero(self, monkeypatch):
        rc, summary = self._run(monkeypatch, [], fail_on_diff=False)
        assert rc == 0
        assert summary["creates"] == 1
