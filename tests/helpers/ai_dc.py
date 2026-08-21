"""
Discovery of an AI DC rail-optimized fabric from live state.

The rail-optimized design is two fabrics sharing the same GPU servers:

  Backend  -- a RoCEv2 IP fabric.  Each GPU server puts one NIC on every rail
              leaf of its stripe, and every rail is its own routed IPv6
              subnet inside a rail VRF.  Rail N of a stripe carries GPU rank N,
              and stripes are joined by the stripe-connector spines.

  Frontend -- an EVPN-VXLAN fabric.  GPU servers and storage nodes are
              dual-homed with all-active LAGs into one storage bridge domain,
              GPU servers VLAN-tagged and storage nodes untagged.

Nothing here is read from the design inputs.  The rail matrix is rebuilt from
what the leaves actually have configured (rail VRF sub-interfaces and their
IPv6 prefixes) and from the containerlab cabling, so the tests validate the
deployed fabric rather than restating the intent that produced it.
"""

from __future__ import annotations

import ipaddress
import itertools
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from tests.conftest import ClabTopology
from tests.helpers.fcli import FcliClient
from tests.helpers.host import HostInterface, attachment_interface, host_interfaces

logger = logging.getLogger(__name__)

# ethernet-1/3.100
_ETH_SUBIF_RE = re.compile(r"^(ethernet-\d+/\d+)\.(\d+)$")
# lag1.100
_LAG_SUBIF_RE = re.compile(r"^(lag\d+)\.(\d+)$")
# fcli reports LAG members in short form: et-1/3
_SHORT_PORT_RE = re.compile(r"^et-(\d+)/(\d+)$")

UNTAGGED_SUBIF = 4096
UNTAGGED = 0


def _normalize_port(port: str) -> str:
    """Bring interface names to ``ethernet-1/3`` form."""
    short = _SHORT_PORT_RE.match(port)
    if short:
        return f"ethernet-{short.group(1)}/{short.group(2)}"
    return port.replace("ethernet-1-", "ethernet-1/") if "ethernet-1-" in port else port


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass(frozen=True)
class RailAttachment:
    """One GPU NIC on one rail of the backend fabric."""

    server: str
    server_container: str
    server_nic: str
    leaf: str
    leaf_container: str
    leaf_port: str
    subinterface: str
    vrf: str
    vlan: int
    prefix: str
    gateway: str
    ip_mtu: int
    stripe: str
    rail: str
    oper_up: bool = True
    rank: int = 0

    @property
    def label(self) -> str:
        return f"{self.server}:{self.server_nic}"

    @property
    def rail_id(self) -> str:
        return f"stripe{self.stripe}/rail{self.rail}"

    @property
    def expected_host_ip(self) -> str:
        """Host address the design allocates: the second address of the rail."""
        network = ipaddress.ip_network(self.prefix, strict=False)
        return str(network.network_address + 2)

    def host_interface(self) -> HostInterface | None:
        """The interface on the server that should carry this rail's traffic."""
        return attachment_interface(
            host_interfaces(self.server_container), self.server_nic, self.vlan,
        )

    def host_address(self) -> str | None:
        """The address the server actually holds inside this rail's prefix."""
        iface = self.host_interface()
        if iface is None:
            return None
        found = iface.addresses_in(self.prefix)
        return found[0].split("/")[0] if found else None


@dataclass(frozen=True)
class StorageAttachment:
    """One server's dual-homed attachment to the storage bridge domain."""

    server: str
    server_container: str
    bridge_domain: str
    vlan: int
    nics: tuple[str, ...]
    leaves: tuple[str, ...]
    lags: tuple[str, ...]
    subinterfaces: tuple[str, ...]
    l2_mtu: int = 0

    @property
    def label(self) -> str:
        return self.server

    @property
    def tagged(self) -> bool:
        return self.vlan != UNTAGGED

    def host_interface(self) -> HostInterface | None:
        if not self.nics:
            return None
        return attachment_interface(
            host_interfaces(self.server_container), self.nics[0], self.vlan,
        )

    def host_address(self) -> str | None:
        """The storage IPv4 address the server actually holds."""
        iface = self.host_interface()
        if iface is None:
            return None
        addrs = iface.global_addresses("inet")
        return addrs[0].split("/")[0] if addrs else None


