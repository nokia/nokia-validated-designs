"""
Core data models for the NVD automation engine.

FabricIntent is the complete, design-agnostic representation of a fabric.
Generators and executors work exclusively on this model, making them
reusable across all Nokia Validated Designs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Underlay models
# ---------------------------------------------------------------------------


class NodeIntent(BaseModel):
    """A single fabric node (leaf or spine)."""

    name: str
    role: str  # "leaf" | "spine"
    platform: str  # e.g. "7220 IXR-D3L"
    version: str  # e.g. "24.10.2"
    system0_ipv4: str  # e.g. "192.0.2.11/32"
    asn: int  # e.g. 65411
    mgmt_ipv4: str  # e.g. "172.21.21.11"
    labels: dict[str, str] = Field(default_factory=dict)
    uplink_interfaces: list[str] = Field(default_factory=list)  # ISL ports on this node


class LinkIntent(BaseModel):
    """An inter-switch link between two nodes."""

    name: str  # e.g. "leaf1-spine1"
    local_node: str
    local_interface: str  # e.g. "ethernet-1-32"
    remote_node: str
    remote_interface: str  # e.g. "ethernet-1-32" or "ethernet-1-32-1" (breakout)


class BreakoutIntent(BaseModel):
    """A breakout configuration on a specific node/interface."""

    node: str
    interface: str  # base connector, e.g. "ethernet-1-32"
    channels: int  # e.g. 4
    speed: str  # e.g. "100G"


# ---------------------------------------------------------------------------
# Edge / access models
# ---------------------------------------------------------------------------


class EdgeInterfaceIntent(BaseModel):
    """A server-facing (edge) interface on a leaf."""

    name: str  # EDA resource name, e.g. "leaf1-ethernet-1-3"
    node: str
    interface: str  # e.g. "ethernet-1-3"
    encap: str = "dot1q"  # "dot1q" | "null"
    labels: dict[str, str] = Field(default_factory=dict)


class LacpConfig(BaseModel):
    """LACP parameters for a LAG interface."""

    interval: str = "fast"  # "fast" | "slow"
    system_id_mac: str = ""  # e.g. "00:00:22:33:44:55"
    system_priority: int = 32768
    admin_key: int | None = None
    fallback: dict[str, str | int] | None = None  # {mode: "static", timeout: 60}


class LagMember(BaseModel):
    """A member interface of a LAG."""

    node: str
    interface: str  # e.g. "ethernet-1-6"
    aggregate_id: str  # e.g. "2"
    lacp_port_priority: int = 32768


class LagIntent(BaseModel):
    """A LAG interface (potentially multi-homed across multiple leaves)."""

    name: str  # e.g. "leaf2-leaf3-leaf4-leaf5-lag2"
    type: str = "lacp"
    multihoming_mode: str = "all-active"  # "all-active" | "port-active"
    min_links: int = 1
    lacp: LacpConfig = Field(default_factory=LacpConfig)
    members: list[LagMember] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    # port-active specific
    revertive: bool = False
    preferred_active_node: str = ""
    standby_signaling: str = ""  # "lacp" or ""
    reload_delay_timer: int = 100


# ---------------------------------------------------------------------------
# Service models
# ---------------------------------------------------------------------------


# EVPN route-target format, mirrors EDA's ``^target.*$`` pattern but also
# requires a colon so ``target:<asn>:<evi>`` etc. are accepted while bare
# ``target`` is rejected.
RT_PATTERN = r"^target:.+$"


class BridgeDomainIntent(BaseModel):
    """A bridge domain (mac-vrf) service.

    ``type`` defaults to ``EVPNVXLAN``; in that case ``vni`` and ``evi``
    are both required. For ``SIMPLE`` (L2-only, node-local) bridge
    domains ``vni`` and ``evi`` are omitted.
    """

    name: str  # e.g. "macvrf-v10"
    type: Literal["EVPNVXLAN", "SIMPLE"] = "EVPNVXLAN"
    vni: int | None = None  # e.g. 10010 (required when type == EVPNVXLAN)
    evi: int | None = None  # e.g. 10    (required when type == EVPNVXLAN)
    mac_learning: bool = True
    mac_aging: int = 300
    mac_duplication: dict | None = Field(
        default_factory=lambda: {
            "enabled": True,
            "hold_down_time": 9,
            "monitoring_window": 3,
            "action": "StopLearning",
            "num_moves": 5,
        }
    )
    # Optional BGP-EVPN route targets; when unset, EDA uses its server-side
    # default ``target:1:<evi>`` and the Ansible srl_builders use the same
    # string client-side so behaviour stays identical.
    export_target: str | None = Field(default=None, pattern=RT_PATTERN)
    import_target: str | None = Field(default=None, pattern=RT_PATTERN)
    origin: str = ""  # "3-stage" | "extras" — set by builder for provenance tracking

    @model_validator(mode="after")
    def _validate_type_vni_evi(self) -> BridgeDomainIntent:
        if self.type == "EVPNVXLAN":
            if self.vni is None or self.evi is None:
                raise ValueError(
                    f"BridgeDomain '{self.name}' is EVPNVXLAN but is missing vni/evi"
                )
        return self


class RouterIntent(BaseModel):
    """A router (ip-vrf) service."""

    name: str  # e.g. "vrf1"
    vni: int  # e.g. 10500
    evi: int  # e.g. 500
    node_selector: list[str] = Field(default_factory=list)  # e.g. ["eda.nokia.com/role=leaf"]
    # Optional BGP-EVPN route targets; when unset, EDA uses its server-side
    # default ``target:1:<evi>`` and the Ansible srl_builders use the same
    # string client-side so behaviour stays identical.
    export_target: str | None = Field(default=None, pattern=RT_PATTERN)
    import_target: str | None = Field(default=None, pattern=RT_PATTERN)


class IrbIpAddress(BaseModel):
    """An IP address entry for an IRB interface (supports dual-stack)."""

    ipv4: dict | None = None  # {"ip_prefix": "...", "primary": True}
    ipv6: dict | None = None  # {"ip_prefix": "...", "primary": True}


class IrbInterfaceIntent(BaseModel):
    """An IRB interface binding a bridge domain to a router."""

    name: str  # e.g. "irb-v10"
    description: str = ""  # human-readable description
    bridge_domain: str  # ref to BridgeDomainIntent.name
    router: str  # ref to RouterIntent.name

    # IP addressing — new dual-stack array (preferred)
    ip_addresses: list[IrbIpAddress] = Field(default_factory=list)
    # Legacy shorthand — used by 3-stage-evpn-vxlan design
    ipv4: str = ""  # e.g. "172.16.10.254/24"

    # Anycast gateway — required for EVPN-VXLAN IRBs where the same IP is
    # configured on every participating leaf. Default True because a
    # non-anycast IP replicated across leaves triggers duplicate-address
    # detection and all ICMP/IP traffic to the gateway is discarded.
    anycast_gw: bool = True

    # L3 proxy
    proxy_arp: bool = True
    proxy_nd: bool = False

    # Timeouts & MTU
    arp_timeout: int = 280
    ip_mtu: int = 1500

    # ARP/ND learning
    learn_unsolicited: str = "NONE"  # "NONE" | "GLOBAL" | "LINK-LOCAL" | "BOTH"

    # EVPN route control
    evpn_route_advertisement_type: dict | None = None
    host_route_populate: dict | bool | None = None

    origin: str = ""  # "3-stage" | "extras" — set by builder for provenance tracking


class VlanIntent(BaseModel):
    """A VLAN service attaching a bridge domain to interfaces via selectors."""

    name: str  # e.g. "tagged-v10"
    bridge_domain: str  # ref to BridgeDomainIntent.name
    vlan_id: str  # "10", "20", or "untagged"
    interface_selector: list[str] = Field(
        default_factory=list
    )  # e.g. ["eda.nokia.com/tagged-v10=enabled"]


class RoutedInterfaceIntent(BaseModel):
    """A routed (L3) interface on a specific node port."""

    name: str
    interface: str  # ref to EdgeInterfaceIntent.name
    router: str  # ref to RouterIntent.name
    vlan_id: str = "null"
    ipv4_addresses: list[dict] = Field(default_factory=list)  # [{ipPrefix, primary}]
    ipv6_addresses: list[dict] = Field(default_factory=list)  # [{ipPrefix, primary}]
    ip_mtu: int = 1500
    arp_timeout: int = 14400


class StaticRouteIntent(BaseModel):
    """A static route in a router."""

    name: str
    router: str  # ref to RouterIntent.name
    nodes: list[str] = Field(default_factory=list)  # specific nodes, or empty for all
    prefixes: list[str] = Field(default_factory=list)  # e.g. ["172.16.92.0/24"]
    nexthop_group: dict = Field(default_factory=dict)


class ConfigletConfigEntry(BaseModel):
    """A single configuration entry within a configlet."""

    path: str  # jspath notation, e.g. '.system.information'
    operation: str = "Update"  # "Create" | "Update" | "Delete"
    config: str  # JSON-formatted string


class ConfigletIntent(BaseModel):
    """A configlet applying raw device configuration to one or more nodes."""

    name: str  # EDA resource name, e.g. "bgp-evpn-rapid"
    configs: list[ConfigletConfigEntry] = Field(default_factory=list)
    # Target by label selector (e.g. ["eda.nokia.com/role=leaf"])
    endpoint_selector: list[str] = Field(default_factory=list)
    # Target by explicit node names (e.g. ["leaf4", "leaf5"])
    endpoints: list[str] = Field(default_factory=list)
    operating_system: str = "srl"
    priority: int = 0
    origin: str = ""  # "3-stage" | "extras" — set by builder for provenance tracking


# ---------------------------------------------------------------------------
# Default MTU
# ---------------------------------------------------------------------------


class DefaultMtuIntent(BaseModel):
    """Default MTU settings applied to fabric nodes via EDA DefaultMTU resource."""

    name: str  # EDA resource name, e.g. "default-port-mtu"
    interface_mtu: int | None = None  # e.g. 9232
    layer2_subif_mtu: int | None = None  # e.g. 9198
    layer3_mtu: int | None = None  # e.g. 9198
    node_selector: list[str] = Field(default_factory=list)  # e.g. ["eda.nokia.com/role=leaf"]
    nodes: list[str] = Field(default_factory=list)  # explicit node names


class BannerIntent(BaseModel):
    """Login and MOTD banner settings applied to fabric nodes via EDA Banner resource."""

    name: str  # EDA resource name, e.g. "nvd-banner"
    login_banner: str = ""  # displayed before login
    motd: str = ""  # displayed after login
    node_selector: list[str] = Field(default_factory=list)  # e.g. ["eda.nokia.com/role=leaf"]
    nodes: list[str] = Field(default_factory=list)  # explicit node names


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------


class PrefixEntry(BaseModel):
    """A single prefix inside a prefix-set."""

    ip_prefix: str  # e.g. "192.168.254.0/24"
    mask_length_range: str = "exact"  # e.g. "32..32", "exact"


class PrefixSetIntent(BaseModel):
    """A named set of prefixes that routing policies can match on."""

    name: str  # e.g. "prefixset-dc1"
    prefixes: list[PrefixEntry] = Field(default_factory=list)
    internal: bool = False  # True for fabric-internal defaults; EDA skips emission


Protocol = Literal["local", "bgp", "aggregate", "bgp_evpn", "static"]


class PolicyMatch(BaseModel):
    """Match criteria for a routing-policy statement."""

    prefix_set: str | None = None  # ref to PrefixSetIntent.name
    protocol: Protocol | None = None
    bgp_evpn_route_types: list[int] | None = None  # subset of [1..5]


class PolicyAction(BaseModel):
    """Action for a routing-policy statement."""

    result: Literal["accept", "reject"] = "accept"
    set_local_preference: int | None = None


class PolicyStatementIntent(BaseModel):
    """A single numbered statement in a routing policy."""

    name: str  # e.g. "10", "25"
    match: PolicyMatch = Field(default_factory=PolicyMatch)
    action: PolicyAction = Field(default_factory=PolicyAction)


class RoutingPolicyIntent(BaseModel):
    """A named routing policy made up of ordered statements."""

    name: str  # e.g. "ebgp-isl-export-policy-dc1"
    default_action: Literal["accept", "reject"] = "reject"
    statements: list[PolicyStatementIntent] = Field(default_factory=list)
    internal: bool = False  # True for fabric-internal defaults; EDA skips emission


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class Credentials(BaseModel):
    """Device credentials used for node onboarding and Ansible connections."""

    username: str = "admin"
    password: str = "NokiaSrl1!"


# ---------------------------------------------------------------------------
# EDA-specific settings
# ---------------------------------------------------------------------------


class EdaSettings(BaseModel):
    """EDA-specific configuration that doesn't apply to non-EDA mode."""

    node_profile: str = ""  # e.g. "clab-srlinux-24.10.2"
    namespace: str = "eda"


