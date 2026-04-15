"""Default SR Linux payload builder (targets 24.10.x).

Each ``build_*`` function receives the merged host_vars dict and returns a list
of ``{"path": ..., "value": ..., "op": ...}`` dicts.  The ``op`` key classifies
each entry for the ``nokia.srlinux.config`` module:

- ``"update"``  -- merge leaves (physical interfaces, hostname, LLDP, BFD)
- ``"replace"`` -- full subtree ownership (subinterfaces, NIs, routing-policy,
                   VXLAN, LAG aggregates, ethernet-segments, event-handler)
- ``"delete"``  -- remove stale list entries (pruning)

Phase-scoped entry points
~~~~~~~~~~~~~~~~~~~~~~~~~
- ``build_topology_updates``  -- system0, underlay, BFD, hostname, LLDP
- ``build_fabric_updates``    -- default NI (BGP), routing-policy
- ``build_services_updates``  -- edge, LAG, IRB, VXLAN, mac-vrf, ip-vrf, ES, event-handler
- ``build_prune_deletes``     -- delete paths for resources absent from intent
"""

from __future__ import annotations

from typing import Any

PROTECTED_NIS = frozenset({"default", "mgmt"})
PROTECTED_INTERFACES = frozenset({"mgmt0", "system0", "irb0", "lo0"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(d, *keys, default=None):
    """Nested dict/Mapping lookup (works with Ansible HostVarsVars)."""
    for k in keys:
        if not hasattr(d, "get"):
            return default
        d = d.get(k, default)
    return d


def _sorted_bridge_domains(hv: dict) -> list[dict]:
    return sorted(hv.get("bridge_domains", []), key=lambda bd: bd["name"])


def _sorted_routers(hv: dict) -> list[dict]:
    return sorted(hv.get("routers", []), key=lambda r: r["name"])


def _irb_index_map(hv: dict) -> dict[str, int]:
    """Map bridge-domain name -> IRB subinterface index (sequential from 0)."""
    return {bd["name"]: idx for idx, bd in enumerate(_sorted_bridge_domains(hv))}


def _vxlan_index_map(hv: dict) -> dict[str, int]:
    """Map service name -> vxlan-interface index (sequential from 500).

    All services (bridge_domains + routers) are sorted by name, then assigned
    indices starting at 500.
    """
    services: list[dict] = []
    for bd in hv.get("bridge_domains", []):
        services.append({"name": bd["name"], "type": "bridged"})
    for r in hv.get("routers", []):
        services.append({"name": r["name"], "type": "routed"})
    services.sort(key=lambda s: s["name"])
    return {s["name"]: 500 + idx for idx, s in enumerate(services)}


def _vxlan_type_map(hv: dict) -> dict[str, str]:
    """Map service name -> 'bridged' or 'routed'."""
    m: dict[str, str] = {}
    for bd in hv.get("bridge_domains", []):
        m[bd["name"]] = "bridged"
    for r in hv.get("routers", []):
        m[r["name"]] = "routed"
    return m


def _vni_map(hv: dict) -> dict[str, int]:
    """Map service name -> VNI."""
    m: dict[str, int] = {}
    for bd in hv.get("bridge_domains", []):
        m[bd["name"]] = bd["vni"]
    for r in hv.get("routers", []):
        m[r["name"]] = r["vni"]
    return m


def _lag_lookup(hv: dict) -> dict[str, dict]:
    """Map LAG name -> LAG definition."""
    return {lag["name"]: lag for lag in hv.get("lags", [])}


def _esi_from_mac(system_id_mac: str) -> str:
    """Derive 10-byte ESI from 6-byte LACP system-id-mac.

    ESI = type-byte ``00`` + system_id_mac (6 bytes) + ``00:00:00`` padding.
    """
    return f"00:{system_id_mac}:00:00:00"


def _collect_all_physical_interfaces(hv: dict) -> list[str]:
    """Return sorted list of all physical interface names that need LLDP."""
    names: set[str] = set()
    for iface in hv.get("underlay_interfaces", []):
        names.add(iface["name"])
    for iface in hv.get("edge_interfaces", []):
        names.add(iface["name"])
    for lag in hv.get("lags", []):
        for member in lag.get("members", []):
            names.add(member["interface"])
    for ri in hv.get("routed_interfaces", []):
        iface_name = ri.get("interface")
        if iface_name and not iface_name.startswith("lag"):
            names.add(iface_name)
    return sorted(names)


def _bd_access_interfaces(bd: dict) -> list[dict]:
    """Return access interface entries for a bridge domain."""
    return bd.get("access", [])


def _bd_lag_access(bd: dict, lag_lookup: dict) -> list[str]:
    """Return LAG interface names used as access in this bridge domain."""
    lag_names: list[str] = []
    for acc in _bd_access_interfaces(bd):
        iface = acc["interface"]
        if iface in lag_lookup:
            lag_names.append(iface)
    return lag_names


# ---------------------------------------------------------------------------
# Interface builders
# ---------------------------------------------------------------------------

def build_interface_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /interface entries.

    *scope* controls which interfaces are emitted:
    - ``"topology"`` -- underlay + system0 only
    - ``"services"`` -- edge, LAG members, LAG aggregates only
    - ``"all"``      -- everything (default, backward-compatible)
    """
    updates: list[dict] = []

    if scope in ("topology", "all"):
        for iface in hv.get("underlay_interfaces", []):
            updates.append({
                "path": f"/interface[name={iface['name']}]",
                "value": {"admin-state": "enable"},
                "op": "update",
            })
        router_id = _get(hv, "node", "router_id")
        if router_id:
            updates.append({
                "path": "/interface[name=system0]",
                "value": {"admin-state": "enable"},
                "op": "update",
            })

    if scope in ("services", "all"):
        for iface in hv.get("edge_interfaces", []):
            name = iface["name"]
            value: dict[str, Any] = {"admin-state": "enable"}
            if iface.get("encap") == "dot1q":
                value["vlan-tagging"] = True
            updates.append({
                "path": f"/interface[name={name}]", "value": value,
                "op": "update",
            })

        member_reload_delays: dict[str, int] = {}
        for lag in hv.get("lags", []):
            reload_delay = lag.get("reload_delay_timer", 100)
            for member in lag.get("members", []):
                member_reload_delays[member["interface"]] = reload_delay

        for lag in hv.get("lags", []):
            for member in lag.get("members", []):
                mname = member["interface"]
                updates.append({
                    "path": f"/interface[name={mname}]",
                    "value": {
                        "description": lag.get("description", lag["name"]),
                        "admin-state": "enable",
                        "ethernet": {
                            "aggregate-id": lag["name"],
                            "lacp-port-priority": 32768,
                            "reload-delay": member_reload_delays.get(mname, 100),
                        },
                    },
                    "op": "update",
                })

        for lag in hv.get("lags", []):
            lag_name = lag["name"]
            lag_value: dict[str, Any] = {
                "description": lag.get("description", lag_name),
                "admin-state": "enable",
                "vlan-tagging": True,
                "lag": {
                    "lag-type": "lacp",
                    "min-links": lag.get("min_links", 1),
                    "lacp-fallback-mode": _get(lag, "fallback", "mode", default="static"),
                    "lacp-fallback-timeout": _get(lag, "fallback", "timeout", default=60),
                    "lacp": {
                        "interval": _get(lag, "lacp", "interval", default="fast").upper(),
                        "lacp-mode": "ACTIVE",
                        "admin-key": _get(lag, "lacp", "admin_key"),
                        "system-id-mac": _get(lag, "lacp", "system_id_mac"),
                        "system-priority": _get(lag, "lacp", "system_priority", default=32768),
                    },
                },
            }
            mode = lag.get("mode", "all-active")
            if mode in ("single-active", "port-active"):
                lag_value["ethernet"] = {"standby-signaling": "lacp"}
            updates.append({
                "path": f"/interface[name={lag_name}]", "value": lag_value,
                "op": "replace",
            })

    return updates


# ---------------------------------------------------------------------------
# Subinterface builders
# ---------------------------------------------------------------------------

def build_subinterface_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /interface[name=X]/subinterface[index=Y] entries.

    *scope* controls which subinterfaces are emitted:
    - ``"topology"`` -- underlay .0 + system0.0
    - ``"services"`` -- edge, LAG, IRB, routed subinterfaces
    - ``"all"``      -- everything (default, backward-compatible)
    """
    updates: list[dict] = []
    lag_lookup = _lag_lookup(hv)

    if scope in ("topology", "all"):
        for iface in hv.get("underlay_interfaces", []):
            name = iface["name"]
            updates.append({
                "path": f"/interface[name={name}]/subinterface[index=0]",
                "value": {
                    "admin-state": "enable",
                    "ipv6": {
                        "admin-state": "enable",
                        "router-advertisement": {
                            "router-role": {
                                "admin-state": "enable",
                                "max-advertisement-interval": 10,
                                "min-advertisement-interval": 4,
                            }
                        },
                    },
                },
                "op": "replace",
            })

        router_id = _get(hv, "node", "router_id")
        if router_id:
            updates.append({
                "path": "/interface[name=system0]/subinterface[index=0]",
                "value": {
                    "admin-state": "enable",
                    "ipv4": {
                        "admin-state": "enable",
                        "address": [{"ip-prefix": f"{router_id}/32"}],
                    },
                },
                "op": "replace",
            })

    if scope in ("services", "all"):
        for iface in hv.get("edge_interfaces", []):
            name = iface["name"]
            for bd in hv.get("bridge_domains", []):
                for acc in _bd_access_interfaces(bd):
                    if acc["interface"] != name:
                        continue
                    vlan = acc.get("vlan", "untagged")
                    if vlan == "untagged":
                        idx = 4096
                        vlan_value: dict[str, Any] = {"encap": {"untagged": {}}}
                    else:
                        idx = int(vlan)
                        vlan_value = {"encap": {"single-tagged": {"vlan-id": idx}}}
                    updates.append({
                        "path": f"/interface[name={name}]/subinterface[index={idx}]",
                        "value": {
                            "type": "bridged",
                            "admin-state": "enable",
                            "vlan": vlan_value,
                        },
                        "op": "replace",
                    })

        for lag in hv.get("lags", []):
            lag_name = lag["name"]
            for bd in hv.get("bridge_domains", []):
                for acc in _bd_access_interfaces(bd):
                    if acc["interface"] != lag_name:
                        continue
                    vlan = acc.get("vlan", "untagged")
                    if vlan == "untagged":
                        idx = 4096
                        vlan_value = {"encap": {"untagged": {}}}
                    else:
                        idx = int(vlan)
                        vlan_value = {"encap": {"single-tagged": {"vlan-id": idx}}}
                    updates.append({
                        "path": f"/interface[name={lag_name}]/subinterface[index={idx}]",
                        "value": {
                            "type": "bridged",
                            "admin-state": "enable",
                            "vlan": vlan_value,
                        },
                        "op": "replace",
                    })

        _build_lag_default_subinterfaces(hv, updates, lag_lookup)

        irb_map = _irb_index_map(hv)
        for bd in _sorted_bridge_domains(hv):
            irb_cfg = bd.get("irb")
            if not irb_cfg:
                continue
            idx = irb_map[bd["name"]]
            ipv4_addr = irb_cfg.get("ipv4")
            if not ipv4_addr:
                continue

            addr_props: dict[str, Any] = {}
            if irb_cfg.get("anycast_gw"):
                addr_props["anycast-gw"] = True

            ipv4_value: dict[str, Any] = {
                "admin-state": "enable",
                "address": [{
                    "ip-prefix": ipv4_addr,
                    **addr_props,
                }],
                "arp": {
                    "timeout": irb_cfg.get("arp_timeout", 250),
                    "evpn": {"advertise": [{"route-type": "dynamic"}]},
                },
            }
            if irb_cfg.get("proxy_arp"):
                ipv4_value["arp"]["proxy-arp"] = True

            sub_value: dict[str, Any] = {
                "ip-mtu": irb_cfg.get("ip_mtu", 1500),
                "ipv4": ipv4_value,
            }
            if irb_cfg.get("anycast_gw"):
                sub_value["anycast-gw"] = {"virtual-router-id": 1}

            updates.append({
                "path": f"/interface[name=irb0]/subinterface[index={idx}]",
                "value": sub_value,
                "op": "replace",
            })

        for ri in hv.get("routed_interfaces", []):
            iface_name = ri.get("interface", ri["name"])
            vlan_id = ri.get("vlan_id")
            if vlan_id is None or str(vlan_id).lower() == "null":
                idx = 4097
            else:
                idx = int(vlan_id)

            addrs = ri.get("ipv4_addresses", [])
            addr_list = []
            for a in addrs:
                entry: dict[str, Any] = {"ip-prefix": a["ip_prefix"]}
                # "primary" is a YANG empty leaf; omit from set payloads
                addr_list.append(entry)

            sub_value = {
                "type": "routed",
                "admin-state": "enable",
                "ip-mtu": ri.get("ip_mtu", 1500),
                "ipv4": {
                    "admin-state": "enable",
                    "address": addr_list,
                    "arp": {"timeout": ri.get("arp_timeout", 14400)},
                },
            }

            if vlan_id is not None and str(vlan_id).lower() != "null":
                sub_value["vlan"] = {
                    "encap": {"single-tagged": {"vlan-id": int(vlan_id)}}
                }

            updates.append({
                "path": f"/interface[name={iface_name}]/subinterface[index={idx}]",
                "value": sub_value,
                "op": "replace",
            })

    return updates


def _build_lag_default_subinterfaces(
    hv: dict, updates: list[dict], lag_lookup: dict
) -> None:
    """Add default bridged subinterfaces for LAGs not explicitly in access lists.

    When a LAG has bridge-domain associations via its member BDs but no explicit
    access entry, it still gets a default untagged subinterface (index 4096).
    """
    lags_with_explicit_sub: set[str] = set()
    for bd in hv.get("bridge_domains", []):
        for acc in _bd_access_interfaces(bd):
            if acc["interface"] in lag_lookup:
                lags_with_explicit_sub.add(acc["interface"])

    for lag in hv.get("lags", []):
        lag_name = lag["name"]
        if lag_name not in lags_with_explicit_sub:
            has_bd_association = False
            for bd in hv.get("bridge_domains", []):
                for acc in _bd_access_interfaces(bd):
                    if acc["interface"] == lag_name:
                        has_bd_association = True
                        break
                if has_bd_association:
                    break

            if not has_bd_association:
                updates.append({
                    "path": f"/interface[name={lag_name}]/subinterface[index=4096]",
                    "value": {
                        "type": "bridged",
                        "admin-state": "enable",
                        "vlan": {"encap": {"untagged": {}}},
                    },
                    "op": "replace",
                })


# ---------------------------------------------------------------------------
# BFD
# ---------------------------------------------------------------------------

def build_bfd_updates(hv: dict) -> list[dict[str, Any]]:
    """Build /bfd/subinterface entries for each underlay .0 subinterface."""
    updates: list[dict] = []
    bfd = hv.get("bfd", {})
    desired = bfd.get("desired_min_transmit_interval", bfd.get("desired_min_tx", 1000000))
    required = bfd.get("required_min_receive", bfd.get("required_min_rx", 1000000))
    detect = bfd.get("detection_multiplier", 3)
    echo = bfd.get("min_echo_receive_interval", bfd.get("min_echo_rx", 1000000))

    for iface in hv.get("underlay_interfaces", []):
        name = iface["name"]
        updates.append({
            "path": f"/bfd/subinterface[id={name}.0]",
            "value": {
                "admin-state": "enable",
                "desired-minimum-transmit-interval": desired,
                "required-minimum-receive": required,
                "detection-multiplier": detect,
                "minimum-echo-receive-interval": echo,
            },
            "op": "update",
        })

    return updates


# ---------------------------------------------------------------------------
# Tunnel interfaces (VXLAN)
# ---------------------------------------------------------------------------

def build_tunnel_interface_updates(hv: dict) -> list[dict[str, Any]]:
    """Build /tunnel-interface[name=vxlan0]/vxlan-interface entries."""
    updates: list[dict] = []
    vxlan_map = _vxlan_index_map(hv)
    type_map = _vxlan_type_map(hv)
    vni_map = _vni_map(hv)

    for svc_name in sorted(vxlan_map, key=lambda n: vxlan_map[n]):
        idx = vxlan_map[svc_name]
        updates.append({
            "path": f"/tunnel-interface[name=vxlan0]/vxlan-interface[index={idx}]",
            "value": {
                "type": type_map[svc_name],
                "ingress": {"vni": vni_map[svc_name]},
                "egress": {"source-ip": "use-system-ipv4-address"},
            },
            "op": "replace",
        })

    return updates


# ---------------------------------------------------------------------------
# Network instances
# ---------------------------------------------------------------------------

def build_network_instance_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /network-instance entries.

    *scope* controls which NIs are emitted:
    - ``"fabric"``   -- default NI only (BGP underlay/overlay)
    - ``"services"`` -- mac-vrf + ip-vrf only
    - ``"all"``      -- everything (default, backward-compatible)
    """
    updates: list[dict] = []
    if scope in ("fabric", "all"):
        _build_default_ni(hv, updates)
    if scope in ("services", "all"):
        _build_macvrf_instances(hv, updates)
        _build_ipvrf_instances(hv, updates)
    return updates


def _build_default_ni(hv: dict, updates: list[dict]) -> None:
    """Build the ``default`` network-instance."""
    node = hv.get("node", {})
    router_id = node.get("router_id")
    asn = node.get("asn")
    role = node.get("role", "leaf")
    fabric_name = hv.get("fabric_name", "dc1")
    bgp_cfg = hv.get("bgp", {})
    group_name = bgp_cfg.get("group_name", bgp_cfg.get("group", f"bgpgroup-ebgp-{fabric_name}"))

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
    ipv6_max = ipv6_cfg.get("multipath_max_paths", ipv6_cfg.get("multipath_max", ipv4_max))

    afi_safi: list[dict[str, Any]] = [
        {
            "afi-safi-name": "evpn",
            "admin-state": "enable",
            "multipath": {
                "allow-multiple-as": True,
                "maximum-paths": evpn_max,
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
                "maximum-paths": ipv4_max,
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
                "maximum-paths": ipv6_max,
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
        "interface": [{
            "name": iface
        } for iface in ni_interfaces],
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


def _build_macvrf_instances(hv: dict, updates: list[dict]) -> None:
    """Build mac-vrf network-instance entries."""
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
                        "routes": {
                            "bridge-table": {
                                "mac-ip": {
                                    "advertise-arp-nd-only-with-mac-table-entry": True,
                                },
                            },
                        },
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
            "bridge-table": {
                "mac-learning": {
                    "admin-state": "enable",
                    "aging": {
                        "admin-state": "enable",
                        "age-time": 300,
                    },
                },
                "mac-duplication": {
                    "admin-state": "enable",
                    "monitoring-window": 3,
                    "num-moves": 5,
                    "hold-down-time": 9,
                    "action": "stop-learning",
                },
            },
        }

        updates.append({
            "path": f"/network-instance[name={bd_name}]",
            "value": ni_value,
            "op": "replace",
        })


def _get_lag_bridge_domains(hv: dict, lag_name: str) -> set[str]:
    """Return set of bridge-domain names that reference this LAG in access."""
    result: set[str] = set()
    for bd in hv.get("bridge_domains", []):
        for acc in _bd_access_interfaces(bd):
            if acc["interface"] == lag_name:
                result.add(bd["name"])
    return result


def _build_ipvrf_instances(hv: dict, updates: list[dict]) -> None:
    """Build ip-vrf network-instance entries."""
    irb_map = _irb_index_map(hv)
    vxlan_map = _vxlan_index_map(hv)

    bd_to_router: dict[str, str] = {}
    for bd in hv.get("bridge_domains", []):
        router = bd.get("router")
        if router:
            bd_to_router[bd["name"]] = router

    ri_to_router: dict[str, str] = {}
    for ri in hv.get("routed_interfaces", []):
        router = ri.get("router")
        if router:
            ri_to_router[ri["name"]] = router

    for router_def in _sorted_routers(hv):
        rname = router_def["name"]
        evi = router_def["evi"]
        vxlan_idx = vxlan_map[rname]

        ni_interfaces: list[str] = []

        for bd_name, r in sorted(bd_to_router.items()):
            if r == rname and bd_name in irb_map:
                ni_interfaces.append(f"irb0.{irb_map[bd_name]}")

        for ri in hv.get("routed_interfaces", []):
            if ri_to_router.get(ri["name"]) == rname:
                iface_name = ri.get("interface", ri["name"])
                vlan_id = ri.get("vlan_id")
                if vlan_id is None or str(vlan_id).lower() == "null":
                    sub_idx = 4097
                else:
                    sub_idx = int(vlan_id)
                ni_interfaces.append(f"{iface_name}.{sub_idx}")

        ni_value: dict[str, Any] = {
            "type": "ip-vrf",
            "admin-state": "enable",
            "description": rname,
            "interface": [{"name": iface} for iface in sorted(ni_interfaces)],
            "vxlan-interface": [{"name": f"vxlan0.{vxlan_idx}"}],
            "protocols": {
                "bgp-evpn": {
                    "bgp-instance": [{
                        "id": 1,
                        "vxlan-interface": f"vxlan0.{vxlan_idx}",
                        "evi": evi,
                        "ecmp": 8,
                        "routes": {
                            "route-table": {
                                "mac-ip": {
                                    "advertise-gateway-mac": True,
                                },
                            },
                        },
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
        }

        sr_value, nhg_value = _collect_static_routes(hv, rname)
        if sr_value:
            ni_value["static-routes"] = sr_value
        if nhg_value:
            ni_value["next-hop-groups"] = nhg_value

        updates.append({
            "path": f"/network-instance[name={rname}]",
            "value": ni_value,
            "op": "replace",
        })


def _collect_static_routes(
    hv: dict, router_name: str,
) -> tuple[dict | None, dict | None]:
    """Build static-routes and next-hop-groups for an ip-vrf.

    Returns (static_routes_value, nhg_value) -- either may be None.
    ``next-hop-groups`` lives at the NI level, not under ``static-routes``.
    """
    routes: list[dict] = []
    nhg_entries: list[dict] = []

    for sr in hv.get("static_routes", []):
        if sr.get("router") != router_name:
            continue
        nhg = sr.get("nexthop_group", {})
        nexthops = []
        for idx, nh in enumerate(nhg.get("nexthops", [])):
            nh_entry: dict[str, Any] = {
                "index": idx,
                "ip-address": nh["ip_address"],
            }
            if not nh.get("resolve", True):
                nh_entry["resolve"] = False
            nexthops.append(nh_entry)

        nhg_entries.append({
            "name": nhg["name"],
            "nexthop": nexthops,
        })

        for prefix in sr.get("prefixes", []):
            routes.append({
                "prefix": prefix,
                "next-hop-group": nhg["name"],
            })

    sr_value = {"route": routes} if routes else None
    nhg_value = {"group": nhg_entries} if nhg_entries else None
    return sr_value, nhg_value


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------

def build_routing_policy_updates(hv: dict) -> list[dict[str, Any]]:
    """Build /routing-policy with prefix-sets and import/export policies."""
    rp_cfg = hv.get("routing_policy", {})
    fabric_name = hv.get("fabric_name", "dc1")

    prefix_set_name = rp_cfg.get("prefix_set", f"prefixset-{fabric_name}")
    prefix = rp_cfg.get("prefix", hv.get("system0_prefix", ""))
    mask_range = rp_cfg.get("mask_length_range", "32..32")

    export_name = f"ebgp-isl-export-policy-{fabric_name}"
    import_name = f"ebgp-isl-import-policy-{fabric_name}"

    local_pref = {"set": 100}

    export_statements = {
        "10": {
            "match": {
                "prefix-set": prefix_set_name,
                "protocol": "local",
            },
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        },
        "15": {
            "match": {"protocol": "bgp"},
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        },
        "20": {
            "match": {"protocol": "aggregate"},
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        },
    }

    for stmt_id, rt in [("25", 1), ("30", 2), ("35", 3), ("40", 4), ("45", 5)]:
        export_statements[stmt_id] = {
            "match": {"bgp": {"evpn": {"route-type": [rt]}}},
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        }

    import_statements: dict[str, Any] = {
        "10": {
            "match": {"protocol": "bgp"},
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        },
    }
    for stmt_id, rt in [("25", 1), ("30", 2), ("35", 3), ("40", 4), ("45", 5)]:
        import_statements[stmt_id] = {
            "match": {"bgp": {"evpn": {"route-type": [rt]}}},
            "action": {
                "policy-result": "accept",
                "bgp": {"local-preference": local_pref},
            },
        }

    value: dict[str, Any] = {
        "prefix-set": [{
            "name": prefix_set_name,
            "prefix": [{
                "ip-prefix": prefix,
                "mask-length-range": mask_range,
            }],
        }],
        "policy": [
            {
                "name": export_name,
                "default-action": {"policy-result": "reject"},
                "statement": [
                    {"name": sid, **body}
                    for sid, body in export_statements.items()
                ],
            },
            {
                "name": import_name,
                "default-action": {"policy-result": "reject"},
                "statement": [
                    {"name": sid, **body}
                    for sid, body in import_statements.items()
                ],
            },
        ],
    }

    return [{"path": "/routing-policy", "value": value, "op": "replace"}]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

def build_system_updates(hv: dict, scope: str = "all") -> list[dict[str, Any]]:
    """Build /system entries.

    *scope* controls which system entries are emitted:
    - ``"topology"`` -- hostname + LLDP only
    - ``"services"`` -- ethernet-segments, event-handler, bgp-vpn instance
    - ``"all"``      -- everything (default, backward-compatible)
    """
    updates: list[dict] = []
    node = hv.get("node", {})

    if scope in ("topology", "all"):
        hostname = node.get("hostname")
        if hostname:
            updates.append({
                "path": "/system/name",
                "value": {"host-name": hostname},
                "op": "update",
            })

        all_phys = _collect_all_physical_interfaces(hv)
        if all_phys:
            updates.append({
                "path": "/system/lldp",
                "value": {
                    "interface": [{
                        "name": iface,
                        "admin-state": "enable",
                    } for iface in all_phys],
                },
                "op": "update",
            })

        mtu_cfg = hv.get("default_mtu", {})
        if mtu_cfg:
            mtu_value: dict[str, Any] = {}
            if "interface_mtu" in mtu_cfg:
                mtu_value["default-port-mtu"] = mtu_cfg["interface_mtu"]
            if "layer2_subif_mtu" in mtu_cfg:
                mtu_value["default-l2-mtu"] = mtu_cfg["layer2_subif_mtu"]
            if "layer3_mtu" in mtu_cfg:
                mtu_value["default-ip-mtu"] = mtu_cfg["layer3_mtu"]
            if mtu_value:
                updates.append({
                    "path": "/system/mtu",
                    "value": mtu_value,
                    "op": "update",
                })

    if scope in ("services", "all"):
        eh = hv.get("event_handler", {})
        ni_cfg = eh.get("node_isolation")
        if ni_cfg:
            down_links = ni_cfg.get("down_links", [])
            hold_down = ni_cfg.get("hold_down_time", 20000)
            req_sessions = ni_cfg.get("required_bgp_sessions", 1)
            updates.append({
                "path": "/system/event-handler/instance[name=overlay-bgp]",
                "value": {
                    "admin-state": "enable",
                    "upython-script": "node-isolation.py",
                    "paths": [
                        "network-instance default protocols bgp neighbor * session-state"
                    ],
                    "options": {
                        "object": [
                            {
                                "name": "down-links",
                                "values": down_links,
                            },
                            {
                                "name": "hold-down-time",
                                "value": str(hold_down),
                            },
                            {
                                "name": "required-bgp-sessions-established",
                                "value": str(req_sessions),
                            },
                        ],
                    },
                },
                "op": "replace",
            })

        _build_ethernet_segments(hv, updates)

        lags = hv.get("lags", [])
        if lags:
            updates.append({
                "path": "/system/network-instance/protocols/bgp-vpn/bgp-instance[id=1]",
                "value": {},
                "op": "update",
            })

    return updates


def _build_ethernet_segments(hv: dict, updates: list[dict]) -> None:
    """Build EVPN ethernet-segment entries for each LAG."""
    for lag in hv.get("lags", []):
        lag_name = lag["name"]
        description = lag.get("description", lag_name)
        system_id_mac = _get(lag, "lacp", "system_id_mac")
        if not system_id_mac:
            continue

        esi = _esi_from_mac(system_id_mac)
        mode = lag.get("mode", "all-active")

        if mode in ("single-active", "port-active"):
            srl_mode = "single-active"
        else:
            srl_mode = "all-active"

        es_value: dict[str, Any] = {
            "admin-state": "enable",
            "esi": esi,
            "multi-homing-mode": srl_mode,
            "interface": [{"ethernet-interface": lag_name}],
        }

        if srl_mode == "all-active":
            es_value["df-election"] = {
                "algorithm": {"type": "default"},
            }
        else:
            df_election: dict[str, Any] = {
                "interface-standby-signaling-on-non-df": {},
                "algorithm": {
                    "type": "preference",
                    "preference-alg": {
                        "preference-value": lag.get("df_preference", 100),
                        "capabilities": {
                            "non-revertive": lag.get("revertive", True) is False,
                        },
                    },
                },
            }
            es_value["df-election"] = df_election

        path = (
            "/system/network-instance/protocols/evpn/ethernet-segments"
            f"/bgp-instance[id=1]/ethernet-segment[name={description}]"
        )
        updates.append({"path": path, "value": es_value, "op": "replace"})


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


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def _intent_ni_names(hv: dict) -> set[str]:
    """Collect all network-instance names that the intent declares."""
    names: set[str] = {"default", "mgmt"}
    for bd in hv.get("bridge_domains", []):
        names.add(bd["name"])
    for r in hv.get("routers", []):
        names.add(r["name"])
    return names


def _intent_vxlan_indices(hv: dict) -> set[int]:
    """Collect all vxlan-interface indices the intent declares."""
    return set(_vxlan_index_map(hv).values())


def _intent_lag_names(hv: dict) -> set[str]:
    """Collect all LAG interface names the intent declares."""
    return {lag["name"] for lag in hv.get("lags", [])}


def build_prune_deletes(hv: dict, device_state: dict) -> list[dict[str, Any]]:
    """Compare intent against *device_state* and return delete paths for stale resources.

    *device_state* is the raw JSON dict returned by ``nokia.srlinux.get`` for
    paths ``/network-instance``, ``/tunnel-interface``, and ``/interface``.

    Purgeable resource types (never deletes protected resources):
    - network-instance (mac-vrf, ip-vrf only)
    - tunnel-interface vxlan-interface indices
    - interface (LAG interfaces only)
    """
    deletes: list[dict[str, Any]] = []

    intent_nis = _intent_ni_names(hv)
    for ni in device_state.get("network_instances", []):
        ni_name = ni.get("name", "")
        ni_type = ni.get("type", "")
        if ni_name in PROTECTED_NIS:
            continue
        if ni_type not in ("mac-vrf", "ip-vrf"):
            continue
        if ni_name not in intent_nis:
            deletes.append({
                "path": f"/network-instance[name={ni_name}]",
                "op": "delete",
            })

    intent_vxlan = _intent_vxlan_indices(hv)
    for vi in device_state.get("vxlan_interfaces", []):
        idx = vi.get("index")
        if idx is not None and int(idx) not in intent_vxlan:
            deletes.append({
                "path": f"/tunnel-interface[name=vxlan0]/vxlan-interface[index={idx}]",
                "op": "delete",
            })

    intent_lags = _intent_lag_names(hv)
    for iface in device_state.get("interfaces", []):
        iface_name = iface.get("name", "")
        if not iface_name.startswith("lag"):
            continue
        if iface_name in PROTECTED_INTERFACES:
            continue
        if iface_name not in intent_lags:
            deletes.append({
                "path": f"/interface[name={iface_name}]",
                "op": "delete",
            })

    return deletes