@dataclass
class AiDcFabric:
    """The discovered rail-optimized fabric."""

    rail_vrf: str = ""
    storage_bd: str = ""
    rails: list[RailAttachment] = field(default_factory=list)
    storage: list[StorageAttachment] = field(default_factory=list)
    backend_leaves: list[str] = field(default_factory=list)
    backend_spines: list[str] = field(default_factory=list)
    frontend_leaves: list[str] = field(default_factory=list)
    frontend_spines: list[str] = field(default_factory=list)
    stripes: dict[str, list[str]] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)

    def container_of(self, node: str) -> str:
        return self.containers.get(node, node)

    # -- backend views ----------------------------------------------------

    @property
    def gpu_servers(self) -> list[str]:
        return sorted({r.server for r in self.rails})

    def rails_of_stripe(self, stripe: str) -> list[RailAttachment]:
        return [r for r in self.rails if r.stripe == stripe]

    def same_leaf_pairs(self) -> list[tuple[RailAttachment, RailAttachment]]:
        """Rail peers on one leaf: routed by that leaf, no spine involved."""
        by_leaf: dict[str, list[RailAttachment]] = defaultdict(list)
        for rail in self.rails:
            by_leaf[rail.leaf].append(rail)
        pairs = []
        for leaf in sorted(by_leaf):
            members = sorted(by_leaf[leaf], key=lambda r: r.server)
            for a, b in itertools.combinations(members, 2):
                if a.server != b.server:
                    pairs.append((a, b))
        return pairs

    def intra_stripe_pairs(self) -> list[tuple[RailAttachment, RailAttachment]]:
        """
        Cross-rail peers inside one stripe: leaf -> spine -> leaf.

        Anchored on the lowest rail of the stripe so the matrix stays linear in
        the number of rails instead of quadratic.
        """
        pairs = []
        for stripe in sorted(self.stripes):
            members = self.rails_of_stripe(stripe)
            if not members:
                continue
            anchor_rail = min((r.rail for r in members), key=_rail_sort_key)
            anchors = [r for r in members if r.rail == anchor_rail]
            anchor = sorted(anchors, key=lambda r: r.server)[0]
            for other in sorted(members, key=lambda r: (_rail_sort_key(r.rail), r.server)):
                if other.rail == anchor_rail or other.server == anchor.server:
                    continue
                pairs.append((anchor, other))
        return pairs

    def cross_stripe_rank_pairs(self) -> list[tuple[RailAttachment, RailAttachment]]:
        """
        Same GPU rank in different stripes: the collective traffic pattern the
        stripe-connector spines exist for.
        """
        by_rank: dict[int, list[RailAttachment]] = defaultdict(list)
        for rail in self.rails:
            by_rank[rail.rank].append(rail)
        pairs = []
        for rank in sorted(by_rank):
            members = sorted(by_rank[rank], key=lambda r: (r.stripe, r.server))
            for a, b in itertools.combinations(members, 2):
                if a.stripe != b.stripe:
                    pairs.append((a, b))
        return pairs

    # -- frontend views ---------------------------------------------------

    @property
    def gpu_storage_members(self) -> list[StorageAttachment]:
        return [s for s in self.storage if s.tagged]

    @property
    def storage_node_members(self) -> list[StorageAttachment]:
        return [s for s in self.storage if not s.tagged]

    def gpu_to_storage_pairs(self) -> list[tuple[StorageAttachment, StorageAttachment]]:
        """GPU server (tagged) to storage node (untagged) across the same BD."""
        return [
            (gpu, node)
            for gpu in self.gpu_storage_members
            for node in self.storage_node_members
        ]

    def storage_mesh_pairs(self) -> list[tuple[StorageAttachment, StorageAttachment]]:
        return list(itertools.combinations(self.storage_node_members, 2))

    def gpu_mesh_pairs(self) -> list[tuple[StorageAttachment, StorageAttachment]]:
        return list(itertools.combinations(self.gpu_storage_members, 2))