# ---------------------------------------------------------------------------
# Fabric configuration input
#
# Mirrors the EDA Fabric spec (fabrics_eda_nokia_com_v1alpha1.json). Snake-case
# input field names map 1:1 to the camelCase fields of the EDA OpenAPI schema:
#   underlay_protocol  → spec.underlayProtocol
#   overlay_protocol   → spec.overlayProtocol
#   inter_switch_links → spec.interSwitchLinks
#
# When this block is omitted on the FabricIntent, the EDA generator falls back
# to the legacy hardcoded "EBGP underlay + EBGP overlay + IPv6 unnumbered ISLs"
# defaults so existing designs (3-stage-evpn-vxlan, collapsed-spine) keep
# working unchanged.
# ---------------------------------------------------------------------------


UnderlayRoutingProtocol = Literal["EBGP", "OSPFv2", "OSPFv3"]
OverlayRoutingProtocol = Literal["IBGP", "EBGP"]
OspfAddressFamily = Literal["IPV4-UNICAST", "IPV6-UNICAST"]


class FabricBfdConfig(BaseModel):
    """BFD timers used by both underlay and overlay protocol blocks.

    Field semantics and ranges mirror EDA's underlay/overlay BFD sub-schemas
    (FabricUnderlayProtocolBfd / FabricOverlayProtocolBfd).
    """

    enabled: bool = False
    desired_min_transmit_int: int | None = Field(default=None, ge=10000, le=100000000)
    required_min_receive: int | None = Field(default=None, ge=10000, le=100000000)
    detection_multiplier: int | None = Field(default=None, ge=3, le=20)
    min_echo_receive_interval: int | None = Field(default=None, ge=0, le=100000000)
    ttl: int | None = Field(default=None, ge=2, le=255)


