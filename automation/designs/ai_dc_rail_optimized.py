"""
AI-DC rail-optimized design builder.

Builds a two-fabric AI datacenter from a compact input description:

* a **backend** RoCEv2 fabric of ``stripes`` stripes, each a set of
  ``rail_size`` rail leaves, interconnected by a stripe-connector spine layer.
  Every GPU server in a stripe puts exactly one NIC on each rail leaf, so rail
  *r* of every server lands on leaf *r* — the property that lets a
  rail-local collective stay inside a single leaf.
* a **frontend** EVPN-VXLAN fabric carrying storage traffic, to which the GPU
  servers and the storage nodes attach over dual-homed LAGs.

The two fabrics live in different EDA namespaces and are modelled as one
``FabricIntent``: they share the containerlab topology and the GPU servers that
straddle both, so splitting them would make the servers unrepresentable.

Everything scales from the input: stripe count, rail size, servers per stripe,
spine count and the frontend size are all free parameters. The reference
``two-stripe-rail-optimized`` inputs pin them to 2 stripes of 8 rails with 2
servers each, but nothing here is specialized to that shape.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math

from automation.core.models import (
    AiBackendIntent,
    AiGpuIsolationGroupIntent,
    AiStripeConnectorIntent,
    AiStripeIntent,
    BridgeDomainIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    DynamicLoadBalancingIntent,
    EdgeInterfaceIntent,
    FabricBfdConfig,
    FabricConfigInput,
    FabricDefinitionIntent,
    FabricInterSwitchLinksConfig,
    FabricIntent,
    FabricOverlayProtocolConfig,
    FabricUnderlayBgpConfig,
    FabricUnderlayProtocolConfig,
    ForwardingClassIntent,
    IndexPoolIntent,
    IpPoolIntent,
    LacpConfig,
    LagIntent,
    LagMember,
    LinkIntent,
    NamespaceIntent,
    NodeGroupIntent,
    NodeIntent,
    PoolAllocation,
    QueueIntent,
    Rocev2QosIntent,
    ServerBond,
    ServerIntent,
    ServerLink,
    TopologyGroupingIntent,
    TopologyGroupSelector,
    TopologyTierSelector,
    VlanIntent,
)
from automation.core.platforms import get_platform, interface_name
from automation.designs._common_builders import (
    build_credentials as _build_credentials,
    build_eda_settings as _build_eda_settings,
    increment_ip,
    merge_extras_configlets as _merge_extras_configlets,
)

logger = logging.getLogger(__name__)

DESIGN_NAME = "ai-dc-rail-optimized"

LABEL_ROLE = "eda.nokia.com/role"
LABEL_STRIPE = "eda.nokia.com/stripe"
LABEL_STRIPE_ROLE = "eda.nokia.com/stripe-role"
LABEL_NAME = "eda.nokia.com/name"
LABEL_SECURITY_PROFILE = "eda.nokia.com/security-profile"
LABEL_GPU_TENANT = "eda.nokia.com/gpu-tenant"

# The EDA aifabrics Backend reconciler keys the stripe-connector layer off this
# role, and the ISL selector off the interSwitch role.
ROLE_SPINE = "spine"
ROLE_LEAF = "leaf"
ROLE_INTER_SWITCH = "interSwitch"
STRIPE_ROLE_CONNECTOR = "inter-stripe-connector"

# Nine queues (0..8) in both a PFC and a normal flavour: SR Linux exposes eight
# forwarding classes plus the network-control class, and RoCEv2 needs the
# lossless (PFC) variant alongside the lossy one on every queue.
QUEUE_IDS = range(9)

# The ip-mtu the aifabrics Backend reconciler puts on the rail sub-interfaces
# when the Backend CR leaves ip_mtu unset. GPU NICs must not exceed it, or
# large RDMA frames are dropped on the leaf.
EDA_DEFAULT_RAIL_IP_MTU = 4136

# The forwarding-class names SR Linux ships. ForwardingClass CRs have an empty
# spec — they exist so QoS policies can reference a class by name.
FORWARDING_CLASSES = (
    "af", "be", "ef",
    "fc0", "fc1", "fc2", "fc3", "fc4", "fc5", "fc6", "fc7",
    "h1", "h2", "l1", "l2", "nc",
)

# Bandwidth strings ("400G") → Gbps, for the uplink/oversubscription maths.
_SPEED_GBPS = {
    "10G": 10, "25G": 25, "40G": 40, "50G": 50, "100G": 100,
    "200G": 200, "400G": 400, "800G": 800,
}


def _speed_to_gbps(speed: str) -> int:
    """Parse a port-speed string such as ``400G`` into Gbps."""
    try:
        return _SPEED_GBPS[speed]
    except KeyError:
        raise ValueError(
            f"Unknown port speed {speed!r}; expected one of "
            f"{', '.join(sorted(_SPEED_GBPS, key=_SPEED_GBPS.get))}"
        ) from None


def _fastest_port_speed(platform_name: str) -> str:
    """The highest port speed *platform_name* offers, as a speed string."""
    platform = get_platform(platform_name)
    return max(
        (pg.speed for pg in platform.port_groups),
        key=lambda s: _speed_to_gbps(s),
    )


def build(topology: dict, services: dict) -> FabricIntent:
    """Build a FabricIntent for the rail-optimized AI-DC design."""
    fabric_name = topology["fabric_name"]
    environment = topology.get("environment", "containerlab")

    backend_cfg = topology["backend"]
    frontend_cfg = topology.get("frontend") or {}
    backend_cfg.setdefault("mgmt_subnet", topology.get("mgmt_subnet", ""))
    frontend_cfg.setdefault("mgmt_subnet", topology.get("mgmt_subnet", ""))

    eda_settings = _build_eda_settings(topology)
    eda_cfg = topology.get("eda", {})
    backend_ns = backend_cfg.get("namespace") or eda_settings.namespace
    frontend_ns = frontend_cfg.get("namespace", f"{fabric_name}-frontend")
    # Namespace CRs are themselves namespaced: they are created inside their
    # parent, which is the EDA system namespace for a top-level fabric.
    system_ns = eda_cfg.get("system_namespace", "eda-system")
    if frontend_ns == backend_ns:
        raise ValueError(
            "frontend.namespace and backend.namespace must differ (both are "
            f"{backend_ns!r}); the two fabrics are separate EDA fabrics and "
            "cannot share a namespace"
        )
    # The intent default must be the backend namespace: resources that do not
    # pin one — and the bootstrap resources the generator derives per node
    # namespace — resolve against it.
    eda_settings.namespace = backend_ns

    backend = _BackendPlan(backend_cfg)
    frontend = _FrontendPlan(frontend_cfg, backend)

    # -- EDA scaffolding -------------------------------------------------
    namespaces = [
        NamespaceIntent(
            name=backend_ns,
            description=f"{fabric_name} AI backend fabric",
            parent_namespace=system_ns,
        ),
        NamespaceIntent(
            name=frontend_ns,
            description=f"{fabric_name} AI frontend/storage fabric",
            parent_namespace=system_ns,
        ),
    ]
    node_groups = [
        NodeGroupIntent(namespace=backend_ns),
        NodeGroupIntent(namespace=frontend_ns),
    ]
    topology_groupings = [
        _build_topology_grouping(backend, system_ns)
    ]

    # -- Allocation pools ------------------------------------------------
    # Each fabric needs its own copies: pool names are resolved inside the
    # namespace of the resource referencing them.
    index_pools = [
        IndexPoolIntent(
            name="asn-pool", namespace=backend_ns,
            start=backend.asn_start, size=backend.asn_size,
        ),
        IndexPoolIntent(
            name="asn-pool", namespace=frontend_ns,
            start=frontend.asn_start, size=frontend.asn_size,
        ),
        # The rail leaves need a *stable* index: it selects the spine port a
        # leaf's uplinks terminate on, so an index that moved between runs
        # would silently rewire the fabric.
        IndexPoolIntent(
            name="leafindex-pool", namespace=backend_ns,
            start=1, size=max(4000, backend.leaf_count + 1),
            allocations=[
                PoolAllocation(name=leaf.name, value=index)
                for index, leaf in enumerate(backend.leaves, start=1)
            ],
        ),
    ]
    ip_pools = [
        IpPoolIntent(
            name="systemipv4-pool", namespace=backend_ns,
            subnet=backend.system0_prefix,
        ),
        IpPoolIntent(
            name="systemipv4-pool", namespace=frontend_ns,
            subnet=frontend.system0_prefix,
        ),
    ]

    # -- Nodes and inter-switch links ------------------------------------
    nodes: list[NodeIntent] = []
    for leaf in backend.leaves:
        nodes.append(leaf.to_node(backend_ns))
    for spine in backend.spines:
        nodes.append(spine.to_node(backend_ns))
    for leaf in frontend.leaves:
        nodes.append(leaf.to_node(frontend_ns))
    for spine in frontend.spines:
        nodes.append(spine.to_node(frontend_ns))

    links = backend.build_links(backend_ns) + frontend.build_links(frontend_ns)

    # -- GPU-facing (rail) interfaces on the backend ----------------------
    edge_interfaces = backend.build_gpu_interfaces(backend_ns)

    # -- Frontend attachment LAGs ----------------------------------------
    lags = frontend.build_lags(frontend_ns)

    # -- The two fabric definitions --------------------------------------
    ai_backends = [_build_ai_backend(backend, backend_ns, fabric_name)]
    fabrics = [
        FabricDefinitionIntent(
            name=fabric_name,
            namespace=frontend_ns,
            system_pool_ipv4="systemipv4-pool",
            leaf_asn_pool="asn-pool",
            spine_asn_pool="asn-pool",
            leaf_node_selector=[f"{LABEL_ROLE}={ROLE_LEAF}"],
            spine_node_selector=[f"{LABEL_ROLE}={ROLE_SPINE}"],
            inter_switch_link_selector=[f"{LABEL_ROLE}={ROLE_INTER_SWITCH}"],
            fabric_config=_frontend_fabric_config(frontend),
        )
    ]

    # -- Backend QoS primitives ------------------------------------------
    queues = [
        QueueIntent(
            name=f"pfc-{qid}", namespace=backend_ns,
            queue_id=qid, queue_type="Pfc", traffic_type="Unicast",
        )
        for qid in QUEUE_IDS
    ] + [
        QueueIntent(
            name=f"unicast-{qid}", namespace=backend_ns,
            queue_id=qid, queue_type="Normal", traffic_type="Unicast",
        )
        for qid in QUEUE_IDS
    ]
    forwarding_classes = [
        ForwardingClassIntent(name=name, namespace=backend_ns)
        for name in FORWARDING_CLASSES
    ]

    # -- Frontend storage service ----------------------------------------
    # Overlay services are ordinary service inputs, and all of them belong to
    # the frontend: the Backend CR derives the backend's per-rail services
    # itself from the stripes, so it accepts no service input.
    bridge_domains, vlans = _build_storage_service(services, frontend_ns)

    # Raw-config knobs, plus whatever the user added under `extras.configlets`.
    # Extras entries pin their own namespace: they may target either fabric.
    configlets = _merge_extras_configlets(
        _build_backend_configlets(backend, backend_ns, ai_backends[0].name),
        (topology.get("extras") or {}).get("configlets", []),
    )

    servers = backend.build_servers(frontend)

    return FabricIntent(
        design=DESIGN_NAME,
        fabric_name=fabric_name,
        environment=environment,
        # These scalars describe the frontend fabric. Neither fabric takes its
        # ASNs from them — both allocate from their namespace's asn-pool — but
        # the intent model requires them and downstream reporting reads them.
        spine_asn=frontend.spine_asn,
        leaf_asn_start=frontend.leaf_asn_start,
        system0_prefix=frontend.system0_prefix,
        mgmt_subnet=topology.get("mgmt_subnet", ""),
        nodes=nodes,
        links=links,
        edge_interfaces=edge_interfaces,
        lags=lags,
        bridge_domains=bridge_domains,
        vlans=vlans,
        namespaces=namespaces,
        node_groups=node_groups,
        topology_groupings=topology_groupings,
        index_pools=index_pools,
        ip_pools=ip_pools,
        fabrics=fabrics,
        ai_backends=ai_backends,
        queues=queues,
        forwarding_classes=forwarding_classes,
        configlets=configlets,
        servers=servers,
        credentials=_build_credentials(topology),
        eda=eda_settings,
    )


# ---------------------------------------------------------------------------
# Node planning
# ---------------------------------------------------------------------------


class _PlannedNode:
    """A switch the design is about to emit, with its port budget resolved."""

    def __init__(
        self,
        *,
        name: str,
        role: str,
        platform: str,
        version: str,
        system0_ipv4: str,
        mgmt_ipv4: str,
        labels: dict[str, str],
        uplink_interfaces: list[str] | None = None,
    ) -> None:
        self.name = name
        self.role = role
        self.platform = platform
        self.version = version
        self.system0_ipv4 = system0_ipv4
        self.mgmt_ipv4 = mgmt_ipv4
        self.labels = labels
        self.uplink_interfaces = uplink_interfaces or []

    def to_node(self, namespace: str) -> NodeIntent:
        return NodeIntent(
            name=self.name,
            namespace=namespace,
            role=self.role,
            platform=self.platform,
            version=self.version,
            system0_ipv4=self.system0_ipv4,
            # ASNs come from the fabric's asn-pool, not from the intent
            # scalars, so this is a placeholder the Fabric/Backend overrides.
            asn=0,
            mgmt_ipv4=self.mgmt_ipv4,
            labels=self.labels,
            uplink_interfaces=self.uplink_interfaces,
        )


class _AddressAllocator:
    """Hands out system0 /32s and management addresses from fixed offsets.

    Offsets are explicit per node group so that adding a stripe does not
    renumber the management addresses of the ones before it — a renumber would
    force every already-onboarded TopoNode to be re-onboarded.
    """

    def __init__(self, system0_prefix: str) -> None:
        self._network = ipaddress.IPv4Network(system0_prefix)

    def system0(self, offset: int) -> str:
        return f"{self._network.network_address + offset}/32"


def _mgmt_ip(base: str, offset: int) -> str:
    """*base* management address advanced by *offset*, or "" if unset.

    Management addressing is optional: in a real deployment the nodes are
    already reachable and EDA discovers them, so only the containerlab twin
    needs these.
    """
    return increment_ip(base, offset) if base else ""


def _offset_cidr(base: str, offset: int) -> str:
    """*base* (an ``address/prefixlen``) advanced by *offset* hosts."""
    interface = ipaddress.IPv4Interface(base)
    return f"{interface.ip + offset}/{interface.network.prefixlen}"


class _BackendPlan:
    """Resolved geometry of the rail-optimized backend fabric."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.stripes: int = cfg.get("stripes", 2)
        self.rail_size: int = cfg.get("rail_size", 8)
        self.servers_per_stripe: int = cfg.get("servers_per_stripe", 2)
        self.gpu_vlan: int = cfg.get("gpu_vlan", 100)
        self.stripe_id_step: int = cfg.get("stripe_id_step", 100)
        self.gpu_tenant: str = cfg.get("gpu_tenant", "tenant1")
        self.mgmt_subnet: str = cfg.get("mgmt_subnet", "")
        self.system0_prefix: str = cfg.get("system0_prefix", "192.0.2.0/24")
        self.asn_start: int = cfg.get("asn_start", 100)
        self.asn_size: int = cfg.get("asn_size", 3000)
        self.dynamic_load_balancing: DynamicLoadBalancingIntent | None = (
            _parse_dynamic_load_balancing(cfg.get("dynamic_load_balancing", True))
        )
        self.rail_ip_mtu: int = cfg.get("ip_mtu") or EDA_DEFAULT_RAIL_IP_MTU

        if self.stripes < 1:
            raise ValueError("backend.stripes must be at least 1")
        if self.rail_size < 1:
            raise ValueError("backend.rail_size must be at least 1")
        if self.servers_per_stripe < 1:
            raise ValueError("backend.servers_per_stripe must be at least 1")

        spine_cfg = cfg.get("spine") or {}
        leaf_cfg = cfg.get("leaf") or {}
        server_cfg = cfg.get("servers") or {}
        self.spine_count: int = spine_cfg.get("count", 2)
        if self.spine_count < 1:
            raise ValueError("backend.spine.count must be at least 1")
        self.spine_platform: str = spine_cfg.get("platform", "7220 IXR-H4")
        self.leaf_platform: str = leaf_cfg.get("platform", "7220 IXR-H4")
        self.leaf_port_bandwidth: str = leaf_cfg.get("port_bandwidth", "")
        # Per-stripe platform overrides let a fabric mix hardware generations
        # (the reference runs stripe 1 on IXR-H4 and stripe 2 on IXR-H5-64D).
        self.leaf_platform_by_stripe: dict[int, str] = {
            int(k): v
            for k, v in (leaf_cfg.get("platform_per_stripe") or {}).items()
        }

        _validate_role(self.spine_platform, "spine", "backend.spine.platform")
        for stripe_num in range(1, self.stripes + 1):
            _validate_role(
                self.leaf_platform_for(stripe_num), "leaf",
                f"backend.leaf platform for stripe {stripe_num}",
            )

        self.uplinks_per_leaf = self._resolve_uplinks(cfg)
        self.leaf_count = self.stripes * self.rail_size
        self._check_capacity()

        self.server_name_template: str = server_cfg.get("name_template", "s{i}")
        self.server_mgmt_base: str = server_cfg.get("mgmt_base_ipv4", "")
        self.server_image: str = server_cfg.get("image", "")

        allocator = _AddressAllocator(self.system0_prefix)
        leaf_name_template = leaf_cfg.get("name_template", "stripe{stripe}-leaf{i}")
        leaf_mgmt_base = leaf_cfg.get("mgmt_base_ipv4", "")
        mgmt_stripe_stride = leaf_cfg.get("mgmt_stripe_stride", 10)
        leaf_version = leaf_cfg.get("version", "")

        # Leaves are numbered stripe-major so the global leaf index — and with
        # it the spine port a leaf lands on — is contiguous per stripe.
        self.leaves: list[_PlannedNode] = []
        for stripe_num in range(1, self.stripes + 1):
            for rail in range(1, self.rail_size + 1):
                index = (stripe_num - 1) * self.rail_size + rail
                name = leaf_name_template.format(i=rail, stripe=stripe_num)
                self.leaves.append(
                    _PlannedNode(
                        name=name,
                        role=ROLE_LEAF,
                        platform=self.leaf_platform_for(stripe_num),
                        version=leaf_version,
                        system0_ipv4=allocator.system0(10 + index),
                        mgmt_ipv4=_mgmt_ip(
                            leaf_mgmt_base,
                            (stripe_num - 1) * mgmt_stripe_stride + rail - 1,
                        ),
                        labels={
                            LABEL_ROLE: ROLE_LEAF,
                            LABEL_STRIPE: f"stripe{stripe_num}",
                            LABEL_NAME: name,
                            LABEL_SECURITY_PROFILE: "managed",
                        },
                        uplink_interfaces=[
                            interface_name(port)
                            for port in range(1, self.uplinks_per_leaf + 1)
                        ],
                    )
                )

        spine_name_template = spine_cfg.get("name_template", "spine{i}")
        spine_mgmt_base = spine_cfg.get("mgmt_base_ipv4", "")
        spine_version = spine_cfg.get("version", leaf_version)
        self.spines: list[_PlannedNode] = []
        for spine_num in range(1, self.spine_count + 1):
            name = spine_name_template.format(i=spine_num)
            self.spines.append(
                _PlannedNode(
                    name=name,
                    role=ROLE_SPINE,
                    platform=self.spine_platform,
                    version=spine_version,
                    system0_ipv4=allocator.system0(100 + spine_num),
                    mgmt_ipv4=_mgmt_ip(spine_mgmt_base, spine_num - 1),
                    labels={
                        LABEL_ROLE: ROLE_SPINE,
                        LABEL_STRIPE_ROLE: STRIPE_ROLE_CONNECTOR,
                        LABEL_NAME: name,
                        LABEL_SECURITY_PROFILE: "managed",
                    },
                    uplink_interfaces=[
                        interface_name(port)
                        for port in range(1, self.spine_ports_used + 1)
                    ],
                )
            )

    # -- geometry helpers ----------------------------------------------

    def leaf_platform_for(self, stripe_num: int) -> str:
        return self.leaf_platform_by_stripe.get(stripe_num, self.leaf_platform)

    def leaf_name(self, stripe_num: int, rail: int) -> str:
        """Name of the rail-*rail* leaf in stripe *stripe_num*."""
        return self.leaves[(stripe_num - 1) * self.rail_size + rail - 1].name

    def global_rail(self, stripe_num: int, rail: int) -> int:
        """Fabric-wide rail number for rail *rail* of stripe *stripe_num*.

        EDA numbers rails continuously across stripes rather than restarting at
        1 per stripe, so with a rail size of 8 stripe 2 owns rails 9..16. The
        rail number is what lands in the rail sub-interface address, so GPU NICs
        must use the global value to match the leaf.
        """
        return (stripe_num - 1) * self.rail_size + rail

    @property
    def uplink_rounds(self) -> int:
        """How many times each leaf cables to the full set of spines."""
        return self.uplinks_per_leaf // self.spine_count

    @property
    def spine_ports_used(self) -> int:
        """Spine ports consumed by leaf uplinks on each spine.

        Every leaf cables to every spine once per round, so a spine carries one
        port per leaf per round. ``_check_capacity`` has already established
        that the rounds come out whole.
        """
        return self.leaf_count * self.uplink_rounds

    def gpu_port_index(self, server_in_stripe: int) -> int:
        """Leaf port carrying the *server_in_stripe*-th GPU NIC (1-based).

        GPU ports follow the uplinks so that growing the uplink count does not
        move the server-facing ports of an existing deployment.
        """
        return self.uplinks_per_leaf + server_in_stripe

    def stripe_id(self, stripe_num: int) -> int:
        return stripe_num * self.stripe_id_step

    def _resolve_uplinks(self, cfg: dict) -> int:
        """Uplinks per rail leaf — explicit, or derived from oversubscription.

        The derivation sizes the leaf's uplink capacity against the GPU
        bandwidth it aggregates::

            uplinks = ceil(servers_per_stripe * gpu_nic_bw
                           / (oversubscription_ratio * leaf.port_bandwidth))

        The result is rounded up to a whole multiple of the spine count, since
        a leaf can only be cabled in complete rounds of the spine layer without
        loading some spines more heavily than others. Rounding up rather than
        down means the fabric always has at least the capacity asked for.

        The result is also deliberately uniform across stripes even when stripes
        run different leaf hardware: an uplink count that varied per stripe
        would make the spine-port allocation non-contiguous, and every leaf's
        spine port is derived from its global index.
        """
        explicit = cfg.get("uplinks_per_leaf")
        if explicit is not None:
            if explicit < 1:
                raise ValueError("backend.uplinks_per_leaf must be at least 1")
            return int(explicit)

        oversubscription = float(cfg.get("oversubscription_ratio", 1.0))
        if oversubscription <= 0:
            raise ValueError(
                "backend.oversubscription_ratio must be greater than 0"
            )

        gpu_nic_bw = _speed_to_gbps(
            cfg.get("gpu_nic_bandwidth")
            or _fastest_port_speed(self.leaf_platform_for(1))
        )
        uplink_bw = _speed_to_gbps(
            self.leaf_port_bandwidth or _fastest_port_speed(self.spine_platform)
        )
        derived = math.ceil(
            self.servers_per_stripe * gpu_nic_bw / (oversubscription * uplink_bw)
        )
        rounds = max(1, math.ceil(derived / self.spine_count))
        return rounds * self.spine_count

    def _check_capacity(self) -> None:
        """Fail early when the requested fabric does not fit the hardware.

        Without this the builder would happily emit port numbers past the end
        of a platform (``ethernet-1-70`` on a 64-port spine), which EDA only
        rejects once the transaction is already being applied.
        """
        leaf_count = self.leaf_count
        # Each leaf cables to every spine once per round, so an uplink count
        # that is not a whole number of rounds would leave some spines carrying
        # one more leaf port than others — an unbalanced stripe connector that
        # silently caps the fabric's bisection at the weakest spine.
        if self.uplinks_per_leaf % self.spine_count:
            raise ValueError(
                f"backend: {self.uplinks_per_leaf} uplinks per leaf does not "
                f"spread evenly over {self.spine_count} spines, so some spines "
                f"would carry more leaf ports than others. Set "
                f"backend.uplinks_per_leaf to a multiple of "
                f"backend.spine.count, or change the spine count."
            )
        ports_per_spine = self.spine_ports_used

        spine_capacity = get_platform(self.spine_platform).total_ports
        if ports_per_spine > spine_capacity:
            raise ValueError(
                f"backend: {leaf_count} leaves x {self.uplinks_per_leaf} uplinks "
                f"needs {ports_per_spine} ports on each of the {self.spine_count} "
                f"{self.spine_platform} spines, which only have {spine_capacity}. "
                f"Reduce stripes/rail_size, add spines, or raise "
                f"backend.oversubscription_ratio."
            )

        leaf_ports_needed = self.uplinks_per_leaf + self.servers_per_stripe
        for stripe_num in range(1, self.stripes + 1):
            platform_name = self.leaf_platform_for(stripe_num)
            capacity = get_platform(platform_name).total_ports
            if leaf_ports_needed > capacity:
                raise ValueError(
                    f"backend: stripe {stripe_num} leaves need "
                    f"{leaf_ports_needed} ports ({self.uplinks_per_leaf} uplinks "
                    f"+ {self.servers_per_stripe} GPU ports) but "
                    f"{platform_name} only has {capacity}."
                )

    # -- emitters -------------------------------------------------------

    def build_links(self, namespace: str) -> list[LinkIntent]:
        """Leaf↔spine ISLs, allocating spine ports by global leaf index.

        Uplink *u* of a leaf goes to spine ``u mod spine_count``; when a leaf
        has more uplinks than there are spines the extra rounds land on the
        same spines one full leaf-block further up the port range, so every
        (leaf, uplink) pair still maps to a distinct spine port.
        """
        links: list[LinkIntent] = []
        for index, leaf in enumerate(self.leaves, start=1):
            for uplink in range(self.uplinks_per_leaf):
                spine = self.spines[uplink % self.spine_count]
                round_num = uplink // self.spine_count
                spine_port = round_num * self.leaf_count + index
                links.append(
                    LinkIntent(
                        name=f"{spine.name}-{leaf.name}",
                        namespace=namespace,
                        local_node=spine.name,
                        local_interface=interface_name(spine_port),
                        remote_node=leaf.name,
                        remote_interface=interface_name(uplink + 1),
                    )
                )
        return links

    def build_gpu_interfaces(self, namespace: str) -> list[EdgeInterfaceIntent]:
        """One GPU-facing interface per (leaf, server-in-stripe) pair.

        The ``rail{r}`` role label is what makes the fabric rail-optimized: all
        NICs that share a rail carry the same label, so a GPU isolation group
        can be expressed as a single interface selector.
        """
        interfaces: list[EdgeInterfaceIntent] = []
        for server_in_stripe in range(1, self.servers_per_stripe + 1):
            port = self.gpu_port_index(server_in_stripe)
            for stripe_num in range(1, self.stripes + 1):
                for rail in range(1, self.rail_size + 1):
                    node = self.leaf_name(stripe_num, rail)
                    iface = interface_name(port)
                    interfaces.append(
                        EdgeInterfaceIntent(
                            name=f"{node}-{iface}",
                            namespace=namespace,
                            node=node,
                            interface=iface,
                            encap="dot1q",
                            labels={
                                LABEL_ROLE: f"rail{rail}",
                                LABEL_GPU_TENANT: self.gpu_tenant,
                            },
                        )
                    )
        return interfaces

    def gpu_servers(self) -> list[tuple[int, int, str]]:
        """``(stripe_num, server_in_stripe, name)`` for every GPU server."""
        servers: list[tuple[int, int, str]] = []
        number = 0
        for stripe_num in range(1, self.stripes + 1):
            for server_in_stripe in range(1, self.servers_per_stripe + 1):
                number += 1
                servers.append(
                    (
                        stripe_num,
                        server_in_stripe,
                        self.server_name_template.format(i=number),
                    )
                )
        return servers

    def build_servers(self, frontend: _FrontendPlan) -> list[ServerIntent]:
        """GPU and storage servers for the containerlab twin.

        GPU rail NICs get the addressing the fabric expects on that rail:
        ``fd00:<stripeID>:<globalRail>:1:0:<leaf-port>:0:2/96``. The leaf port is
        part of the address so two servers on the same rail (different leaf
        ports) do not collide. The rail sub-interface on the leaf is tagged with
        the stripe's GPU VLAN, so the NIC address goes on a matching VLAN
        sub-interface.
        """
        servers: list[ServerIntent] = []
        for number, (stripe_num, server_in_stripe, name) in enumerate(
            self.gpu_servers()
        ):
            stripe_id = self.stripe_id(stripe_num)
            leaf_port = self.gpu_port_index(server_in_stripe)
            links = [
                ServerLink(
                    node=self.leaf_name(stripe_num, rail),
                    interface=interface_name(leaf_port),
                    eth_index=rail,
                    ipv6_address=(
                        f"fd00:{stripe_id}:{self.global_rail(stripe_num, rail)}"
                        f":1:0:{leaf_port}:0:2/96"
                    ),
                    vlan_id=str(self.gpu_vlan),
                    mtu=self.rail_ip_mtu,
                )
                for rail in range(1, self.rail_size + 1)
            ]
            bonds: list[ServerBond] = []
            frontend_links = frontend.gpu_attachment(name)
            if frontend_links:
                first_bond_eth = self.rail_size + 1
                for offset, (leaf_name, leaf_iface) in enumerate(frontend_links):
                    links.append(
                        ServerLink(
                            node=leaf_name,
                            interface=leaf_iface,
                            eth_index=first_bond_eth + offset,
                        )
                    )
                bonds.append(
                    ServerBond(
                        name="bond0",
                        eth_indices=[
                            first_bond_eth + offset
                            for offset in range(len(frontend_links))
                        ],
                        ipv4_address=frontend.gpu_bond_address(name),
                        vlan_id=str(self.gpu_vlan),
                    )
                )
            servers.append(
                ServerIntent(
                    name=name,
                    role="gpu",
                    image=self.server_image,
                    mgmt_ipv4=_mgmt_ip(self.server_mgmt_base, number),
                    links=links,
                    bonds=bonds,
                )
            )

        servers.extend(frontend.build_storage_servers())
        return servers