def _edge_port_map(topo: ClabTopology) -> dict[tuple[str, str], tuple[str, str]]:
    """``(leaf, ethernet-1/3) -> (server, eth1)`` from the containerlab cabling."""
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    for link in topo.edge_links:
        port = _normalize_port(link.get("interface", ""))
        client = link.get("client", "")
        client_intf = link.get("client_interface", "")
        if port and client:
            mapping[(link.get("node", ""), port)] = (client, client_intf)
    return mapping


def _lag_member_map(fcli: FcliClient) -> dict[tuple[str, str], list[str]]:
    """``(leaf, lag1) -> [ethernet-1/3, ...]``."""
    members: dict[tuple[str, str], list[str]] = defaultdict(list)
    try:
        rows = fcli.lag()
    except Exception as exc:
        logger.warning("Cannot read LAG state: %s", exc)
        return members
    for row in rows:
        node = str(row.get("Node", ""))
        lag = str(row.get("lag", ""))
        member = _normalize_port(str(row.get("member-itf", "")))
        if node and lag and member:
            members[(node, lag)].append(member)
    return members


def _stripe_and_rail(gateway: str) -> tuple[str, str]:
    """
    Split a rail gateway address into its stripe and rail identifiers.

    Rail addressing is ``fd00:<stripe>:<rail>:1:0:<leaf-port>:0:<host>``, so the
    second and third groups identify the stripe and the rail.  Both are written
    as decimal numbers into hex fields -- ``fd00:200:10:`` is stripe 200 rail 10
    -- so the groups are kept as the digit strings an operator reads off the
    address rather than reinterpreted as hex.

    EDA numbers rails globally across stripes (stripe 2 of an 8-rail fabric
    owns rails 9-16), which is why a rail index is not comparable across
    stripes and GPU rank has to be derived separately.
    """
    groups = ipaddress.IPv6Address(gateway).exploded.split(":")
    return groups[1].lstrip("0") or "0", groups[2].lstrip("0") or "0"


def _rail_sort_key(rail: str) -> tuple[int, str]:
    """Order rails numerically, tolerating non-numeric tokens."""
    return (int(rail), "") if rail.isdigit() else (1 << 31, rail)


def discover_ai_dc(topo: ClabTopology, fcli: FcliClient) -> AiDcFabric:
    """Rebuild the rail matrix and storage bridge domain from live state."""
    fabric = AiDcFabric()
    edge = _edge_port_map(topo)
    lag_members = _lag_member_map(fcli)

    try:
        ni_rows = fcli.network_instances()
    except Exception as exc:
        logger.warning("Cannot read network instances: %s", exc)
        return fabric

    storage_acc: dict[tuple[str, str], dict] = {}

    for row in ni_rows:
        node = str(row.get("Node", ""))
        ni = str(row.get("NI", ""))
        ni_type = str(row.get("type", ""))
        subitf = str(row.get("Subitf", ""))
        if not node or ni == "mgmt":
            continue

        if ni_type == "ip-vrf":
            match = _ETH_SUBIF_RE.match(subitf)
            if not match:
                continue
            port = match.group(1)
            server, nic = edge.get((node, port), ("", ""))
            if not server:
                continue
            prefixes = [
                p for p in _as_list(row.get("ip-prefix"))
                if ":" in p and not p.lower().startswith("fe80")
            ]
            if not prefixes:
                continue
            gateway = prefixes[0].split("/")[0]
            stripe, rail = _stripe_and_rail(gateway)
            fabric.rails.append(RailAttachment(
                server=server,
                server_container=topo.container_name(server),
                server_nic=nic,
                leaf=node,
                leaf_container=topo.container_name(node),
                leaf_port=port,
                subinterface=subitf,
                vrf=ni,
                vlan=int(row.get("vlan", UNTAGGED) or UNTAGGED),
                prefix=str(ipaddress.ip_network(prefixes[0], strict=False)),
                gateway=gateway,
                ip_mtu=int(row.get("mtu", 0) or 0),
                stripe=stripe,
                rail=rail,
                oper_up=str(row.get("if-oper", "")).lower() == "up",
            ))

        elif ni_type == "mac-vrf":
            lag_match = _LAG_SUBIF_RE.match(subitf)
            eth_match = _ETH_SUBIF_RE.match(subitf)
            if lag_match:
                parent = lag_match.group(1)
                ports = lag_members.get((node, parent), [])
            elif eth_match:
                parent = eth_match.group(1)
                ports = [parent]
            else:
                continue

            vlan = int(row.get("vlan", UNTAGGED) or UNTAGGED)
            for port in ports:
                server, nic = edge.get((node, port), ("", ""))
                if not server:
                    continue
                key = (server, ni)
                acc = storage_acc.setdefault(key, {
                    "vlan": vlan,
                    "nics": set(),
                    "leaves": set(),
                    "lags": set(),
                    "subinterfaces": set(),
                    "l2_mtu": int(row.get("mtu", 0) or 0),
                })
                acc["nics"].add(nic)
                acc["leaves"].add(node)
                acc["lags"].add(f"{node}:{parent}")
                acc["subinterfaces"].add(f"{node}:{subitf}")

    for (server, bd), acc in sorted(storage_acc.items()):
        fabric.storage.append(StorageAttachment(
            server=server,
            server_container=topo.container_name(server),
            bridge_domain=bd,
            vlan=acc["vlan"],
            nics=tuple(sorted(acc["nics"])),
            leaves=tuple(sorted(acc["leaves"])),
            lags=tuple(sorted(acc["lags"])),
            subinterfaces=tuple(sorted(acc["subinterfaces"])),
            l2_mtu=acc["l2_mtu"],
        ))

    _classify_roles(fabric, topo)
    _assign_ranks(fabric)
    return fabric


