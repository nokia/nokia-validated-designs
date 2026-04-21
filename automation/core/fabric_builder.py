"""
Fabric builder dispatcher.

Routes to the appropriate design-specific builder based on the
'design' field in the topology input. Each design builder transforms
simple input (topology.yaml + services.yaml) into a FabricIntent.
"""

from __future__ import annotations

import importlib
import logging
from typing import Protocol

from automation.core.models import FabricIntent

logger = logging.getLogger(__name__)


class DesignBuilder(Protocol):
    """Protocol that every design-specific builder must implement."""

    def build(self, topology: dict, services: dict) -> FabricIntent: ...


# Registry mapping design names to builder module paths.
# Each module must expose a `build(topology, services) -> FabricIntent` function.
DESIGN_BUILDERS: dict[str, str] = {
    "3-stage-evpn-vxlan": "automation.designs.three_stage_evpn_vxlan",
    "unconstrained-3-stage": "automation.designs.unconstrained_3_stage",
    "collapsed-spine": "automation.designs.collapsed_spine",
}


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
        valid = ", ".join(sorted(DESIGN_BUILDERS.keys()))
        raise ValueError(
            f"Unknown design '{design_name}'. Supported designs: {valid}"
        )

    logger.info("Building intent for design: %s", design_name)
    module = importlib.import_module(module_path)
    return module.build(topology, services)