class _FrontendPlan:
    """Resolved geometry of the frontend/storage EVPN-VXLAN fabric."""

    def __init__(self, cfg: dict, backend: _BackendPlan) -> None:
        leaf_cfg = cfg.get("leaf") or {}
        spine_cfg = cfg.get("spine") or {}
        storage_cfg = cfg.get("storage_servers") or {}

        self.leaf_count: int = leaf_cfg.get("count", 2)
        self.spine_count: int = spine_cfg.get("count", 2)
        self.leaf_platform: str = leaf_cfg.get("platform", "7220 IXR-D5")
        self.spine_platform: str = spine_cfg.get("platform", "7220 IXR-H5-64O")
        self.uplinks_per_leaf: int = leaf_cfg.get(
            "uplinks_per_leaf", self.spine_count
        )
        self.storage_count: int = storage_cfg.get("count", 8)
        self.storage_name_template: str = storage_cfg.get(
            "name_template", "weka{i}"
        )
        self.storage_bond_base: str = storage_cfg.get(
            "bond_base_ipv4", "172.16.10.11/24"
        )
        self.storage_mgmt_base: str = storage_cfg.get("mgmt_base_ipv4", "")
        self.storage_image: str = storage_cfg.get("image", "")
        self.gpu_bond_base: str = cfg.get("gpu_bond_base_ipv4", "172.16.10.1/24")
        self.mgmt_subnet: str = cfg.get("mgmt_subnet", backend.mgmt_subnet)
        self.system0_prefix: str = cfg.get(
            "system0_prefix", backend.system0_prefix
        )
        self.asn_start: int = cfg.get("asn_start", backend.asn_start)
        self.asn_size: int = cfg.get("asn_size", backend.asn_size)
        self.spine_asn: int = cfg.get("spine_asn", self.asn_start)
        self.leaf_asn_start: int = cfg.get("leaf_asn_start", self.asn_start)
        self.vlan_id: int = cfg.get("gpu_vlan", backend.gpu_vlan)
        self.name_prefix: str = cfg.get("name_prefix", "frontend")
        self.isl_ip_mtu: int | None = cfg.get("isl_ip_mtu")

        if self.leaf_count < 1:
            raise ValueError("frontend.leaf.count must be at least 1")
        if self.spine_count < 1:
            raise ValueError("frontend.spine.count must be at least 1")
        # Uplinks are cabled in whole rounds of the spine layer, so anything
        # that is not a multiple would leave a leaf reaching some spines more
        # often than others — or, below one round, not at all.
        if (
            self.uplinks_per_leaf < self.spine_count
            or self.uplinks_per_leaf % self.spine_count
        ):
            raise ValueError(
                f"frontend.leaf.uplinks_per_leaf ({self.uplinks_per_leaf}) must "
                f"be a positive multiple of frontend.spine.count "
                f"({self.spine_count}) so that every leaf reaches every spine "
                f"the same number of times"
            )
        _validate_role(self.leaf_platform, "leaf", "frontend.leaf.platform")
        _validate_role(self.spine_platform, "spine", "frontend.spine.platform")

        self._backend = backend
        allocator = _AddressAllocator(self.system0_prefix)

        leaf_name_template = leaf_cfg.get(
            "name_template", f"{self.name_prefix}-leaf{{i}}"
        )
        spine_name_template = spine_cfg.get(
            "name_template", f"{self.name_prefix}-spine{{i}}"
        )
        leaf_version = leaf_cfg.get("version", "")
        spine_version = spine_cfg.get("version", leaf_version)
        leaf_mgmt_base = leaf_cfg.get("mgmt_base_ipv4", "")
        spine_mgmt_base = spine_cfg.get("mgmt_base_ipv4", "")

        self.leaves = [
            _PlannedNode(
                name=leaf_name_template.format(i=n),
                role=ROLE_LEAF,
                platform=self.leaf_platform,
                version=leaf_version,
                system0_ipv4=allocator.system0(150 + n),
                mgmt_ipv4=_mgmt_ip(leaf_mgmt_base, n - 1),
                labels={
                    LABEL_ROLE: ROLE_LEAF,
                    LABEL_NAME: leaf_name_template.format(i=n),
                    LABEL_SECURITY_PROFILE: "managed",
                },
                uplink_interfaces=[
                    interface_name(port)
                    for port in range(1, self.uplinks_per_leaf + 1)
                ],
            )
            for n in range(1, self.leaf_count + 1)
        ]
        self.spines = [
            _PlannedNode(
                name=spine_name_template.format(i=n),
                role=ROLE_SPINE,
                platform=self.spine_platform,
                version=spine_version,
                system0_ipv4=allocator.system0(200 + n),
                mgmt_ipv4=_mgmt_ip(spine_mgmt_base, n - 1),
                labels={
                    LABEL_ROLE: ROLE_SPINE,
                    LABEL_NAME: spine_name_template.format(i=n),
                    LABEL_SECURITY_PROFILE: "managed",
                },
                uplink_interfaces=[
                    interface_name(port)
                    for port in range(1, self.spine_ports_used + 1)
                ],
            )
            for n in range(1, self.spine_count + 1)
        ]

        self._check_capacity()

    @property
    def uplink_rounds(self) -> int:
        """How many times each leaf cables to the full set of spines."""
        return self.uplinks_per_leaf // self.spine_count

    @property
    def spine_ports_used(self) -> int:
        """Leaf-facing ports consumed on each frontend spine."""
        return self.leaf_count * self.uplink_rounds

    # -- geometry helpers ----------------------------------------------

    @property
    def _first_access_port(self) -> int:
        """First leaf port available for server attachment.

        Frontend leaf ports 1..uplinks_per_leaf are uplinks, so access ports
        start after them.
        """
        return self.uplinks_per_leaf + 1

    def _gpu_server_names(self) -> list[str]:
        return [name for _, _, name in self._backend.gpu_servers()]

    def gpu_attachment(self, server_name: str) -> list[tuple[str, str]]:
        """``(leaf, interface)`` pairs a GPU server's frontend NICs land on.

        Every server is dual-homed across all frontend leaves, one NIC each,
        which is what the all-active LAG on the leaf side expects.
        """
        names = self._gpu_server_names()
        if server_name not in names:
            return []
        slot = names.index(server_name)
        port = self._first_access_port + slot
        return [(leaf.name, interface_name(port)) for leaf in self.leaves]

    def gpu_bond_address(self, server_name: str) -> str:
        """The GPU server's frontend bond address, counting from the base."""
        names = self._gpu_server_names()
        return _offset_cidr(self.gpu_bond_base, names.index(server_name))

    def storage_name(self, index: int) -> str:
        return self.storage_name_template.format(i=index)

    def storage_attachment(self, index: int) -> list[tuple[str, str]]:
        """``(leaf, interface)`` pairs for the *index*-th storage node (1-based)."""
        port = (
            self._first_access_port
            + len(self._gpu_server_names())
            + index - 1
        )
        return [(leaf.name, interface_name(port)) for leaf in self.leaves]

    def _check_capacity(self) -> None:
        ports_needed = (
            self.uplinks_per_leaf
            + len(self._gpu_server_names())
            + self.storage_count
        )
        capacity = get_platform(self.leaf_platform).total_ports
        if ports_needed > capacity:
            raise ValueError(
                f"frontend: each {self.leaf_platform} leaf needs "
                f"{ports_needed} ports ({self.uplinks_per_leaf} uplinks + "
                f"{len(self._gpu_server_names())} GPU servers + "
                f"{self.storage_count} storage nodes) but only has {capacity}."
            )
        spine_capacity = get_platform(self.spine_platform).total_ports
        if self.spine_ports_used > spine_capacity:
            raise ValueError(
                f"frontend: {self.leaf_count} leaves x {self.uplink_rounds} "
                f"uplink round(s) need {self.spine_ports_used} ports on each "
                f"{self.spine_platform} spine, which only has {spine_capacity}."
            )

    # -- emitters -------------------------------------------------------

    def build_links(self, namespace: str) -> list[LinkIntent]:
        """Full-mesh frontend leaf↔spine ISLs, one round per uplink group.

        With ``uplinks_per_leaf == spine_count`` this is a plain full mesh. A
        higher uplink count adds parallel links to the same spines, one extra
        round each time, so every declared uplink port is actually cabled.
        """
        links: list[LinkIntent] = []
        for leaf_num, leaf in enumerate(self.leaves, start=1):
            for uplink in range(self.uplinks_per_leaf):
                spine = self.spines[uplink % self.spine_count]
                round_num = uplink // self.spine_count
                spine_port = round_num * self.leaf_count + leaf_num
                name = f"{spine.name}-leaf{leaf_num}"
                if round_num:
                    name = f"{name}-{round_num + 1}"
                links.append(
                    LinkIntent(
                        name=name,
                        namespace=namespace,
                        local_node=spine.name,
                        local_interface=interface_name(spine_port),
                        remote_node=leaf.name,
                        remote_interface=interface_name(uplink + 1),
                    )
                )
        return links

    def build_lags(self, namespace: str) -> list[LagIntent]:
        """All-active LAGs for GPU servers (tagged) and storage nodes (untagged).

        GPU servers reach storage over the GPU VLAN, so they are attached with a
        ``tagged-v<vlan>`` label; the storage nodes carry ``untagged-v<vlan>``.
        Both are dot1q LAG interfaces — it is the VLAN CR's ``vlanID`` that
        decides tagged vs untagged, selected by these labels.
        """
        lags: list[LagIntent] = []
        aggregate_id = 0

        for name in self._gpu_server_names():
            aggregate_id += 1
            lags.append(
                self._lag(
                    namespace=namespace,
                    server_name=name,
                    attachment=self.gpu_attachment(name),
                    aggregate_id=aggregate_id,
                    labels={
                        LABEL_ROLE: "edge",
                        f"eda.nokia.com/tagged-v{self.vlan_id}": "enabled",
                    },
                )
            )

        for index in range(1, self.storage_count + 1):
            aggregate_id += 1
            lags.append(
                self._lag(
                    namespace=namespace,
                    server_name=self.storage_name(index),
                    attachment=self.storage_attachment(index),
                    aggregate_id=aggregate_id,
                    labels={
                        LABEL_ROLE: "edge",
                        f"eda.nokia.com/untagged-v{self.vlan_id}": "enabled",
                    },
                )
            )
        return lags

    def _lag(
        self,
        *,
        namespace: str,
        server_name: str,
        attachment: list[tuple[str, str]],
        aggregate_id: int,
        labels: dict[str, str],
    ) -> LagIntent:
        leaf_part = "-".join(
            leaf_name.removeprefix(f"{self.name_prefix}-")
            for leaf_name, _ in attachment
        )
        return LagIntent(
            name=f"{self.name_prefix}-{leaf_part}-{server_name}-lag",
            namespace=namespace,
            multihoming_mode="all-active",
            min_links=1,
            lacp=LacpConfig(
                interval="fast",
                system_id_mac=_lacp_system_mac(aggregate_id),
                system_priority=32768,
                admin_key=aggregate_id,
                fallback={"mode": "static", "timeout": 60},
            ),
            members=[
                LagMember(
                    node=leaf_name,
                    interface=leaf_iface,
                    aggregate_id=str(aggregate_id),
                )
                for leaf_name, leaf_iface in attachment
            ],
            labels=labels,
        )

    def build_storage_servers(self) -> list[ServerIntent]:
        """Storage nodes: one bond per node, untagged on the storage VLAN."""
        servers: list[ServerIntent] = []
        for index in range(1, self.storage_count + 1):
            attachment = self.storage_attachment(index)
            servers.append(
                ServerIntent(
                    name=self.storage_name(index),
                    role="storage",
                    image=self.storage_image,
                    mgmt_ipv4=_mgmt_ip(self.storage_mgmt_base, index - 1),
                    links=[
                        ServerLink(
                            node=leaf_name,
                            interface=leaf_iface,
                            eth_index=offset + 1,
                        )
                        for offset, (leaf_name, leaf_iface) in enumerate(attachment)
                    ],
                    bonds=[
                        ServerBond(
                            name="bond0",
                            eth_indices=list(range(1, len(attachment) + 1)),
                            ipv4_address=_offset_cidr(
                                self.storage_bond_base, index - 1
                            ),
                        )
                    ],
                )
            )
        return servers