class FabricBgpTimersConfig(BaseModel):
    """BGP timers — applied to underlay or overlay BGP sessions."""

    connect_retry: int | None = Field(default=None, ge=1, le=65535)
    hold_time: int | None = Field(default=None, ge=0, le=65535)
    keep_alive: int | None = Field(default=None, ge=0, le=21845)
    minimum_advertisement_interval: int | None = Field(default=None, ge=1, le=255)


class FabricUnderlayBgpConfig(BaseModel):
    """Underlay-specific BGP configuration."""

    asn_pool: str | None = None  # IndexAllocationPool name; defaults to design pool
    export_policy: list[str] = Field(default_factory=list)
    import_policy: list[str] = Field(default_factory=list)
    keychain: str | None = None
    timers: FabricBgpTimersConfig | None = None


class FabricUnderlayOspfConfig(BaseModel):
    """Underlay-specific OSPF configuration."""

    address_family: list[OspfAddressFamily] = Field(default_factory=list)


class FabricUnderlayProtocolConfig(BaseModel):
    """Underlay protocol selection + per-protocol options + BFD."""

    protocol: list[UnderlayRoutingProtocol] = Field(default_factory=lambda: ["EBGP"])
    bgp: FabricUnderlayBgpConfig | None = None
    ospf: FabricUnderlayOspfConfig | None = None
    bfd: FabricBfdConfig | None = None

    @model_validator(mode="after")
    def _validate(self) -> FabricUnderlayProtocolConfig:
        if not self.protocol:
            raise ValueError("underlay_protocol.protocol must list at least one of EBGP, OSPFv2, OSPFv3")
        if len(set(self.protocol)) != len(self.protocol):
            raise ValueError("underlay_protocol.protocol entries must be unique")
        # OSPFv2 and OSPFv3 cannot be combined with each other on the same ISL.
        if "OSPFv2" in self.protocol and "OSPFv3" in self.protocol:
            raise ValueError(
                "underlay_protocol.protocol cannot contain both OSPFv2 and OSPFv3"
            )
        return self


