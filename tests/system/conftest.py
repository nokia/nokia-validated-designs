"""System-test fixtures: per-version clab single node + design intent factory.

The ``--run-system`` flag gates collection so normal ``pytest`` runs ignore
these heavyweight tests. Each test parameterization spins a fresh 1-node
containerlab, applies config via JSON-RPC, and tears the lab down.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.models import FabricIntent
from automation.core.schema_validator import load_inputs

from tests.helpers.srlinux_jsonrpc import wait_json_rpc_ready

DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"
CLAB_TEMPLATE = Path(__file__).parent / "clab" / "single_node.clab.yml.tmpl"

# In the source tree, ``srl_builders/`` lives under
# ``automation/generators/ansible_filter_plugins/`` — put that dir on
# sys.path so ``srl_config.srl_config()`` can resolve ``from srl_builders``.
_PLUGIN_DIR = (
    Path(__file__).resolve().parents[2]
    / "automation" / "generators" / "ansible_filter_plugins"
)
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

JSONRPC_USER = "admin"
JSONRPC_PASSWORD = "NokiaSrl1!"
JSONRPC_PORT = 443


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-system",
        action="store_true",
        default=False,
        help="Run heavyweight system tests (spin up containerlab per test).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-system"):
        return
    skip = pytest.mark.skip(reason="needs --run-system to run")
    for item in items:
        if item.get_closest_marker("system") is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def _raw_inputs_factory() -> Callable[[str], tuple[dict, dict]]:
    """Session-scoped cache that loads ``(topo, svc)`` for a given design."""
    cache: dict[str, tuple[dict, dict]] = {}

    def _load(design_name: str) -> tuple[dict, dict]:
        if design_name not in cache:
            design_dir = DESIGNS_ROOT / design_name
            if not design_dir.exists():
                pytest.skip(f"Design dir not found: {design_dir}")
            cache[design_name] = load_inputs(design_dir)
        return cache[design_name]

    return _load


@pytest.fixture()
def design_intent(
    _raw_inputs_factory,
) -> Callable[[str, str], FabricIntent]:
    """Return a factory ``(design_name, srl_version) -> FabricIntent``.

    All node versions in the loaded topology are overridden to
    *srl_version* so the generated config targets the version we are
    about to deploy in containerlab.
    """

    def _factory(design_name: str, srl_version: str) -> FabricIntent:
        topo, svc = _raw_inputs_factory(design_name)
        topo_copy = copy.deepcopy(topo)
        svc_copy = copy.deepcopy(svc)
        # Group-style node declarations (3-stage + collapsed-spine).
        for key in ("leafs", "spines", "collapsed_spines"):
            group = topo_copy.get(key)
            if isinstance(group, dict) and "version" in group:
                group["version"] = srl_version
        # List-style node declarations (e.g. tors, explicit nodes).
        for list_key in ("nodes", "tors"):
            for node in topo_copy.get(list_key, []) or []:
                if isinstance(node, dict) and "version" in node:
                    node["version"] = srl_version
        return build_intent(topo_copy, svc_copy)

    return _factory


def _slug(version: str) -> str:
    return version.replace(".", "-")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _mgmt_ipv4(lab_name: str, node: str = "target") -> str:
    """Return the mgmt-ipv4 address of *node* in lab *lab_name*."""
    result = subprocess.run(
        ["containerlab", "inspect", "--name", lab_name, "-f", "json"],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    entries: list[dict[str, Any]] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                entries.extend(v)
    for entry in entries:
        name = entry.get("name") or entry.get("Name") or ""
        if name.endswith(f"-{node}") or name == node:
            ip = (
                entry.get("ipv4_address")
                or entry.get("IPv4Address")
                or entry.get("mgmt-ipv4-address")
                or ""
            )
            ip = str(ip).split("/")[0]
            if ip:
                return ip
    raise RuntimeError(f"Could not find mgmt IPv4 for {node} in lab {lab_name}")


@pytest.fixture()
def clab_single_node(tmp_path: Path, request: pytest.FixtureRequest):
    """Factory fixture: call with an SR Linux version to deploy a 1-node lab.

    Yields ``(host_ip, port)`` and tears the lab down on finalization,
    even if the test raised or was interrupted.
    """
    if shutil.which("containerlab") is None:
        pytest.skip("containerlab not installed on PATH")

    deployed: list[Path] = []

    def _deploy(srl_version: str) -> tuple[str, int]:
        slug = _slug(srl_version)
        lab_name = f"srl-accept-{slug}"
        lab_file = tmp_path / f"{lab_name}.clab.yml"
        lab_file.write_text(
            CLAB_TEMPLATE.read_text().format(name=lab_name, version=srl_version)
        )
        _run(["containerlab", "deploy", "-c", "-t", str(lab_file)], cwd=tmp_path)
        deployed.append(lab_file)
        ip = _mgmt_ipv4(lab_name)
        wait_json_rpc_ready(ip, JSONRPC_PORT, JSONRPC_USER, JSONRPC_PASSWORD)
        return ip, JSONRPC_PORT

    try:
        yield _deploy
    finally:
        for lab_file in deployed:
            subprocess.run(
                ["containerlab", "destroy", "-c", "-t", str(lab_file)],
                capture_output=True, text=True, check=False,
            )
