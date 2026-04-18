"""Acceptance tests: does a live SR Linux device accept our generated config?

For each supported (version, role) pair, build the host_vars from the
3-stage-evpn-vxlan design (with the node's SR Linux version overridden),
render the JSON-RPC payload via ``srl_config``, and POST it to a
freshly-deployed single-node containerlab. The test asserts:

1. the commit returns without an error;
2. a second identical commit also succeeds (idempotency).
"""

from __future__ import annotations

import pytest

from automation.eda_models.registry import SUPPORTED_SRL_VERSIONS
from automation.generators.ansible_filter_plugins.srl_config import srl_config
from automation.generators.ansible_generator import (
    _build_group_vars_all,
    _build_group_vars_leafs,
    _build_group_vars_spines,
    _build_leaf_host_vars,
    _build_spine_host_vars,
    _resolve_placement,
)

from tests.helpers.srlinux_jsonrpc import commit_set

TARGET_VERSIONS = ["24.10.3", "25.10.1", "26.3.1" ]
ROLES = ["leaf", "spine"]


def _merged_vars(intent, role: str) -> dict:
    """Merge group_vars (all + role) with host_vars the way Ansible does.

    The per-node host_vars alone lack the fabric-scope data that lives in
    ``group_vars`` (BGP/BFD block, routing-policy, shared services). In a
    playbook Ansible merges these into ``hostvars[inventory_hostname]``
    before the ``srl_config`` filter runs — this helper reproduces that.
    """
    merged = {}
    merged.update(_build_group_vars_all(intent))
    if role == "leaf":
        merged.update(_build_group_vars_leafs(intent))
        node = next(n for n in intent.nodes if n.name == "leaf1")
        placement = _resolve_placement(intent)
        host = _build_leaf_host_vars(node, intent, placement[node.name])
    else:
        merged.update(_build_group_vars_spines(intent))
        node = next(n for n in intent.nodes if n.name == "spine1")
        host = _build_spine_host_vars(node, intent)
    merged.update(host)
    return merged


@pytest.mark.parametrize("srl_version", TARGET_VERSIONS)
def test_version_supported(srl_version: str) -> None:
    """Guardrail: the target versions are declared supported by registry."""
    assert srl_version in SUPPORTED_SRL_VERSIONS


@pytest.mark.system
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("srl_version", TARGET_VERSIONS)
def test_config_accepted(
    srl_version: str,
    role: str,
    design_intent,
    clab_single_node,
) -> None:
    intent = design_intent(srl_version)
    host_vars = _merged_vars(intent, role)
    payload = srl_config(host_vars, sw_version=srl_version, phase="all")
    assert payload["update"] or payload["replace"], (
        "builder produced no update/replace entries — nothing to commit"
    )

    host, port = clab_single_node(srl_version)

    commit_set(
        host, port, "admin", "NokiaSrl1!",
        payload["update"], payload["replace"], payload["delete"],
    )
    commit_set(
        host, port, "admin", "NokiaSrl1!",
        payload["update"], payload["replace"], payload["delete"],
    )