class FabricOverlayBgpConfig(BaseModel):
    """Overlay-specific BGP configuration.

    ``autonomous_system`` and ``cluster_id`` are required when the overlay
    protocol is IBGP (validated by FabricConfigInput).
    """

    autonomous_system: int | None = None
    cluster_id: str | None = None
    export_policy: list[str] = Field(default_factory=list)
    import_policy: list[str] = Field(default_factory=list)
    keychain: str | None = None
    rr_node_selector: list[str] = Field(default_factory=list)
    rr_client_node_selector: list[str] = Field(default_factory=list)
    rr_ip_addresses: list[str] = Field(default_factory=list)
    timers: FabricBgpTimersConfig | None = None


class FabricOverlayProtocolConfig(BaseModel):
    """Overlay protocol selection + BGP + BFD.

    ``EBGP`` reuses the underlay BGP sessions; ``IBGP`` builds dedicated
    route-reflector / client peerings.
    """

    protocol: OverlayRoutingProtocol = "EBGP"
    bgp: FabricOverlayBgpConfig | None = None
    bfd: FabricBfdConfig | None = None


class FabricInterSwitchLinksConfig(BaseModel):
    """Inter-switch-link wiring options on the Fabric CR.

    EDA only supports IPv6 link-local unnumbered today; for IPv4 numbered
    ISLs leave ``unnumbered`` unset and provide ``pool_ipv4`` (the name of a
    SubnetAllocationPool resource).
    """

    unnumbered: Literal["IPV6"] | None = "IPV6"
    pool_ipv4: str | None = None
    pool_ipv6: str | None = None
    ip_mtu: int | None = Field(default=None, ge=1280, le=9486)
    vlan_id: int | None = Field(default=None, ge=1, le=4094)


