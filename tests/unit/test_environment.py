"""Unit tests for the ``environment`` field and the deployer's preflight checks."""

from __future__ import annotations

import argparse
import logging

import pytest
from pydantic import ValidationError

from automation.core.models import Credentials, FabricIntent, NodeIntent
from automation.deploy import _preflight_environment


def _node(name: str, mgmt_ipv4: str) -> NodeIntent:
    return NodeIntent(
        name=name,
        role="leaf",
        platform="7220 IXR-D3L",
        version="25.10.1",
        system0_ipv4="192.0.2.11/32",
        asn=65411,
        mgmt_ipv4=mgmt_ipv4,
    )


def _intent(environment: str, nodes: list[NodeIntent], **overrides) -> FabricIntent:
    base = {
        "design": "3-stage-evpn-vxlan",
        "fabric_name": "dc1",
        "environment": environment,
        "spine_asn": 65500,
        "leaf_asn_start": 65411,
        "system0_prefix": "192.0.2.0/24",
        "nodes": nodes,
    }
    base.update(overrides)
    return FabricIntent(**base)


def _args(mode: str = "eda", generate_clab: bool = False) -> argparse.Namespace:
    return argparse.Namespace(mode=mode, generate_clab=generate_clab)


class TestEnvironmentField:
    def test_known_values_accepted(self):
        for env in ("containerlab", "physical"):
            assert _intent(env, [_node("leaf1", "172.21.21.11")]).environment == env

    def test_unknown_value_rejected(self):
        with pytest.raises(ValidationError):
            _intent("staging", [_node("leaf1", "172.21.21.11")])


class TestContainerlabPreflight:
    def test_lab_defaults_are_not_flagged(self, caplog):
        """A lab fabric may use factory credentials and unset mgmt IPs."""
        intent = _intent("containerlab", [_node("leaf1", "")])
        with caplog.at_level(logging.WARNING):
            assert _preflight_environment(intent, _args()) == []
        assert caplog.records == []


class TestPhysicalPreflight:
    def test_complete_intent_passes(self):
        intent = _intent(
            "physical",
            [_node("leaf1", "10.0.0.11"), _node("leaf2", "10.0.0.12")],
            credentials=Credentials(username="netops", password="s3cret"),
        )
        assert _preflight_environment(intent, _args()) == []

    def test_missing_mgmt_address_is_fatal(self):
        intent = _intent(
            "physical",
            [_node("leaf1", "10.0.0.11"), _node("leaf2", "")],
            credentials=Credentials(username="netops", password="s3cret"),
        )
        errors = _preflight_environment(intent, _args())
        assert len(errors) == 1
        assert "leaf2" in errors[0]
        assert "leaf1" not in errors[0]

    def test_factory_credentials_warn(self, caplog):
        intent = _intent("physical", [_node("leaf1", "10.0.0.11")])
        with caplog.at_level(logging.WARNING):
            assert _preflight_environment(intent, _args()) == []
        assert any("factory" in r.message for r in caplog.records)

    def test_ansible_mode_warns_about_cleartext_password(self, caplog):
        intent = _intent(
            "physical",
            [_node("leaf1", "10.0.0.11")],
            credentials=Credentials(username="netops", password="s3cret"),
        )
        with caplog.at_level(logging.WARNING):
            _preflight_environment(intent, _args(mode="ansible"))
        assert any("cleartext" in r.message for r in caplog.records)

    def test_generate_clab_warns_it_is_a_twin(self, caplog):
        intent = _intent(
            "physical",
            [_node("leaf1", "10.0.0.11")],
            credentials=Credentials(username="netops", password="s3cret"),
        )
        with caplog.at_level(logging.WARNING):
            assert _preflight_environment(intent, _args(generate_clab=True)) == []
        assert any("twin" in r.message for r in caplog.records)
