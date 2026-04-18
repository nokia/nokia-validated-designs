"""Ansible filter plugin: transform EDA-like host_vars into nokia.srlinux.config payloads.

Usage in a playbook::

    - name: Build config
      set_fact:
        config: "{{ hostvars[inventory_hostname] | srl_config(sw_version, 'topology') }}"

    - name: Apply config
      nokia.srlinux.config:
        update:  "{{ config['update'] }}"
        replace: "{{ config['replace'] }}"
        delete:  "{{ config['delete'] }}"

The ``srl_builders`` package sits next to the ``filter_plugins/`` directory
(not inside it, to prevent Ansible from scanning it as a filter)::

    filter_plugins/
      srl_config.py          <-- this file
    srl_builders/
      __init__.py
      default.py
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict


def _ensure_builders_importable() -> None:
    """Make ``srl_builders`` importable from both layouts.

    In the deployed Ansible role, ``srl_builders/`` sits next to the
    ``filter_plugins/`` dir (parent of this file). In the source repo, it
    sits *alongside* this file under
    ``automation/generators/ansible_filter_plugins/``. Both locations are
    added to ``sys.path``.
    """
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(plugin_dir)
    for path in (plugin_dir, project_dir):
        if path not in sys.path:
            sys.path.insert(0, path)


_INFRA_PATH_PREFIXES = ("/interface", "/tunnel-interface")


def _split_by_op(entries: list[dict]) -> dict[str, list[dict]]:
    """Partition builder output into ``nokia.srlinux.config`` buckets.

    Returns ``{"update": [...], "replace": [...], "delete": [...]}``.

    The per-entry ``op`` is authoritative: builders know whether a given
    path should merge (``update``) or overwrite children (``replace``),
    and upgrading an ``update`` to a ``replace`` on a parent path like
    ``/interface[name=ethernet-1/6]`` wipes unmodelled children (e.g.
    subinterfaces still referenced by a network-instance), which the
    device then rejects at commit time.

    The prefix-based routing is kept only as a fallback for legacy
    entries missing an ``op`` field.
    """
    buckets: dict[str, list[dict]] = {"update": [], "replace": [], "delete": []}
    for entry in entries:
        op = entry.get("op")
        item = {"path": entry["path"]}
        if "value" in entry:
            item["value"] = entry["value"]
        if op == "delete":
            buckets["delete"].append(item)
        elif op == "replace":
            buckets["replace"].append(item)
        elif op == "update":
            buckets["update"].append(item)
        elif any(entry["path"].startswith(p) for p in _INFRA_PATH_PREFIXES):
            buckets["replace"].append(item)
        else:
            buckets["update"].append(item)
    return buckets


def _labels_match(selectors: list[str], labels: dict[str, str]) -> bool:
    """Return True if any selector matches the labels (OR semantics).

    Each selector is a ``key=value`` string.  A match occurs when the
    label dict contains the key with the exact value.
    """
    for sel in selectors:
        key, _, value = sel.strip().partition("=")
        if labels.get(key.strip()) == value.strip():
            return True
    return False


def _resolve_services(hv: dict) -> dict:
    """Resolve selector-based group_vars services into per-node format.

    When ``vlans`` is present in *hv*, the function performs selector-based
    resolution of bridge_domains, routers, and VLANs, producing the same
    per-node ``bridge_domains`` format (with ``access``, ``irb``, ``router``)
    that the srl_builder already expects.

    When ``vlans`` is absent (legacy pre-resolved projects), returns *hv*
    unchanged for full backward compatibility.
    """
    if "vlans" not in hv:
        return hv

    hv = dict(hv)
    node_labels = hv.get("node", {}).get("labels", {})
    edges = hv.get("edge_interfaces", [])
    lags = hv.get("lags", [])
    vlans = hv.get("vlans", [])
    irb_by_bd = {i["bridge_domain"]: i for i in hv.get("irb_interfaces", [])}

    routers = [
        r for r in hv.get("routers", [])
        if _labels_match(r.get("node_selector", []), node_labels)
    ]
    router_names = {r["name"] for r in routers}
    hv["routers"] = routers

    bd_access: dict[str, list[dict]] = defaultdict(list)
    for vlan in vlans:
        selectors = vlan.get("interface_selector", [])
        for ei in edges:
            if _labels_match(selectors, ei.get("labels", {})):
                bd_access[vlan["bridge_domain"]].append(
                    {"interface": ei["name"], "vlan": vlan["vlan_id"]}
                )
        for lag in lags:
            if _labels_match(selectors, lag.get("labels", {})):
                bd_access[vlan["bridge_domain"]].append(
                    {"interface": lag["name"], "vlan": vlan["vlan_id"]}
                )

    resolved_bds = []
    for bd in hv.get("bridge_domains", []):
        bd_name = bd["name"]
        irb = irb_by_bd.get(bd_name)
        has_access = bd_name in bd_access
        has_irb_on_this_node = irb is not None and irb.get("router") in router_names
        if not has_access and not has_irb_on_this_node:
            continue
        entry = dict(bd)
        if has_access:
            entry["access"] = bd_access[bd_name]
        if irb and has_irb_on_this_node:
            entry["irb"] = {
                k: v for k, v in irb.items()
                if k not in ("name", "bridge_domain", "router")
            }
            entry["router"] = irb["router"]
        resolved_bds.append(entry)
    hv["bridge_domains"] = resolved_bds

    del hv["vlans"]
    return hv


def srl_config(
    host_vars: dict,
    sw_version: str = "24.10.2",
    phase: str = "all",
) -> dict[str, list[dict]]:
    """Transform EDA-like host_vars intent into nokia.srlinux.config payloads.

    Returns ``{"update": [...], "replace": [...], "delete": []}``.

    Infrastructure paths (interfaces, subinterfaces, tunnel-interfaces) are
    placed in ``replace`` so the module creates them *before* the ``update``
    entries (BFD, NIs, routing-policy, etc.) that reference them.

    *phase* selects which configuration scope to build:
    - ``"topology"`` -- underlay interfaces, system0, BFD, hostname, LLDP
    - ``"fabric"``   -- default NI (BGP), routing-policy
    - ``"services"`` -- edge, LAG, IRB, VXLAN, mac-vrf, ip-vrf, ES, event-handler
    - ``"all"``      -- everything (default)
    """
    _ensure_builders_importable()
    from srl_builders import get_builder

    host_vars = _resolve_services(host_vars)
    builder = get_builder(sw_version)

    if phase == "topology":
        raw = builder.build_topology_updates(host_vars)
    elif phase == "fabric":
        raw = builder.build_fabric_updates(host_vars)
    elif phase == "services":
        raw = builder.build_services_updates(host_vars)
    else:
        raw = []
        raw += builder.build_interface_updates(host_vars)
        raw += builder.build_subinterface_updates(host_vars)
        raw += builder.build_bfd_updates(host_vars)
        raw += builder.build_tunnel_interface_updates(host_vars)
        raw += builder.build_network_instance_updates(host_vars)
        raw += builder.build_routing_policy_updates(host_vars)
        raw += builder.build_system_updates(host_vars)

    return _split_by_op(raw)


def srl_config_deletes(
    host_vars: dict,
    device_state: dict,
    sw_version: str = "24.10.2",
) -> list[dict]:
    """Compare intent against device state and return delete entries for stale resources.

    *device_state* must already be normalized (see ``srl_normalize_state``).
    Returns a list of ``{"path": ...}`` dicts (no ``value`` key -- deletes only).
    """
    _ensure_builders_importable()
    from srl_builders import get_builder

    host_vars = _resolve_services(host_vars)
    builder = get_builder(sw_version)
    raw = builder.build_prune_deletes(host_vars, device_state)
    return [{"path": e["path"]} for e in raw]


def _strip_yang_prefix(value: str) -> str:
    """Strip YANG module prefix from a value like ``srl_nokia-network-instance:mac-vrf``."""
    if ":" in value:
        return value.split(":", 1)[1]
    return value


def _find_list_in_dict(d: dict) -> list:
    """Extract the first list value from a dict, ignoring YANG-prefixed keys."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, list):
                return v
    return []