class FabricConfigInput(BaseModel):
    """User-facing fabric configuration.

    Maps 1:1 to the EDA Fabric spec (``fabrics_eda_nokia_com_v1alpha1.json``)
    so what you write here lands in the generated Fabric CR with field-name
    parity (snake_case → camelCase).
    """

    underlay_protocol: FabricUnderlayProtocolConfig = Field(
        default_factory=FabricUnderlayProtocolConfig
    )
    overlay_protocol: FabricOverlayProtocolConfig = Field(
        default_factory=FabricOverlayProtocolConfig
    )
    inter_switch_links: FabricInterSwitchLinksConfig = Field(
        default_factory=FabricInterSwitchLinksConfig
    )

    @model_validator(mode="after")
    def _validate(self) -> FabricConfigInput:
        if self.overlay_protocol.protocol == "IBGP":
            bgp = self.overlay_protocol.bgp
            missing: list[str] = []
            if bgp is None or bgp.autonomous_system is None:
                missing.append("overlay_protocol.bgp.autonomous_system")
            if bgp is None or not bgp.cluster_id:
                missing.append("overlay_protocol.bgp.cluster_id")
            if missing:
                raise ValueError(
                    "Overlay protocol IBGP requires: " + ", ".join(missing)
                )
        # Mutually-exclusive ISL addressing: unnumbered IPv6 OR pool_ipv4 OR
        # pool_ipv6, but not pool_ipv4+unnumbered (pool_ipv6 is allowed
        # alongside unnumbered=IPV6 for dual-stack global addresses).
        isl = self.inter_switch_links
        if isl.unnumbered == "IPV6" and isl.pool_ipv4:
            raise ValueError(
                "inter_switch_links: cannot combine unnumbered=IPV6 with pool_ipv4; "
                "drop unnumbered to use IPv4 numbered ISLs"
            )
        return self


