"""
Auto-generated Pydantic v2 models for EDA fabrics API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class FabricOspf(_EDABase):
    address_family: list[str] | None = Field(None, alias="addressFamily", description="Selects enabled address families for OSPFv3. If not specified, both address families will be enabled by default when ...", title="OSPFv3 Address Family")


class FabricUnderlayProtocolBfd(_EDABase):
    desired_min_transmit_int: int | None = Field(1000000, alias="desiredMinTransmitInt", description="The minimum interval in microseconds between transmission of BFD control packets.", title="Transmit Interval", ge=10000, le=100000000)
    detection_multiplier: int | None = Field(3, alias="detectionMultiplier", description="The number of packets that must be missed to declare this session as down.", title="Multiplier", ge=3, le=20)
    enabled: bool | None = Field(False, description="Enable Biforward Detection.", title="Enabled")
    min_echo_receive_interval: int | None = Field(1000000, alias="minEchoReceiveInterval", description="The minimum interval between echo packets the local node can receive in microseconds.", title="Minimum Echo Receive Interval", ge=0, le=100000000)
    required_min_receive: int | None = Field(1000000, alias="requiredMinReceive", description="The minimum interval in microseconds between received BFD control packets that this system should support.", title="Receive Interval", ge=10000, le=100000000)
    ttl: int | None = Field(None, description="Sets custom IP TTL or Hop Limit for multi-hop BFD sessions packets. Not applicable to single-hop BFD sessions.", title="IP TTL/Hop Limit", ge=2, le=255)


class FabricUnderlayProtocol(_EDABase):
    bfd: FabricUnderlayProtocolBfd | None = Field(None, description="Enable BFD on underlay protocol", title="Underlay Protocol BFD")
    bgp: FabricBgp | None = Field(None, description="Underlay specific BGP properties.", title="BGP")
    ospf: FabricOspf | None = Field(None, description="OSPF underlay properties.", title="OSPF")
    protocol: list[str] = Field(..., description="List of routing protocols to used between peers of an ISL.  Multiple protocols may be listed, if so multiple protocol...", title="Protocol")


class FabricSuperspines(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...", title="Autonomous System Pool")
    route_leaking: FabricRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouters on each node.  If specified under the L...", title="Route Leaking")
    super_spine_node_selector: list[str] | None = Field(None, alias="superSpineNodeSelector", description="Label selector used to select Toponodes to configure as Superspine nodes.", title="Superspine Node Selector")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  This referen...", title="IPv4 Pool - System IP")
    system_pool_ipv6: str | None = Field(None, alias="systemPoolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to system/lo0 interfaces.  This referen...", title="IPv6 Pool - System IP")


class FabricSpines(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...", title="Autonomous System Pool")
    route_leaking: FabricRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouters on each node.  If specified under the L...", title="Route Leaking")
    spine_node_selector: list[str] | None = Field(None, alias="spineNodeSelector", description="Label selector used to select Toponodes to configure as Spine nodes.", title="Spine Node Selector")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  This referen...", title="IPv4 Pool - System IP")
    system_pool_ipv6: str | None = Field(None, alias="systemPoolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to system/lo0 interfaces.  This referen...", title="IPv6 Pool - System IP")


class FabricTimers(_EDABase):
    connect_retry: int | None = Field(None, alias="connectRetry", description="The time interval in seconds between successive attempts to establish a session with a peer.", title="Connect Retry", ge=1, le=65535)
    hold_time: int | None = Field(None, alias="holdTime", description="The hold-time interval in seconds that the router proposes to the peer in its OPEN message.", title="Hold Time", ge=0, le=65535)
    keep_alive: int | None = Field(None, alias="keepAlive", description="The interval in seconds between successive keepalive messages sent to the peer.", title="Keep Alive", ge=0, le=21845)
    minimum_advertisement_interval: int | None = Field(None, alias="minimumAdvertisementInterval", description="The value assigned to the MinRouteAdvertisementIntervalTimer of RFC 4271, for both EBGP and IBGP sessions.", title="Minimum Advertisement Interval", ge=1, le=255)


class FabricBgp(_EDABase):
    autonomous_system: int | None = Field(None, alias="autonomousSystem", description="Autonomous System used for iBGP peering session, when protocol is set to IBGP providing an autonomousSystem is required.", title="Autonomous System")
    cluster_id: str | None = Field(None, alias="clusterID", description="Sets the cluster ID used by DefaultRouteReflectors, when protocol is set to IBGP providing a clusterID is required.", title="Cluster ID")
    export_policy: list[str] | None = Field(None, alias="exportPolicy", description="Reference to a Policy, when left empty or not specified the Fabric will automatically generate a policy for the speci...", title="Export Policy")
    import_policy: list[str] | None = Field(None, alias="importPolicy", description="Reference to a Policy, when left empty or not specified the Fabric will automatically generate a policy for the speci...", title="Import Policy")
    keychain: str | None = Field(None, description="Keychain to be used for authentication when overlay protocol is IBGP, ignored otherwise", title="Keychain")
    rr_client_node_selector: list[str] | None = Field(None, alias="rrClientNodeSelector", description="Label selector used to select Toponodes to configure as DefaultRouteReflectorClients, these are typically Leaf or Bor...", title="Route Reflector Client Node Selector")
    rr_ip_addresses: list[str] | None = Field(None, alias="rrIPAddresses", description="List of route reflector IP addresses not provisioned by this instance of a Fabric resource.  Used with rrClientNodeSe...", title="Route Reflector IP Addresses")
    rr_node_selector: list[str] | None = Field(None, alias="rrNodeSelector", description="Label selector used to select Toponodes to configure as DefaultRouteReflectors, these are typically Spine, Superspine...", title="Route Reflector Node Selector")
    timers: FabricTimers | None = Field(None, description="Timer configurations", title="Timers")


class FabricOverlayProtocolBfd(_EDABase):
    desired_min_transmit_int: int | None = Field(1000000, alias="desiredMinTransmitInt", description="The minimum interval in microseconds between transmission of BFD control packets.", title="Transmit Interval", ge=10000, le=100000000)
    detection_multiplier: int | None = Field(3, alias="detectionMultiplier", description="The number of packets that must be missed to declare this session as down.", title="Multiplier", ge=3, le=20)
    enabled: bool | None = Field(False, description="Enable Biforward Detection.", title="Enabled")
    min_echo_receive_interval: int | None = Field(1000000, alias="minEchoReceiveInterval", description="The minimum interval between echo packets the local node can receive in microseconds.", title="Minimum Echo Receive Interval", ge=0, le=100000000)
    required_min_receive: int | None = Field(1000000, alias="requiredMinReceive", description="The minimum interval in microseconds between received BFD control packets that this system should support.", title="Receive Interval", ge=10000, le=100000000)
    ttl: int | None = Field(None, description="Sets custom IP TTL or Hop Limit for multi-hop BFD sessions packets. Not applicable to single-hop BFD sessions.", title="IP TTL/Hop Limit", ge=2, le=255)


class FabricOverlayProtocol(_EDABase):
    bfd: FabricOverlayProtocolBfd | None = Field(None, description="Enable BFD on overlay protocol", title="Overlay Protocol BFD")
    bgp: FabricBgp | None = Field(None, description="Overlay specific BGP properties.", title="BGP")
    protocol: Literal['IBGP', 'EBGP'] = Field(..., description="List of routing protocols to used to advertise EVPN routes for overlay services.  When EBGP is used, the BGP properti...", title="Protocol")


class FabricLeafs(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...", title="Autonomous System Pool")
    leaf_node_selector: list[str] | None = Field(None, alias="leafNodeSelector", description="Label selector used to select Toponodes to configure as Leaf nodes.", title="Leaf Node Selector")
    route_leaking: FabricRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouters on each node.  If specified under the L...", title="Route Leaking")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  This referen...", title="IPv4 Pool - System IP")
    system_pool_ipv6: str | None = Field(None, alias="systemPoolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to system/lo0 interfaces.  This referen...", title="IPv6 Pool - System IP")


class FabricQos(_EDABase):
    egress_policy: str | None = Field(None, alias="egressPolicy", title="Egress Policy")
    ingress_policy: str | None = Field(None, alias="ingressPolicy", title="Ingress Policy")


class FabricInterswitchlinks(_EDABase):
    ip_mtu: int | None = Field(None, alias="ipMTU", description="Sets the IP MTU for the DefaultInterface.", title="IP MTU", ge=1280, le=9486)
    link_selector: list[str] | None = Field(None, alias="linkSelector", description="Selects TopoLinks to include in this Fabric, creating an ISL resource if both Nodes in the TopoLink are part of this ...", title="Link Selector")
    pool_ipv4: str | None = Field(None, alias="poolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to DefaultInterfaces which are members ...", title="IPv4 Pool - InterSwitch Link IP")
    pool_ipv6: str | None = Field(None, alias="poolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to DefaultInterfaces which are members ...", title="IPv6 Pool - InterSwitch Link IP")
    qos: FabricQos | None = Field(None, title="QoS")
    unnumbered: Literal['IPV6'] | None = Field(None, description="Enables unnumbered interfaces on the ISL; for IPv6, only link-local addresses are used unless a PoolIPV6 is also spec...", title="Unnumbered")
    vlan_id: int | None = Field(None, alias="vlanID", description="Configures the provided VLAN on the DefaultInterfaces which are members of the ISLs.", title="VLAN ID - InterSwitch Link", ge=1, le=4094)


class FabricRouteLeaking(_EDABase):
    export_policy: str = Field(..., alias="exportPolicy", description="Reference to a Policy resource to use when evaluating route exports from the DefaultRouter.", title="Export Policy")
    import_policy: str = Field(..., alias="importPolicy", description="Reference to a Policy resource to use when evaluating route imports into the DefaultRouter.", title="Import Policy")


class FabricBorderleafs(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...", title="Autonomous System Pool")
    border_leaf_node_selector: list[str] | None = Field(None, alias="borderLeafNodeSelector", description="Label selector used to select Toponodes to configure as Borderleaf nodes.", title="Borderleaf Node Selector")
    route_leaking: FabricRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouters on each node.  If specified under the L...", title="Route Leaking")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  This referen...", title="IPv4 Pool - System IP")
    system_pool_ipv6: str | None = Field(None, alias="systemPoolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to system/lo0 interfaces.  This referen...", title="IPv6 Pool - System IP")


class FabricSpec(_EDABase):
    border_leafs: FabricBorderleafs | None = Field(None, alias="borderLeafs", title="Borderleafs")
    fabric_selector: list[str] | None = Field(None, alias="fabricSelector", description="Selects Fabric resources when connecting multiple Fabrics together. Only one Fabric needs the selector, typically the...", title="Fabric Selector")
    inter_switch_links: FabricInterswitchlinks | None = Field(None, alias="interSwitchLinks", title="InterSwitchLinks")
    leafs: FabricLeafs | None = Field(None, title="Leafs")
    overlay_protocol: FabricOverlayProtocol | None = Field(None, alias="overlayProtocol", description="Set the overlay protocol used", title="Overlay Protocol")
    route_leaking: FabricRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouters on each node.  If specified under the L...", title="Route Leaking")
    spines: FabricSpines | None = Field(None, title="Spines")
    super_spines: FabricSuperspines | None = Field(None, alias="superSpines", title="Superspines")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  If specified...", title="IPv4 Pool - System IP")
    system_pool_ipv6: str | None = Field(None, alias="systemPoolIPV6", description="Reference to an IPAllocationPool used to dynamically allocate an IPv6 address to system/lo0 interfaces.  If specified...", title="IPv6 Pool - System IP")
    underlay_protocol: FabricUnderlayProtocol | None = Field(None, alias="underlayProtocol", description="Set the underlay protocol used", title="Underlay Protocol")