def srl_normalize_state(get_result: list) -> dict:
    """Normalize raw ``nokia.srlinux.get`` result into a clean device_state dict.

    Expects *get_result* to be the ``.result`` list from a ``nokia.srlinux.get``
    call with three paths (in order):

    1. ``/network-instance``  (datastore: running)
    2. ``/tunnel-interface[name=vxlan0]/vxlan-interface``  (datastore: running)
    3. ``/interface``  (datastore: running)

    The raw JSON-RPC response wraps data in YANG module-prefixed keys
    (e.g., ``srl_nokia-network-instance:network-instance``) and uses prefixed
    enum values (e.g., ``srl_nokia-network-instance:mac-vrf``).  This filter
    strips those prefixes so ``build_prune_deletes`` can compare plain names.

    Returns::

        {
            "network_instances": [{"name": "...", "type": "mac-vrf"}, ...],
            "vxlan_interfaces":  [{"index": 500}, ...],
            "interfaces":        [{"name": "lag1"}, ...],
        }
    """
    if not isinstance(get_result, list) or len(get_result) < 3:
        return {"network_instances": [], "vxlan_interfaces": [], "interfaces": []}

    raw_nis = _find_list_in_dict(get_result[0]) if get_result[0] else []
    raw_vxlan = _find_list_in_dict(get_result[1]) if get_result[1] else []
    raw_ifaces = _find_list_in_dict(get_result[2]) if get_result[2] else []

    network_instances = []
    for ni in raw_nis:
        if not isinstance(ni, dict):
            continue
        name = ni.get("name", "")
        ni_type = _strip_yang_prefix(str(ni.get("type", "")))
        network_instances.append({"name": name, "type": ni_type})

    vxlan_interfaces = []
    for vi in raw_vxlan:
        if not isinstance(vi, dict):
            continue
        idx = vi.get("index")
        if idx is not None:
            vxlan_interfaces.append({"index": int(idx)})

    interfaces = []
    for iface in raw_ifaces:
        if not isinstance(iface, dict):
            continue
        name = iface.get("name", "")
        if name:
            interfaces.append({"name": name})

    return {
        "network_instances": network_instances,
        "vxlan_interfaces": vxlan_interfaces,
        "interfaces": interfaces,
    }


class FilterModule:
    """Ansible filter plugin registration."""

    def filters(self):
        return {
            "srl_config": srl_config,
            "srl_config_deletes": srl_config_deletes,
            "srl_normalize_state": srl_normalize_state,
        }
