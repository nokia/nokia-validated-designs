"""SR Linux payload builder for 25.3.x.

Inherits everything from the default (24.10.x) builder and overrides only
the parts affected by YANG model changes introduced at the 25.x yearly
boundary.

Breaking changes vs 24.10.x
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **routing-policy match prefix-set**: The ``prefix-set`` leaf moved from
  ``statement/match/prefix-set`` to ``statement/match/prefix/prefix-set``
  (a new ``prefix`` container wraps the reference).
  24.10:  ``"match": {"prefix-set": "<name>", ...}``
  25.3:   ``"match": {"prefix": {"prefix-set": "<name>"}, ...}``

- **BGP afi-safi multipath maximum-paths**: The ``maximum-paths`` leaf moved
  from ``multipath/maximum-paths`` into ``multipath/ebgp/maximum-paths``
  (a new ``ebgp`` sub-container).
  24.10:  ``"multipath": {"allow-multiple-as": true, "maximum-paths": N}``
  25.3:   ``"multipath": {"allow-multiple-as": true, "ebgp": {"maximum-paths": N}}``
"""

from __future__ import annotations

from typing import Any

from . import default as _base
from .default import _build_routing_policy, _get

PROTECTED_NIS = _base.PROTECTED_NIS
PROTECTED_INTERFACES = _base.PROTECTED_INTERFACES

# Re-export every public symbol from the base builder unchanged.
build_interface_updates = _base.build_interface_updates
build_subinterface_updates = _base.build_subinterface_updates
build_bfd_updates = _base.build_bfd_updates
build_tunnel_interface_updates = _base.build_tunnel_interface_updates
build_system_updates = _base.build_system_updates
build_prune_deletes = _base.build_prune_deletes


# ---------------------------------------------------------------------------
# Network instances  (overridden — multipath schema changed in 25.3)
# ---------------------------------------------------------------------------