# ---------------------------------------------------------------------------
# Fabric / Backend / service assembly
# ---------------------------------------------------------------------------


def _parse_dynamic_load_balancing(value: object) -> DynamicLoadBalancingIntent | None:
    """Read ``backend.dynamic_load_balancing`` into an intent, or ``None`` if off.

    Accepts the ``true``/``false`` shorthand as well as a mapping of explicit
    tunables, so a design can opt into ``PerPacket`` mode or retune the flowset
    without dropping to a configlet.
    """
    if value is None or value is False:
        return None
    if value is True:
        return DynamicLoadBalancingIntent()
    if isinstance(value, dict):
        options = dict(value)
        if not options.pop("enabled", True):
            return None
        return DynamicLoadBalancingIntent(**options)
    raise ValueError(
        "backend.dynamic_load_balancing must be a boolean or a mapping of "
        f"dynamic load balancing options, got {type(value).__name__}"
    )


def _validate_role(platform_name: str, role: str, field: str) -> None:
    """Reject a platform that the registry does not allow in *role*."""
    platform = get_platform(platform_name)
    if not platform.validate_role(role):
        raise ValueError(
            f"{field}: platform '{platform.name}' is not allowed as {role} "
            f"(allowed roles: {', '.join(platform.allowed_roles)})"
        )


