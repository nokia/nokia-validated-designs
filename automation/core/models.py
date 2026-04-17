"""
Core data models for the NVD automation engine.

FabricIntent is the complete, design-agnostic representation of a fabric.
Generators and executors work exclusively on this model, making them
reusable across all Nokia Validated Designs.
"""

from __future__ import annotations

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


class BridgeDomainIntent(BaseModel):
    """A bridge domain (mac-vrf) service."""

    name: str  # e.g. "macvrf-v10"
    vni: int  # e.g. 10010
    evi: int  # e.g. 10
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
    origin: str = ""  # "3-stage" | "extras" — set by builder for provenance tracking


class RouterIntent(BaseModel):
    """A router (ip-vrf) service."""

    name: str  # e.g. "vrf1"
    vni: int  # e.g. 10500
    evi: int  # e.g. 500
    node_selector: list[str] = Field(default_factory=list)  # e.g. ["eda.nokia.com/role=leaf"]


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

    # L3 proxy
    proxy_arp: bool = True
    proxy_nd: bool = False

    # Timeouts & MTU
    arp_timeout: int = 280
    ip_mtu: int = 1500

    # ARP/ND learning
    learn_unsolicited: str = "NONE"  # "NONE" | "GLOBAL"

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
    environment: str  # "containerlab" | "physical"

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

    # Containerlab settings
    mgmt_subnet: str = ""  # e.g. "172.21.21.0/16" — used by clab generator

    # Credentials
    credentials: Credentials = Field(default_factory=Credentials)

    # EDA settings
    eda: EdaSettings = Field(default_factory=EdaSettings)

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

        for ri in self.routed_interfaces:
            if ri.interface not in edge_names:
                errors.append(
                    f"RoutedInterface '{ri.name}' references unknown interface '{ri.interface}'"
                )
            if ri.router not in router_names:
                errors.append(
                    f"RoutedInterface '{ri.name}' references unknown router '{ri.router}'"
                )

        for sr in self.static_routes:
            if sr.router not in router_names:
                errors.append(
                    f"StaticRoute '{sr.name}' references unknown router '{sr.router}'"
                )

        if errors:
            raise ValueError(
                "Cross-reference errors:\n  " + "\n  ".join(errors)
            )
        return self