def build_network_instance_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /network-instance entries (25.3.x multipath schema)."""
    updates: list[dict] = []
    if scope in ("fabric", "all"):
        _build_default_ni(hv, updates)
    if scope in ("services", "all"):
        _base._build_macvrf_instances(hv, updates)
        _base._build_ipvrf_instances(hv, updates)
    return updates


def _build_default_ni(hv: dict, updates: list[dict]) -> None:
    """Build the ``default`` network-instance with 25.3.x multipath schema.

    In 25.3.x ``maximum-paths`` moved from ``multipath/`` into
    ``multipath/ebgp/`` (new sub-container).
    """
    node = hv.get("node", {})
    router_id = node.get("router_id")
    asn = node.get("asn")
    role = node.get("role", "leaf")
    fabric_name = hv.get("fabric_name", "dc1")
    bgp_cfg = hv.get("bgp", {})
    group_name = bgp_cfg.get(
        "group_name", bgp_cfg.get("group", f"bgpgroup-ebgp-{fabric_name}"),
    )

    underlay_ifaces = [f"{i['name']}.0" for i in hv.get("underlay_interfaces", [])]
    ni_interfaces = underlay_ifaces + ["system0.0"]

    dynamic_neighbors: dict[str, Any] = {}
    for iface in hv.get("underlay_interfaces", []):
        subif = f"{iface['name']}.0"
        dynamic_neighbors[subif] = {
            "peer-group": group_name,
            "allowed-peer-as": [iface["peer_asn"]],
        }

    evpn_cfg = _get(bgp_cfg, "afi_safi", "evpn", default={})
    ipv4_cfg = _get(bgp_cfg, "afi_safi", "ipv4_unicast", default={})
    ipv6_cfg = _get(bgp_cfg, "afi_safi", "ipv6_unicast", default={})

    evpn_max = evpn_cfg.get("multipath_max_paths", evpn_cfg.get("multipath_max", 64))
    ipv4_max = ipv4_cfg.get("multipath_max_paths", ipv4_cfg.get("multipath_max", 2))
    ipv6_max = ipv6_cfg.get(
        "multipath_max_paths", ipv6_cfg.get("multipath_max", ipv4_max),
    )

    afi_safi: list[dict[str, Any]] = [
        {
            "afi-safi-name": "evpn",
            "admin-state": "enable",
            "multipath": {
                "allow-multiple-as": True,
                "ebgp": {"maximum-paths": evpn_max},
            },
            "evpn": {
                "inter-as-vpn": True,
                "rapid-update": True,
            },
        },
        {
            "afi-safi-name": "ipv4-unicast",
            "admin-state": "enable",
            "multipath": {
                "allow-multiple-as": True,
                "ebgp": {"maximum-paths": ipv4_max},
            },
            "ipv4-unicast": {
                "advertise-ipv6-next-hops": True,
                "receive-ipv6-next-hops": True,
            },
            "evpn": {"rapid-update": True},
        },
        {
            "afi-safi-name": "ipv6-unicast",
            "admin-state": "enable",
            "multipath": {
                "allow-multiple-as": True,
                "ebgp": {"maximum-paths": ipv6_max},
            },
            "evpn": {"rapid-update": True},
        },
    ]

    export_policy = f"ebgp-isl-export-policy-{fabric_name}"
    import_policy = f"ebgp-isl-import-policy-{fabric_name}"

    group_value: dict[str, Any] = {
        "admin-state": "enable",
        "export-policy": [export_policy],
        "import-policy": [import_policy],
        "failure-detection": {
            "enable-bfd": True,
            "fast-failover": True,
        },
        "afi-safi": [
            {
                "afi-safi-name": "evpn",
                "admin-state": "enable",
            },
            {
                "afi-safi-name": "ipv4-unicast",
                "admin-state": "enable",
                "ipv4-unicast": {
                    "advertise-ipv6-next-hops": True,
                    "receive-ipv6-next-hops": True,
                },
            },
            {
                "afi-safi-name": "ipv6-unicast",
                "admin-state": "enable",
            },
        ],
    }

    ni_value: dict[str, Any] = {
        "type": "default",
        "admin-state": "enable",
        "description": f"fabric: {fabric_name} role: {role}",
        "router-id": router_id,
        "ip-forwarding": {"receive-ipv4-check": False},
        "interface": [{"name": iface} for iface in ni_interfaces],
        "protocols": {
            "bgp": {
                "admin-state": "enable",
                "autonomous-system": asn,
                "router-id": router_id,
                "dynamic-neighbors": {
                    "interface": [{
                        "interface-name": subif,
                        "peer-group": dn["peer-group"],
                        "allowed-peer-as": dn["allowed-peer-as"],
                    } for subif, dn in dynamic_neighbors.items()],
                },
                "ebgp-default-policy": {
                    "import-reject-all": True,
                    "export-reject-all": True,
                },
                "afi-safi": afi_safi,
                "preference": {"ebgp": 170, "ibgp": 170},
                "route-advertisement": {
                    "rapid-withdrawal": True,
                    "wait-for-fib-install": False,
                },
                "group": [{
                    "group-name": group_name,
                    **group_value,
                }],
            },
        },
    }

    updates.append({
        "path": "/network-instance[name=default]",
        "value": ni_value,
        "op": "replace",
    })


# ---------------------------------------------------------------------------
# Routing policy  (overridden — prefix-set nesting changed in 25.3)
# ---------------------------------------------------------------------------

def build_routing_policy_updates(hv: dict) -> list[dict[str, Any]]:
    """Build /routing-policy with the 25.3.x ``match.prefix.prefix-set`` schema."""
    return _build_routing_policy(hv, local_pref={"set": 100}, nested_prefix_set=True)


# ---------------------------------------------------------------------------
# Phase-scoped entry points
# ---------------------------------------------------------------------------

def build_topology_updates(hv: dict) -> list[dict[str, Any]]:
    """Topology phase: system0, underlay interfaces/subinterfaces, BFD, hostname, LLDP."""
    updates: list[dict] = []
    updates += build_interface_updates(hv, scope="topology")
    updates += build_subinterface_updates(hv, scope="topology")
    updates += build_bfd_updates(hv)
    updates += build_system_updates(hv, scope="topology")
    return updates


def build_fabric_updates(hv: dict) -> list[dict[str, Any]]:
    """Fabric phase: default NI (BGP underlay/overlay) + routing-policy."""
    updates: list[dict] = []
    updates += build_network_instance_updates(hv, scope="fabric")
    updates += build_routing_policy_updates(hv)
    return updates


def build_services_updates(hv: dict) -> list[dict[str, Any]]:
    """Services phase: edge, LAG, IRB, VXLAN, mac-vrf, ip-vrf, ES, event-handler."""
    updates: list[dict] = []
    updates += build_interface_updates(hv, scope="services")
    updates += build_subinterface_updates(hv, scope="services")
    updates += build_tunnel_interface_updates(hv)
    updates += build_network_instance_updates(hv, scope="services")
    updates += build_system_updates(hv, scope="services")
    return updates