def _lacp_system_mac(aggregate_id: int) -> str:
    """A stable, locally-administered LACP system MAC per LAG."""
    return f"00:00:00:00:00:{aggregate_id:02x}"


def _build_topology_grouping(
    backend: _BackendPlan, system_ns: str
) -> TopologyGroupingIntent:
    """The EDA UI topology view: one group per stripe, plus the connector tier."""
    group_selectors = [
        TopologyGroupSelector(
            group=f"stripe{stripe_num}",
            node_selector=[f"{LABEL_STRIPE}=stripe{stripe_num}"],
        )
        for stripe_num in range(1, backend.stripes + 1)
    ]
    group_selectors.append(
        TopologyGroupSelector(
            group="stripe-conn",
            node_selector=[f"{LABEL_STRIPE_ROLE}={STRIPE_ROLE_CONNECTOR}"],
        )
    )
    return TopologyGroupingIntent(
        name="per-rail",
        namespace=system_ns,
        group_selectors=group_selectors,
        tier_selectors=[
            TopologyTierSelector(tier=1, node_selector=[f"{LABEL_ROLE}=backbone"]),
            TopologyTierSelector(tier=2, node_selector=[f"{LABEL_ROLE}=superspine"]),
            TopologyTierSelector(tier=3, node_selector=[f"{LABEL_ROLE}={ROLE_SPINE}"]),
            TopologyTierSelector(tier=4, node_selector=[f"{LABEL_ROLE}=borderleaf"]),
            TopologyTierSelector(tier=4, node_selector=[f"{LABEL_ROLE}={ROLE_LEAF}"]),
            TopologyTierSelector(tier=4, node_selector=[f"{LABEL_ROLE}=rail"]),
            # Catch-all: must stay last, an entry without a selector matches
            # every node that no earlier tier claimed.
            TopologyTierSelector(tier=4),
        ],
        ui_name="per-rail",
        ui_description="Assign nodes to tiers and stripes from their EDA role labels.",
    )


