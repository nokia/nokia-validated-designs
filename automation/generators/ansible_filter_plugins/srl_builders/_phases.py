"""Shared phase composition for SR Linux payload builders.

Every version builder exposes the same three phase entry points
(``build_topology_updates`` / ``build_fabric_updates`` /
``build_services_updates``). The *composition* of those phases out of the
lower-level ``build_*_updates`` functions is identical across versions — only
the individual builders differ. Defining the composition once here (rather than
copy-pasting it into each version module) keeps phase ordering identical by
construction.

Each version module calls :func:`make_phase_entrypoints` with its own module
namespace; the returned functions resolve the ``build_*`` builders on that
namespace at call time, so any per-version override is honored automatically.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable

PhaseFn = Callable[[dict], list[dict[str, Any]]]


def make_phase_entrypoints(ns: ModuleType) -> tuple[PhaseFn, PhaseFn, PhaseFn]:
    """Return ``(topology, fabric, services)`` phase entry points for *ns*.

    *ns* is the version builder module (typically ``sys.modules[__name__]``).
    The builders are looked up on *ns* at call time so re-exported and
    overridden ``build_*`` functions both resolve correctly.
    """

    def build_topology_updates(hv: dict) -> list[dict[str, Any]]:
        """Topology phase: system0, underlay interfaces/subinterfaces, BFD, hostname, LLDP."""
        updates: list[dict] = []
        updates += ns.build_interface_updates(hv, scope="topology")
        updates += ns.build_subinterface_updates(hv, scope="topology")
        updates += ns.build_bfd_updates(hv)
        updates += ns.build_system_updates(hv, scope="topology")
        return updates

    def build_fabric_updates(hv: dict) -> list[dict[str, Any]]:
        """Fabric phase: default NI (BGP underlay/overlay) + routing-policy."""
        updates: list[dict] = []
        updates += ns.build_network_instance_updates(hv, scope="fabric")
        updates += ns.build_routing_policy_updates(hv)
        return updates

    def build_services_updates(hv: dict) -> list[dict[str, Any]]:
        """Services phase: edge, LAG, IRB, VXLAN, mac-vrf, ip-vrf, ES, event-handler."""
        updates: list[dict] = []
        updates += ns.build_interface_updates(hv, scope="services")
        updates += ns.build_subinterface_updates(hv, scope="services")
        updates += ns.build_tunnel_interface_updates(hv)
        updates += ns.build_network_instance_updates(hv, scope="services")
        updates += ns.build_system_updates(hv, scope="services")
        return updates

    return build_topology_updates, build_fabric_updates, build_services_updates
