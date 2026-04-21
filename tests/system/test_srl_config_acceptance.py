"""Acceptance tests: does a live SR Linux device accept our generated config?

For each supported ``(design, version, node)`` combination, build the
host_vars the same way the ansible generator does (group_vars + host_vars
merged à la Ansible), render the JSON-RPC payload via ``srl_config``, and
POST it to a freshly-deployed single-node containerlab. The test asserts:

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

TARGET_VERSIONS = ["24.10.3", "25.10.1"]

# Per design, the node(s) we want the live SR Linux device to accept the
# config for. The test selects the right host_vars builder based on the
# node's ``role`` in the built FabricIntent (leaf vs. anything else), so
# the collapsed-spine design exercises both a collapsed-spine (role=leaf)
# and a ToR (role=tor — goes through the generic/"spine" builder path).
DESIGN_CASES = [
    pytest.param("3-stage-evpn-vxlan", "leaf1", id="3stage-leaf1"),
    pytest.param("3-stage-evpn-vxlan", "spine1", id="3stage-spine1"),
    pytest.param("collapsed-spine", "spine1", id="collapsed-spine1"),
    # NOTE: ToR ansible generation is not yet supported — the collapsed-
    # spine design is currently deployed via EDA manifests, so ToRs
    # (role=tor, asn=0, EVPN-unaware) would be sent through the generic
    # non-leaf builder which emits an invalid BGP autonomous-system=0.
    # Re-enable once a dedicated ToR ansible path lands.
    pytest.param(
        "collapsed-spine", "tor1", id="collapsed-tor1",
        marks=pytest.mark.skip(
            reason="ToR ansible generation not yet implemented",
        ),
    ),
]


def _merged_vars(intent, node_name: str) -> dict:
    """Merge group_vars (all + role) with host_vars the way Ansible does.

    The per-node host_vars alone lack the fabric-scope data that lives in
    ``group_vars`` (BGP/BFD block, routing-policy, shared services). In a
    playbook Ansible merges these into ``hostvars[inventory_hostname]``
    before the ``srl_config`` filter runs — this helper reproduces that.
    """
    node = next(n for n in intent.nodes if n.name == node_name)
    merged: dict = {}
    merged.update(_build_group_vars_all(intent))
    if node.role == "leaf":
        merged.update(_build_group_vars_leafs(intent))
        placement = _resolve_placement(intent)
        host = _build_leaf_host_vars(node, intent, placement[node.name])
    else:
        # Non-leaf nodes (3-stage spines, collapsed-spine ToRs) all go
        # through the generator's "else" branch today.
        merged.update(_build_group_vars_spines(intent))
        host = _build_spine_host_vars(node, intent)
    merged.update(host)
    return merged


@pytest.mark.parametrize("srl_version", TARGET_VERSIONS)
def test_version_supported(srl_version: str) -> None:
    """Guardrail: the target versions are declared supported by registry."""
    assert srl_version in SUPPORTED_SRL_VERSIONS


@pytest.mark.system
@pytest.mark.parametrize("design_name,node_name", DESIGN_CASES)
@pytest.mark.parametrize("srl_version", TARGET_VERSIONS)
def test_config_accepted(
    srl_version: str,
    design_name: str,
    node_name: str,
    design_intent,
    clab_single_node,
) -> None:
    intent = design_intent(design_name, srl_version)
    host_vars = _merged_vars(intent, node_name)
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
