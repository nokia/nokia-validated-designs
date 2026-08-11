"""
Fabric builder dispatcher.

Routes to the appropriate design-specific builder based on the
'design' field in the topology input. Each design builder transforms
simple input (topology.yaml + services.yaml) into a FabricIntent.

:data:`SUPPORTED_DESIGNS` is the single source of truth for which validated
designs this engine can build and deploy. The repository ships other designs
under ``validated-designs/`` that are deployed by their own tooling (hand
written EDA manifests, containerlab labs); those have no builder here and are
reported as unsupported rather than failing with an obscure error.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from automation.core.models import FabricIntent

logger = logging.getLogger(__name__)

# Repository layout: <repo>/automation/core/fabric_builder.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGNS_ROOT = REPO_ROOT / "validated-designs"


class DesignBuilder(Protocol):
    """Protocol that every design-specific builder must implement."""

    def build(self, topology: dict, services: dict) -> FabricIntent: ...


@dataclass(frozen=True)
class DesignSpec:
    """One design the automation engine can build and deploy.

    ``name`` is the value of the ``design`` field in ``topology.yaml``;
    ``design_dir`` is the repo-relative directory passed to ``--design``.
    """

    name: str
    module: str
    strategy: str
    design_dir: str
    summary: str


# Registry of designs supported by this engine. Each `module` must expose a
# `build(topology, services) -> FabricIntent` function.
SUPPORTED_DESIGNS: dict[str, DesignSpec] = {
    "3-stage-evpn-vxlan": DesignSpec(
        name="3-stage-evpn-vxlan",
        module="automation.designs.three_stage_evpn_vxlan",
        strategy="constrained",
        design_dir="validated-designs/3-stage-evpn-vxlan",
        summary=(
            "Leaf/spine EVPN-VXLAN fabric with eBGP underlay. Nodes, ISLs, "
            "ASNs and system0 IPs are auto-generated from spine/leaf counts."
        ),
    ),
    "collapsed-spine": DesignSpec(
        name="collapsed-spine",
        module="automation.designs.collapsed_spine",
        strategy="constrained",
        design_dir="validated-designs/collapsed-spine",
        summary=(
            "Two collapsed-spines host all overlay services, with explicit "
            "ToR nodes onboarded outside the Fabric selectors. ISLs are "
            "enumerated explicitly; IRBs are dual-stack."
        ),
    ),
    "unconstrained-3-stage": DesignSpec(
        name="unconstrained-3-stage",
        module="automation.designs.unconstrained_3_stage",
        strategy="passthrough",
        design_dir="validated-designs/unconstrained-3-stage",
        summary=(
            "Fully explicit 3-stage fabric: every node, link, ASN and IP is "
            "declared in the input YAML. No auto-generation."
        ),
    ),
}

# Back-compat view used by the dispatcher and existing callers.
DESIGN_BUILDERS: dict[str, str] = {
    name: spec.module for name, spec in SUPPORTED_DESIGNS.items()
}


def list_supported_designs() -> list[DesignSpec]:
    """Return every supported design, sorted by design name."""
    return [SUPPORTED_DESIGNS[name] for name in sorted(SUPPORTED_DESIGNS)]


def supported_design_names() -> list[str]:
    """Return the sorted names accepted in ``topology.design``."""
    return sorted(SUPPORTED_DESIGNS)


def is_engine_managed(design_dir: Path) -> bool:
    """Whether *design_dir* carries inputs this engine can build from.

    Engine-managed designs ship ``inputs/topology.yaml`` (or a
    ``inputs/topology.d/`` fragment directory). Designs deployed by their own
    tooling have neither.
    """
    inputs = Path(design_dir) / "inputs"
    return (inputs / "topology.yaml").is_file() or (inputs / "topology.d").is_dir()


def find_unmanaged_design_dirs(designs_root: Path | None = None) -> list[str]:
    """List directories under ``validated-designs/`` with no engine inputs.

    Used to tell the user *why* a design directory was rejected instead of
    letting the input loader fail on a missing ``topology.yaml``.
    """
    root = Path(designs_root) if designs_root is not None else DESIGNS_ROOT
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and not is_engine_managed(d)
    )


def check_design_dir(design_dir: Path) -> str | None:
    """Return an error message if *design_dir* is not deployable, else None."""
    design_dir = Path(design_dir)
    if not design_dir.exists():
        return f"Design directory not found: {design_dir}"
    if is_engine_managed(design_dir):
        return None
    supported = ", ".join(spec.design_dir for spec in list_supported_designs())
    return (
        f"Design directory '{design_dir}' is not supported by the NVD deployer: "
        f"it has no inputs/topology.yaml or inputs/topology.d/. Designs in this "
        f"repository that ship their own EDA manifests or containerlab labs are "
        f"deployed by their own instructions, not by this engine. "
        f"Supported design directories: {supported}. "
        f"Run 'python -m automation.deploy --list-designs' for details."
    )


def build_intent(topology: dict, services: dict) -> FabricIntent:
    """
    Build a FabricIntent from simple input by dispatching to the
    appropriate design-specific builder.

    Args:
        topology: Parsed topology.yaml dict
        services: Parsed services.yaml dict

    Returns:
        FabricIntent — the complete, design-agnostic intent

    Raises:
        ValueError: If the design name is not recognized
    """
    design_name = topology.get("design")
    if not design_name:
        raise ValueError("topology.yaml must contain a 'design' field")

    module_path = DESIGN_BUILDERS.get(design_name)
    if not module_path:
        valid = ", ".join(supported_design_names())
        raise ValueError(
            f"Unknown design '{design_name}'. Supported designs: {valid}. "
            f"Run 'python -m automation.deploy --list-designs' for details."
        )

    logger.info("Building intent for design: %s", design_name)
    module = importlib.import_module(module_path)
    return module.build(topology, services)
