"""
Linux host interface inspection via docker exec.

Reads ``ip -j -d addr show`` from a client container so tests can assert what
a server *actually* has configured -- VLAN tags, bond membership, MTU and
addresses -- instead of trusting the startup script that was supposed to
configure it.

Used by the AI DC validation suite, where the GPU servers carry one rail NIC
per backend leaf plus a dual-homed storage bond, and a missing VLAN tag or a
wrong prefix is indistinguishable from a fabric fault unless the host side is
inspected directly.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

UNTAGGED = 0


@dataclass(frozen=True)
class HostInterface:
    """A single interface inside a client container."""

    name: str
    kind: str = ""
    mtu: int = 0
    mac: str = ""
    oper_state: str = ""
    vlan_id: int = UNTAGGED
    lower: str = ""
    master: str = ""
    slave_state: str = ""
    addresses: tuple[str, ...] = ()

    @property
    def is_up(self) -> bool:
        return self.oper_state.upper() == "UP"

    @property
    def is_vlan(self) -> bool:
        return self.kind == "vlan"

    def addresses_in(self, network: str) -> list[str]:
        """Return the configured addresses (CIDR form) that fall inside *network*."""
        try:
            net = ipaddress.ip_network(network, strict=False)
        except ValueError:
            return []
        found = []
        for cidr in self.addresses:
            try:
                addr = ipaddress.ip_address(cidr.split("/")[0])
            except ValueError:
                continue
            if addr.version == net.version and addr in net:
                found.append(cidr)
        return found

    def global_addresses(self, family: str) -> list[str]:
        """Return globally scoped addresses of *family* (``inet`` or ``inet6``)."""
        want_v6 = family == "inet6"
        out = []
        for cidr in self.addresses:
            ip = cidr.split("/")[0]
            is_v6 = ":" in ip
            if is_v6 != want_v6:
                continue
            if is_v6 and (ip.lower().startswith("fe80") or ip == "::1"):
                continue
            if not is_v6 and ip.startswith("127."):
                continue
            out.append(cidr)
        return out


_cache: dict[str, dict[str, HostInterface]] = {}


def host_interfaces(
    container: str, *, refresh: bool = False, timeout: int = 15,
) -> dict[str, HostInterface]:
    """Return ``{ifname: HostInterface}`` for *container*, cached per container."""
    if not refresh and container in _cache:
        return _cache[container]

    result: dict[str, HostInterface] = {}
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "ip", "-j", "-d", "addr", "show"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "Cannot read interfaces on %s: %s", container, proc.stderr.strip(),
            )
            _cache[container] = result
            return result
        links = json.loads(proc.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read interfaces on %s: %s", container, exc)
        _cache[container] = result
        return result

    for link in links:
        name = link.get("ifname", "")
        if not name:
            continue
        linkinfo = link.get("linkinfo") or {}
        info_data = linkinfo.get("info_data") or {}
        slave_data = linkinfo.get("info_slave_data") or {}
        addresses = tuple(
            f"{a.get('local')}/{a.get('prefixlen')}"
            for a in link.get("addr_info", [])
            if a.get("local")
        )
        result[name] = HostInterface(
            name=name,
            kind=linkinfo.get("info_kind", ""),
            mtu=int(link.get("mtu", 0) or 0),
            mac=str(link.get("address", "") or "").lower(),
            oper_state=link.get("operstate", ""),
            vlan_id=int(info_data.get("id", UNTAGGED) or UNTAGGED),
            lower=link.get("link", "") or "",
            master=link.get("master", "") or "",
            slave_state=slave_data.get("state", "") or "",
            addresses=addresses,
        )

    _cache[container] = result
    return result


def clear_cache() -> None:
    """Drop cached interface state (call after changing host configuration)."""
    _cache.clear()


def attachment_interface(
    ifaces: dict[str, HostInterface], nic: str, vlan_id: int,
) -> HostInterface | None:
    """
    Resolve the interface that carries traffic for *nic* at *vlan_id*.

    Follows bond enslavement first (a NIC that is a bond member cannot hold
    addresses itself), then VLAN tagging.  Returns ``None`` when the expected
    interface does not exist, which is itself a meaningful test result.
    """
    base = ifaces.get(nic)
    if base is None:
        return None
    if base.master:
        base = ifaces.get(base.master, base)
    if vlan_id == UNTAGGED:
        return base
    for iface in ifaces.values():
        if iface.is_vlan and iface.vlan_id == vlan_id and iface.lower == base.name:
            return iface
    return None


def bond_members(ifaces: dict[str, HostInterface], bond: str) -> list[HostInterface]:
    """Return the interfaces enslaved to *bond*."""
    return sorted(
        (i for i in ifaces.values() if i.master == bond), key=lambda i: i.name,
    )