def _build_ai_backend(
    backend: _BackendPlan, namespace: str, fabric_name: str
) -> AiBackendIntent:
    """The aifabrics Backend CR describing stripes + stripe connector."""
    cfg = backend.cfg
    qos_cfg = cfg.get("rocev2_qos") or {}
    return AiBackendIntent(
        name=f"{fabric_name}-backend",
        namespace=namespace,
        system_pool_ipv4="systemipv4-pool",
        asn_pool="asn-pool",
        ip_mtu=cfg.get("ip_mtu"),
        stripes=[
            AiStripeIntent(
                name=f"stripe{stripe_num}",
                stripe_id=backend.stripe_id(stripe_num),
                gpu_vlan=backend.gpu_vlan,
                node_selector=[f"{LABEL_STRIPE}=stripe{stripe_num}"],
            )
            for stripe_num in range(1, backend.stripes + 1)
        ],
        # One isolation group spanning every rail: all GPUs in the fabric may
        # talk to each other. Splitting the tenant label per group is how a
        # multi-tenant fabric would carve this up.
        gpu_isolation_groups=[
            AiGpuIsolationGroupIntent(
                name="all-rails",
                interface_selector=[f"{LABEL_GPU_TENANT}={backend.gpu_tenant}"],
            )
        ],
        stripe_connector=AiStripeConnectorIntent(
            name="backend",
            node_selector=[f"{LABEL_ROLE}={ROLE_SPINE}"],
            link_selector=[f"{LABEL_ROLE}={ROLE_INTER_SWITCH}"],
        ),
        rocev2_qos=Rocev2QosIntent(**qos_cfg) if qos_cfg else Rocev2QosIntent(),
        # The balancer belongs on the rail leaves; releases that render it as a
        # configlet need to know that, ones with the native block do not.
        dynamic_load_balancing=(
            dlb.model_copy(
                update={"endpoint_selector": [f"{LABEL_ROLE}={ROLE_LEAF}"]}
            )
            if (dlb := backend.dynamic_load_balancing) is not None
            else None
        ),
    )