def _classify_roles(fabric: AiDcFabric, topo: ClabTopology) -> None:
    """Split the SR Linux nodes into backend/frontend leaves and spines."""
    fabric.backend_leaves = sorted({r.leaf for r in fabric.rails})
    fabric.frontend_leaves = sorted({leaf for s in fabric.storage for leaf in s.leaves})
    fabric.rail_vrf = next((r.vrf for r in fabric.rails), "")
    fabric.storage_bd = next((s.bridge_domain for s in fabric.storage), "")
    fabric.containers = {n: topo.container_name(n) for n in topo.srlinux_nodes}

    leaves = set(fabric.backend_leaves) | set(fabric.frontend_leaves)
    backend_spines: set[str] = set()
    frontend_spines: set[str] = set()
    for isl in topo.isl_links:
        for local, remote in (
            (isl["local_node"], isl["remote_node"]),
            (isl["remote_node"], isl["local_node"]),
        ):
            if local in leaves:
                continue
            if remote in fabric.backend_leaves:
                backend_spines.add(local)
            if remote in fabric.frontend_leaves:
                frontend_spines.add(local)
    fabric.backend_spines = sorted(backend_spines)
    fabric.frontend_spines = sorted(frontend_spines)

    stripes: dict[str, set[str]] = defaultdict(set)
    for rail in fabric.rails:
        stripes[rail.stripe].add(rail.leaf)
    fabric.stripes = {k: sorted(v) for k, v in sorted(stripes.items())}


def _assign_ranks(fabric: AiDcFabric) -> None:
    """
    Number each rail by its position inside its stripe.

    Rail indices are globally unique across stripes, so the GPU rank -- the
    thing that has to line up for a rail-optimized collective -- is the rail's
    ordinal within its own stripe.
    """
    rank_of: dict[tuple[str, str], int] = {}
    for stripe in fabric.stripes:
        rails = sorted(
            {r.rail for r in fabric.rails if r.stripe == stripe}, key=_rail_sort_key,
        )
        for position, rail in enumerate(rails, start=1):
            rank_of[(stripe, rail)] = position
    fabric.rails = [
        RailAttachment(**{
            **rail.__dict__,
            "rank": rank_of.get((rail.stripe, rail.rail), 0),
        })
        for rail in fabric.rails
    ]
    fabric.rails.sort(key=lambda r: (r.stripe, r.rank, r.server))