# ---------------------------------------------------------------------------
# Top-level intent
# ---------------------------------------------------------------------------


class FabricIntent(BaseModel):
    """
    Complete declarative intent for one fabric.

    This is the design-agnostic 'complete input' that generators and
    executors work on. It can be produced by a design-specific builder
    (from simple input) or provided directly by an advanced user.
    """

    # Identity
    design: str  # e.g. "3-stage-evpn-vxlan"
    fabric_name: str  # e.g. "dc1"
    environment: Literal["containerlab", "physical"]

    # Underlay parameters
    spine_asn: int
    leaf_asn_start: int
    system0_prefix: str  # e.g. "192.0.2.0/24"

    # Nodes and links
    nodes: list[NodeIntent] = Field(default_factory=list)
    links: list[LinkIntent] = Field(default_factory=list)
    breakouts: list[BreakoutIntent] = Field(default_factory=list)

    # Edge / access
    edge_interfaces: list[EdgeInterfaceIntent] = Field(default_factory=list)
    lags: list[LagIntent] = Field(default_factory=list)

    # Services
    bridge_domains: list[BridgeDomainIntent] = Field(default_factory=list)
    routers: list[RouterIntent] = Field(default_factory=list)
    irb_interfaces: list[IrbInterfaceIntent] = Field(default_factory=list)
    vlans: list[VlanIntent] = Field(default_factory=list)
    routed_interfaces: list[RoutedInterfaceIntent] = Field(default_factory=list)
    static_routes: list[StaticRouteIntent] = Field(default_factory=list)
    configlets: list[ConfigletIntent] = Field(default_factory=list)
    default_mtus: list[DefaultMtuIntent] = Field(default_factory=list)
    banners: list[BannerIntent] = Field(default_factory=list)

    # Routing policy
    prefix_sets: list[PrefixSetIntent] = Field(default_factory=list)
    routing_policies: list[RoutingPolicyIntent] = Field(default_factory=list)
    # Names of RoutingPolicies attached to the fabric's default eBGP group
    fabric_export_policies: list[str] = Field(default_factory=list)
    fabric_import_policies: list[str] = Field(default_factory=list)

    # Containerlab settings
    mgmt_subnet: str = ""  # e.g. "172.21.21.0/16" — used by clab generator

    # Credentials
    credentials: Credentials = Field(default_factory=Credentials)

    # EDA settings
    eda: EdaSettings = Field(default_factory=EdaSettings)

    # Optional fabric overrides (mirrors the EDA Fabric spec). When None, the
    # EDA generator falls back to the legacy EBGP-underlay/EBGP-overlay/IPv6
    # unnumbered defaults that 3-stage-evpn-vxlan and collapsed-spine rely on.
    fabric_config: FabricConfigInput | None = None

    @model_validator(mode="after")
    def validate_cross_references(self) -> FabricIntent:
        """Check that all name-based references resolve to existing objects."""
        bd_names = {bd.name for bd in self.bridge_domains}
        router_names = {r.name for r in self.routers}
        edge_names = {e.name for e in self.edge_interfaces}
        errors: list[str] = []

        for irb in self.irb_interfaces:
            if irb.bridge_domain not in bd_names:
                errors.append(
                    f"IRB '{irb.name}' references unknown bridge_domain '{irb.bridge_domain}'"
                )
            if irb.router not in router_names:
                errors.append(
                    f"IRB '{irb.name}' references unknown router '{irb.router}'"
                )

        for vlan in self.vlans:
            if vlan.bridge_domain not in bd_names:
                errors.append(
                    f"VLAN '{vlan.name}' references unknown bridge_domain '{vlan.bridge_domain}'"
                )

        edge_by_name = {e.name: e for e in self.edge_interfaces}
        for ri in self.routed_interfaces:
            if ri.interface not in edge_names:
                errors.append(
                    f"RoutedInterface '{ri.name}' references unknown interface '{ri.interface}'"
                )
            if ri.router not in router_names:
                errors.append(
                    f"RoutedInterface '{ri.name}' references unknown router '{ri.router}'"
                )
            # SR Linux consistency: a tagged (vlan-tagging=true) parent
            # interface cannot carry an untagged routed sub-interface, and
            # an untagged (vlan-tagging=false) parent cannot carry a
            # dot1q-tagged routed sub-interface. .vlan.encap.untagged is
            # supported only for bridged sub-interfaces on SR Linux.
            parent = edge_by_name.get(ri.interface)
            if parent is not None:
                ri_untagged = ri.vlan_id in (None, "null", "untagged")
                parent_untagged = parent.encap == "null"
                if parent_untagged and not ri_untagged:
                    errors.append(
                        f"RoutedInterface '{ri.name}' has vlan_id='{ri.vlan_id}' "
                        f"but parent edge interface '{parent.name}' has encap='null' "
                        f"(vlan-tagging=false). SR Linux requires a tagged parent "
                        f"(encap='dot1q') for any numerically-tagged routed sub-interface."
                    )
                elif not parent_untagged and ri_untagged:
                    errors.append(
                        f"RoutedInterface '{ri.name}' is untagged (vlan_id='{ri.vlan_id}') "
                        f"but parent edge interface '{parent.name}' has encap='dot1q' "
                        f"(vlan-tagging=true). SR Linux does not allow untagged routed "
                        f"sub-interfaces on a tagged parent; set the parent's encap to "
                        f"'null' or give the routed interface a dot1q vlan_id."
                    )

        for sr in self.static_routes:
            if sr.router not in router_names:
                errors.append(
                    f"StaticRoute '{sr.name}' references unknown router '{sr.router}'"
                )

        prefix_set_names = {ps.name for ps in self.prefix_sets}
        policy_names = {rp.name for rp in self.routing_policies}

        for rp in self.routing_policies:
            for stmt in rp.statements:
                ps = stmt.match.prefix_set
                if ps and ps not in prefix_set_names:
                    errors.append(
                        f"RoutingPolicy '{rp.name}' statement '{stmt.name}' "
                        f"references unknown prefix_set '{ps}'"
                    )

        for name in self.fabric_export_policies:
            if name not in policy_names:
                errors.append(
                    f"fabric_export_policies references unknown routing_policy '{name}'"
                )
        for name in self.fabric_import_policies:
            if name not in policy_names:
                errors.append(
                    f"fabric_import_policies references unknown routing_policy '{name}'"
                )

        if errors:
            raise ValueError(
                "Cross-reference errors:\n  " + "\n  ".join(errors)
            )
        return self