def _build_backend_configlets(
    backend: _BackendPlan, namespace: str, backend_cr_name: str
) -> list[ConfigletIntent]:
    """Raw-config knobs the Backend CR does not expose.

    One thing a rail-optimized RoCEv2 fabric needs that has no field on the
    Backend CR:

    * **IPv6 eBGP multipath** — the rail leaves must use every spine path, and
      each path is a different AS, so multipath needs both a maximum-paths
      matching the spine count and ``allow-multiple-as``.

    Dynamic load balancing used to live here too. It is now carried as intent
    on the Backend (``dynamic_load_balancing``); the EDA generators render it
    natively where the Backend CR supports it and fall back to an equivalent
    configlet on releases that do not.
    """
    configlets: list[ConfigletIntent] = []
    leaf_selector = [f"{LABEL_ROLE}={ROLE_LEAF}"]

    # The Backend reconciler builds the egress QoS profiles below from its
    # rocev2QoS block, but leaves a few knobs at platform defaults that a
    # lossless RoCEv2 fabric has to change. The profile names it picks are
    # derived from the Backend CR's own name.
    profile = f"egress-backend-{backend_cr_name}"
    fabric_selector = [
        f"{LABEL_ROLE}={ROLE_LEAF}",
        f"{LABEL_ROLE}={ROLE_SPINE}",
    ]
    qos_entries: list[tuple[str, str, dict]] = [
        (
            "qos-mbs-q0",
            f'.qos.buffer-management.buffer-allocation-profile{{.name=="{profile}"}}.queues',
            {
                "queue": [
                    {"queue-name": "unicast-0", "maximum-burst-size": 5211064}
                ]
            },
        ),
        (
            "qos-pfc-headroom",
            '.qos.linecard{.slot==1}.forwarding-complex{.name=="0"}.input',
            {"pfc-buffer-reservation": 10},
        ),
        (
            "qos-enable-ecn-slope",
            f'.qos.buffer-management.queue-management-profile{{.name=="{profile}-2"}}.wred',
            {
                "wred-slope": [
                    {
                        "traffic-type": "all",
                        "drop-probability": "all",
                        "enable-ecn": True,
                        "min-threshold-percent": 30,
                        "max-threshold-percent": 85,
                        "slope-enabled": True,
                        "max-drop-probability-percent": 100,
                    }
                ]
            },
        ),
        # The two scheduler entries split egress bandwidth between the
        # lossless RoCEv2 queue (unicast-6, 80%) and everything else
        # (unicast-0, capped at 10%) so background traffic cannot starve
        # collectives.
        (
            "qos-unicast-6-queue",
            f'.qos.scheduler-policies.scheduler-policy{{.name=="{profile}"}}'
            f".scheduler{{.sequence==0}}",
            {
                "input": [
                    {
                        "id": "unicast-6",
                        "queue-name": "unicast-6",
                        "peak-rate-percent": 80,
                    }
                ]
            },
        ),
        (
            "qos-unicast-3-queue",
            f'.qos.scheduler-policies.scheduler-policy{{.name=="{profile}"}}'
            f".scheduler{{.sequence==1}}",
            {
                "input": [
                    {
                        "id": "unicast-0",
                        "queue-name": "unicast-0",
                        "peak-rate-percent": 10,
                        "weight": 10,
                    }
                ]
            },
        ),
    ]
    configlets.extend(
        ConfigletIntent(
            name=name,
            namespace=namespace,
            endpoint_selector=fabric_selector,
            priority=100,
            origin=DESIGN_NAME,
            configs=[
                ConfigletConfigEntry(
                    path=path,
                    operation="Update",
                    config=json.dumps(config, indent=2),
                )
            ],
        )
        for name, path, config in qos_entries
    )

    configlets.append(
        ConfigletIntent(
            name="ipv6-multipath",
            namespace=namespace,
            endpoint_selector=[
                f"{LABEL_STRIPE}=stripe{stripe_num}"
                for stripe_num in range(1, backend.stripes + 1)
            ],
            priority=100,
            origin=DESIGN_NAME,
            configs=[
                ConfigletConfigEntry(
                    path=(
                        '.network-instance{.name=="default"}.protocols.bgp'
                        '.afi-safi{.afi-safi-name=="ipv6-unicast"}'
                    ),
                    operation="Update",
                    config=json.dumps(
                        {
                            "multipath": {
                                "allow-multiple-as": True,
                                "ebgp": {
                                    "maximum-paths": backend.spine_count,
                                },
                            }
                        },
                        indent=2,
                    ),
                )
            ],
        )
    )
    return configlets


