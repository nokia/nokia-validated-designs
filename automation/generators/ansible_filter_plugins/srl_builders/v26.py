"""SR Linux payload builder for all 26.x releases.

Inherits from ``v25_3`` (the 25.x yearly-boundary builder) and overrides
only the parts affected by 26.x breaking changes.

Breaking changes vs 25.x
~~~~~~~~~~~~~~~~~~~~~~~~~
- **routing-policy local-preference**: ``{"set": N}`` →
  ``{"value": N, "operation": "set"}``.

- **advertise-arp-nd-only-with-mac-table-entry**: Leaf removed from
  ``bgp-evpn/bgp-instance/routes/bridge-table/mac-ip`` in 26.3.
  Always enabled; must be omitted from configs.
"""

from __future__ import annotations

from typing import Any

from . import default as _base
from . import v25_3
from .default import (
    _bd_access_interfaces,
    _build_bridge_table,
    _build_routing_policy,
    _get,
    _get_lag_bridge_domains,
    _irb_index_map,
    _lag_lookup,
    _sorted_bridge_domains,
    _vxlan_index_map,
)

PROTECTED_NIS = _base.PROTECTED_NIS
PROTECTED_INTERFACES = _base.PROTECTED_INTERFACES

build_interface_updates = _base.build_interface_updates
build_subinterface_updates = _base.build_subinterface_updates
build_bfd_updates = _base.build_bfd_updates
build_tunnel_interface_updates = _base.build_tunnel_interface_updates
build_system_updates = _base.build_system_updates
build_prune_deletes = _base.build_prune_deletes

# Re-use the 25.3 default-NI builder (multipath ebgp nesting).
_build_default_ni = v25_3._build_default_ni


# ---------------------------------------------------------------------------
# Network instances  (overridden — advertise-arp-nd removed in 26.x)
# ---------------------------------------------------------------------------

def build_network_instance_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /network-instance entries (26.x schema)."""
    updates: list[dict] = []
    if scope in ("fabric", "all"):
        _build_default_ni(hv, updates)
    if scope in ("services", "all"):
        _build_macvrf_instances(hv, updates)
        _base._build_ipvrf_instances(hv, updates)
    return updates


def _build_macvrf_instances(hv: dict, updates: list[dict]) -> None:
    """Build mac-vrf entries without ``advertise-arp-nd-only-with-mac-table-entry``."""
    irb_map = _irb_index_map(hv)
    vxlan_map = _vxlan_index_map(hv)
    lag_lookup = _lag_lookup(hv)

    for bd in _sorted_bridge_domains(hv):
        bd_name = bd["name"]
        evi = bd["evi"]
        vxlan_idx = vxlan_map[bd_name]

        ni_interfaces: list[str] = []

        for acc in _bd_access_interfaces(bd):
            iface = acc["interface"]
            vlan = acc.get("vlan", "untagged")
            if vlan == "untagged":
                sub_idx = 4096
            else:
                sub_idx = int(vlan)
            ni_interfaces.append(f"{iface}.{sub_idx}")

        if bd_name in irb_map:
            irb_idx = irb_map[bd_name]
            ni_interfaces.append(f"irb0.{irb_idx}")

        for lag in hv.get("lags", []):
            lag_name = lag["name"]
            explicitly_listed = any(
                acc["interface"] == lag_name
                for acc in _bd_access_interfaces(bd)
            )
            if not explicitly_listed:
                for other_bd in hv.get("bridge_domains", []):
                    if other_bd["name"] != bd_name:
                        continue
                    lag_bds = _get_lag_bridge_domains(hv, lag_name)
                    if bd_name in lag_bds:
                        ni_interfaces.append(f"{lag_name}.4096")

        ni_value: dict[str, Any] = {
            "type": "mac-vrf",
            "admin-state": "enable",
            "description": bd_name,
            "interface": [{"name": iface} for iface in sorted(set(ni_interfaces))],
            "vxlan-interface": [{"name": f"vxlan0.{vxlan_idx}"}],
            "protocols": {
                "bgp-evpn": {
                    "bgp-instance": [{
                        "id": 1,
                        "vxlan-interface": f"vxlan0.{vxlan_idx}",
                        "evi": evi,
                        "ecmp": 8,
                    }],
                },
                "bgp-vpn": {
                    "bgp-instance": [{
                        "id": 1,
                        "route-target": {
                            "export-rt": f"target:1:{evi}",
                            "import-rt": f"target:1:{evi}",
                        },
                    }],
                },
            },
            "bridge-table": _build_bridge_table(bd),
        }

        updates.append({
            "path": f"/network-instance[name={bd_name}]",
            "value": ni_value,
            "op": "replace",
        })


# ---------------------------------------------------------------------------
# Routing policy  (overridden — local-preference syntax changed in 26.x)
# ---------------------------------------------------------------------------

def build_routing_policy_updates(hv: dict) -> list[dict[str, Any]]:
    """Build /routing-policy with 26.x ``local-preference`` and ``match.prefix`` schemas."""
    return _build_routing_policy(
        hv,
        local_pref={"value": 100, "operation": "set"},
        nested_prefix_set=True,
    )


# ---------------------------------------------------------------------------
# Phase-scoped entry points
# ---------------------------------------------------------------------------

def build_topology_updates(hv: dict) -> list[dict[str, Any]]:
    updates: list[dict] = []
    updates += build_interface_updates(hv, scope="topology")
    updates += build_subinterface_updates(hv, scope="topology")
    updates += build_bfd_updates(hv)
    updates += build_system_updates(hv, scope="topology")
    return updates


def build_fabric_updates(hv: dict) -> list[dict[str, Any]]:
    updates: list[dict] = []
    updates += build_network_instance_updates(hv, scope="fabric")
    updates += build_routing_policy_updates(hv)
    return updates


def build_services_updates(hv: dict) -> list[dict[str, Any]]:
    updates: list[dict] = []
    updates += build_interface_updates(hv, scope="services")
    updates += build_subinterface_updates(hv, scope="services")
    updates += build_tunnel_interface_updates(hv)
    updates += build_network_instance_updates(hv, scope="services")
    updates += build_system_updates(hv, scope="services")
    return updates