def _frontend_fabric_config(frontend: _FrontendPlan) -> FabricConfigInput:
    """eBGP underlay + eBGP overlay on IPv6-unnumbered ISLs, with BFD."""
    return FabricConfigInput(
        underlay_protocol=FabricUnderlayProtocolConfig(
            protocol=["EBGP"],
            bgp=FabricUnderlayBgpConfig(asn_pool="asn-pool"),
            bfd=FabricBfdConfig(
                enabled=True,
                detection_multiplier=3,
                min_echo_receive_interval=1000000,
                desired_min_transmit_int=1000000,
                required_min_receive=1000000,
            ),
        ),
        overlay_protocol=FabricOverlayProtocolConfig(protocol="EBGP"),
        inter_switch_links=FabricInterSwitchLinksConfig(
            unnumbered="IPV6",
            ip_mtu=frontend.isl_ip_mtu,
        ),
    )


def _build_storage_service(
    services: dict, namespace: str
) -> tuple[list[BridgeDomainIntent], list[VlanIntent]]:
    """Overlay services from the service inputs, pinned to *namespace*.

    Only the frontend fabric takes service input; the backend's per-rail
    services are derived by the Backend reconciler from the stripes.
    """
    bridge_domains = [
        BridgeDomainIntent(
            name=bd["name"],
            namespace=namespace,
            type=bd.get("type", "EVPNVXLAN"),
            vni=bd["vni"],
            evi=bd["evi"],
            mac_learning=bd.get("mac_learning", True),
            mac_aging=bd.get("mac_aging", 300),
            origin=DESIGN_NAME,
        )
        for bd in services.get("bridge_domains", [])
    ]
    vlans = [
        VlanIntent(
            name=vlan["name"],
            namespace=namespace,
            bridge_domain=vlan["bridge_domain"],
            vlan_id=str(vlan["vlan_id"]),
            interface_selector=vlan.get("interface_selector", []),
        )
        for vlan in services.get("vlans", [])
    ]
    return bridge_domains, vlans
