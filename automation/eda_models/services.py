"""
Auto-generated Pydantic v2 models for EDA services API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class RoutedInterfaceL3ProxyArpNd(_EDABase):
    proxy_arp: bool | None = Field(False, alias="proxyARP", description="Select whether Proxy ARP should be enabled.", title="Proxy ARP Enabled")
    proxy_nd: bool | None = Field(False, alias="proxyND", description="Select whether Proxy ND should be enabled.", title="Proxy ND Enabled")


class RoutedInterfacePrefixes(_EDABase):
    autonomous_flag: bool | None = Field(True, alias="autonomousFlag", description="When this is set in the prefix information option hosts can use the prefix for stateless address autoconfiguration (S...", title="Autonomous Flag")
    on_link_flag: bool | None = Field(True, alias="onLinkFlag", description="When this is set in the prefix information option hosts can use the prefix for on-link determination.", title="On-Link Flag")
    preferred_lifetime: int | None = Field(604800, alias="preferredLifetime", description="The length of time in seconds (relative to the time the packet is sent) that addresses generated from the prefix via ...", title="Preferred Lifetime", ge=0, le=4294967295)
    prefix: str = Field(..., description="An IPv6 global unicast address prefix.", title="IPv6 Prefix")
    valid_lifetime: int | None = Field(2592000, alias="validLifetime", description="The length of time in seconds (relative to the time the packet is sent) that the prefix is valid for the purpose of o...", title="Valid Lifetime", ge=0, le=4294967295)


class RoutedInterfaceIpv6RouterAdvertisement(_EDABase):
    current_hop_limit: int = Field(..., alias="currentHopLimit", description="The current hop limit to advertise in the router advertisement messages.", title="Current Hop Limit", ge=0, le=255)
    enabled: bool = Field(..., description="Enable or disable IPv6 router advertisements.", title="Enable Router Advertisements")
    ip_mtu: int | None = Field(None, alias="ipMTU", description="The IP MTU to advertise in the router advertisement messages.", title="IP MTU", ge=1280, le=9486)
    managed_configuration_flag: bool = Field(..., alias="managedConfigurationFlag", description="Enable DHCPv6 for address configuration (M-bit).", title="Managed Configuration Flag")
    max_advertisement_interval: int = Field(..., alias="maxAdvertisementInterval", description="Maximum time between router advertisements (in seconds).", title="Maximum Advertisement Interval", ge=4, le=1800)
    min_advertisement_interval: int = Field(..., alias="minAdvertisementInterval", description="Minimum time between router advertisements (in seconds).", title="Minimum Advertisement Interval", ge=3, le=1350)
    other_configuration_flag: bool = Field(..., alias="otherConfigurationFlag", description="Enable DHCPv6 for other configuration (O-bit).", title="Other Configuration Flag")
    prefixes: list[RoutedInterfacePrefixes] | None = Field(None, description="IPv6 prefixes to advertise in router advertisements.", title="Prefixes")
    reachable_time: int | None = Field(0, alias="reachableTime", description="Time in milliseconds for Neighbor Unreachability Detection.", title="Reachable Time", ge=0, le=3600000)
    retransmit_time: int = Field(..., alias="retransmitTime", description="Time in milliseconds between retransmitted NS messages.", title="Retransmit Time", ge=0, le=1800000)
    router_lifetime: int = Field(..., alias="routerLifetime", description="Router lifetime in seconds for default gateway.", title="Router Lifetime", ge=0, le=9000)


class RoutedInterfaceIpv6Addresses(_EDABase):
    ip_prefix: str = Field(..., alias="ipPrefix", description="Address and mask to use", title="IP Prefix")
    primary: bool | None = Field(None, description="Indicates which address to use as primary for broadcast", title="Primary")


class RoutedInterfaceIpv4SpecificParameters(_EDABase):
    directed_broadcast: bool | None = Field(None, alias="directedBroadcast", description="Allow receiving and forwarding of directed broadcast packets. Enabled when set to true.", title="Directed Broadcast")


class RoutedInterfaceIpv4Addresses(_EDABase):
    ip_prefix: str = Field(..., alias="ipPrefix", description="Address and mask to use", title="IP Prefix")
    primary: bool | None = Field(None, description="Indicates which address to use as primary for broadcast", title="Primary")


class RoutedInterfaceIngressActions(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at ingress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Ingress policy references to use at ingress.", title="QoS Ingress Policy")


class RoutedInterfaceStatic(_EDABase):
    datapath_programming: bool | None = Field(None, alias="datapathProgramming", description="Enable datapath programming for host routes.", title="Datapath Programming")
    populate: bool = Field(..., description="Enable population of host routes based on ARP/ND entries.", title="Populate")


class RoutedInterfaceEvpnLearned(_EDABase):
    datapath_programming: bool | None = Field(None, alias="datapathProgramming", description="Enable datapath programming for host routes.", title="Datapath Programming")
    populate: bool = Field(..., description="Enable population of host routes based on ARP/ND entries.", title="Populate")


class RoutedInterfaceDynamic(_EDABase):
    datapath_programming: bool | None = Field(None, alias="datapathProgramming", description="Enable datapath programming for host routes.", title="Datapath Programming")
    populate: bool = Field(..., description="Enable population of host routes based on ARP/ND entries.", title="Populate")


class RoutedInterfaceHostRoutes(_EDABase):
    dynamic: RoutedInterfaceDynamic | None = Field(None, description="Create host routes out of dynamic ARP/ND entries.", title="Dynamic")
    evpn: RoutedInterfaceEvpnLearned | None = Field(None, description="Create host routes out of EVPN learned ARP/ND entries.", title="EVPN Learned")
    static: RoutedInterfaceStatic | None = Field(None, description="Create host routes out of static ARP/ND entries.", title="Static")


class RoutedInterfaceEgressActions(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at egress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Egress policy references to use at egress.", title="QoS Egress Policy")


class RoutedInterfaceBfdConfiguration(_EDABase):
    desired_min_transmit_int: int | None = Field(1000000, alias="desiredMinTransmitInt", description="The minimum interval in microseconds between transmission of BFD control packets.", title="Transmit Interval", ge=10000, le=100000000)
    detection_multiplier: int | None = Field(3, alias="detectionMultiplier", description="The number of packets that must be missed to declare this session as down.", title="Multiplier", ge=3, le=20)
    enabled: bool = Field(..., description="Enables Biforward Detection.", title="Enabled")
    min_echo_receive_interval: int | None = Field(0, alias="minEchoReceiveInterval", description="The minimum interval between echo packets the local node can receive.", title="Minimum Echo Receive Interval", ge=0, le=100000000)
    required_min_receive: int | None = Field(1000000, alias="requiredMinReceive", description="The minimum interval in microseconds between received BFD control packets that this system should support.", title="Receive Interval", ge=10000, le=100000000)
    ttl: int | None = Field(None, description="Sets custom IP TTL or Hop Limit for multi-hop BFD sessions packets. Not applicable to single-hop BFD sessions.", title="IP TTL/Hop Limit", ge=2, le=255)


class RoutedInterfaceSpec(_EDABase):
    arp_timeout: int | None = Field(14400, alias="arpTimeout", description="Duration of time that dynamic ARP entries remain in the ARP cache before they expire.", title="ARP Timeout")
    bfd: RoutedInterfaceBfdConfiguration | None = Field(None, description="Enables BFD on the RoutedInterface.", title="BFD Configuration")
    description: str | None = Field(None, description="The description of the RoutedInterface.", title="Description")
    egress: RoutedInterfaceEgressActions | None = Field(None, description="Manages actions on traffic at Egress.", title="Egress Actions")
    host_route_populate: RoutedInterfaceHostRoutes | None = Field(None, alias="hostRoutePopulate", description="Configures host route population based on ARP/ND entries.", title="Host Routes")
    ingress: RoutedInterfaceIngressActions | None = Field(None, description="Manages actions on traffic at Ingress.", title="Ingress Actions")
    interface: str = Field(..., description="Reference to an Interface to use for attachment.", title="Interface")
    ip_mtu: int | None = Field(1500, alias="ipMTU", description="IP MTU for the RoutedInterface.", title="IP MTU", ge=1280, le=9486)
    ipv4_addresses: list[RoutedInterfaceIpv4Addresses] | None = Field(None, alias="ipv4Addresses", description="List of IPv4 addresses in IP/mask form, e.g., 192.168.0.1/24.", title="IPv4 Addresses")
    ipv4_parameters: RoutedInterfaceIpv4SpecificParameters | None = Field(None, alias="ipv4Parameters", title="IPv4-specific Parameters")
    ipv6_addresses: list[RoutedInterfaceIpv6Addresses] | None = Field(None, alias="ipv6Addresses", description="List of IPv6 addresses in IP/mask form, e.g., fc00::1/120.", title="IPv6 Addresses")
    ipv6_router_advertisement: RoutedInterfaceIpv6RouterAdvertisement | None = Field(None, alias="ipv6RouterAdvertisement", description="Manages IPV6 Router Advertisement parameters.", title="IPv6 Router Advertisement")
    l3_proxy_arpnd: RoutedInterfaceL3ProxyArpNd | None = Field(None, alias="l3ProxyARPND", description="L3 Proxy ARP and ND configuration.", title="L3 Proxy ARP/ND")
    learn_unsolicited: Literal['BOTH', 'GLOBAL', 'LINK-LOCAL', 'NONE'] | None = Field('NONE', alias="learnUnsolicited", description="Enable or disable learning of unsolicited ARPs.", title="Learn Unsolicited ARPs")
    router: str = Field(..., description="Reference to a Router.", title="Router")
    unnumbered: Literal['IPV6'] | None = Field(None, description="Enables the use of unnumbered interfaces on the IRBInterface.  If IPv6 is specified, no IP address are configured on ...", title="Unnumbered")
    vlan_id: str | None = Field('pool', alias="vlanID", description="Single value between 1-4094 support, ranges supported in the format x-y,x-y, or the special keyword null, any, untagg...", title="VLAN ID")
    vlan_pool: str | None = Field('vlan-pool', alias="vlanPool", description="Reference to a VLAN pool to use for allocations.", title="VLAN Pool")


class VLANUplink(_EDABase):
    egress: VLANEgress | None = Field(None, description="Manages actions on traffic at Egress of the Local endpoint of the Uplink.", title="Egress")
    ingress: VLANIngress | None = Field(None, description="Manages actions on traffic at Ingress of the Local endpoint of the Uplink.", title="Ingress")
    uplink_selector: list[str] | None = Field(None, alias="uplinkSelector", description="Selects TopoLinks which connect a leaf switch to a breakout switch. This is the uplink between your access breakout s...", title="Uplink Selector")
    uplink_vlanid: str | None = Field('pool', alias="uplinkVLANID", description="The VLAN ID to be utilized to isolate traffic from the VLAN on the access breakout switch to the leaf switch on the s...", title="Uplink VLAN ID")
    uplink_vlan_pool: str | None = Field(None, alias="uplinkVLANPool", description="A VLAN from this pool will be utilized to isolate traffic from the VLAN on the access breakout switch to the leaf swi...", title="Uplink VLAN Pool")


class VLANIngress(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at ingress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Ingress policy references to use at ingress.", title="QoS Ingress Policy")


class VLANEgress(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at egress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Egress policy references to use at egress.", title="QoS Egress Policy")


class VLANSpec(_EDABase):
    bridge_domain: str = Field(..., alias="bridgeDomain", description="Reference to a BridgeDomain or SimpleBridgeDomain.", title="Bridge Domain")
    description: str | None = Field(None, description="The description of the VLAN.", title="Description")
    egress: VLANEgress | None = Field(None, description="Manages actions on traffic at Egress.", title="Egress")
    ingress: VLANIngress | None = Field(None, description="Manages actions on traffic at Ingress.", title="Ingress")
    interface_selector: list[str] = Field(..., alias="interfaceSelector", description="Interfaces to use for attachment to this VLAN based on the label selector.  Selects Interfaces based on their associa...", title="Interface Selector")
    l2_mtu: int | None = Field(None, alias="l2MTU", description="L2 MTU specifies the maximum sized Ethernet frame that can be transmitted on the subinterface. If a frame exceeds thi...", title="L2 MTU", ge=1450, le=9500)
    mac_duplication_detection_action: Literal['Blackhole', 'OperDown', 'StopLearning', 'UseBridgeDomainAction'] | None = Field(None, alias="macDuplicationDetectionAction", description="If Mac Duplication Detection is enabled on the associated Bridge Domain, this property will override the MDD action s...", title="MAC Duplication Detection Action")
    split_horizon_group: str | None = Field(None, alias="splitHorizonGroup", description="Name of the Split Horizon Group to be used for this VLAN.  All subinterfaces within this VLAN will be members of this...", title="Split Horizon Group")
    uplink: VLANUplink | None = Field(None, description="The Uplink between your access breakout switch and your leaf switch.", title="Uplink")
    vlan_id: str | None = Field('pool', alias="vlanID", description="Single value between 1-4094 support, ranges supported in the format x-y,x-y, or the special keyword null, any, untagg...", title="VLAN ID")
    vlan_pool: str | None = Field(None, alias="vlanPool", description="Reference to a VLAN pool to use for allocations. [default=\"vlan-pool\"]", title="VLAN Pool")


class IRBInterfaceVirtualIpDiscovery(_EDABase):
    address: str = Field(..., description="Virtual IP Address.", title="Address")
    allowed_mac: list[str] | None = Field(None, alias="allowedMAC", description="List of allowed MAC addresses for a discovered virtual IP address.", title="Allowed MAC Addresses")
    bridge_interface_to_probe: list[str] | None = Field(None, alias="bridgeInterfaceToProbe", description="List of BridgeInterfaces on the associated MAC-VRF to which the ARP probes are sent. If left blank, the probes are se...", title="Bridge Interfaces to Probe")
    probe_interval: int | None = Field(0, alias="probeInterval", description="ARP probe interval in seconds.", title="Probe Interval", ge=0, le=86400)
    vlan_to_probe: list[str] | None = Field(None, alias="vlanToProbe", description="List of VLANs on the associated BridgeDomain to which the ARP probes are sent.  If left blank, the probes are sent on...", title="VLANs to Probe")


class IRBInterfaceL3ProxyArpNd(_EDABase):
    proxy_arp: bool | None = Field(False, alias="proxyARP", description="Select whether Proxy ARP should be enabled.", title="Proxy ARP Enabled")
    proxy_nd: bool | None = Field(False, alias="proxyND", description="Select whether Proxy ND should be enabled.", title="Proxy ND Enabled")


class IRBInterfacePrefixes(_EDABase):
    autonomous_flag: bool | None = Field(True, alias="autonomousFlag", description="When this is set in the prefix information option hosts can use the prefix for stateless address autoconfiguration (S...", title="Autonomous Flag")
    on_link_flag: bool | None = Field(True, alias="onLinkFlag", description="When this is set in the prefix information option hosts can use the prefix for on-link determination.", title="On-Link Flag")
    preferred_lifetime: int | None = Field(604800, alias="preferredLifetime", description="The length of time in seconds (relative to the time the packet is sent) that addresses generated from the prefix via ...", title="Preferred Lifetime", ge=0, le=4294967295)
    prefix: str = Field(..., description="An IPv6 global unicast address prefix.", title="IPv6 Prefix")
    valid_lifetime: int | None = Field(2592000, alias="validLifetime", description="The length of time in seconds (relative to the time the packet is sent) that the prefix is valid for the purpose of o...", title="Valid Lifetime", ge=0, le=4294967295)


class IRBInterfaceIpv6RouterAdvertisement(_EDABase):
    current_hop_limit: int = Field(..., alias="currentHopLimit", description="The current hop limit to advertise in the router advertisement messages.", title="Current Hop Limit", ge=0, le=255)
    enabled: bool = Field(..., description="Enable or disable IPv6 router advertisements.", title="Enable Router Advertisements")
    ip_mtu: int | None = Field(None, alias="ipMTU", description="The IP MTU to advertise in the router advertisement messages.", title="IP MTU", ge=1280, le=9486)
    managed_configuration_flag: bool = Field(..., alias="managedConfigurationFlag", description="Enable DHCPv6 for address configuration (M-bit).", title="Managed Configuration Flag")
    max_advertisement_interval: int = Field(..., alias="maxAdvertisementInterval", description="Maximum time between router advertisements (in seconds).", title="Maximum Advertisement Interval", ge=4, le=1800)
    min_advertisement_interval: int = Field(..., alias="minAdvertisementInterval", description="Minimum time between router advertisements (in seconds).", title="Minimum Advertisement Interval", ge=3, le=1350)
    other_configuration_flag: bool = Field(..., alias="otherConfigurationFlag", description="Enable DHCPv6 for other configuration (O-bit).", title="Other Configuration Flag")
    prefixes: list[IRBInterfacePrefixes] | None = Field(None, description="IPv6 prefixes to advertise in router advertisements.", title="Prefixes")
    reachable_time: int | None = Field(0, alias="reachableTime", description="Time in milliseconds for Neighbor Unreachability Detection.", title="Reachable Time", ge=0, le=3600000)
    retransmit_time: int = Field(..., alias="retransmitTime", description="Time in milliseconds between retransmitted NS messages.", title="Retransmit Time", ge=0, le=1800000)
    router_lifetime: int = Field(..., alias="routerLifetime", description="Router lifetime in seconds for default gateway.", title="Router Lifetime", ge=0, le=9000)


class IRBInterfaceIpv4SpecificParameters(_EDABase):
    directed_broadcast: bool | None = Field(None, alias="directedBroadcast", description="Allow receiving and forwarding of directed broadcast packets. Enabled when set to true.", title="Directed Broadcast")


class IRBInterfaceIpv6Addresses(_EDABase):
    ip_prefix: str = Field(..., alias="ipPrefix", description="Address and mask to use", title="IP Prefix")
    primary: bool | None = Field(None, description="Indicates which address to use as primary for broadcast", title="Primary")


class IRBInterfaceIpv4Addresses(_EDABase):
    ip_prefix: str = Field(..., alias="ipPrefix", description="Address and mask to use", title="IP Prefix")
    primary: bool | None = Field(None, description="Indicates which address to use as primary for broadcast", title="Primary")


class IRBInterfaceIpAddresses(_EDABase):
    ipv4_address: IRBInterfaceIpv4Addresses | None = Field(None, alias="ipv4Address", description="IPv4 address in IP/mask form, e.g., 192.168.0.1/24.", title="IPv4 Addresses")
    ipv6_address: IRBInterfaceIpv6Addresses | None = Field(None, alias="ipv6Address", description="IPv6 address in IP/mask form, e.g., fc00::1/120.", title="IPv6 Addresses")
    node: str | None = Field(None, description="Reference to a TopoNode resource, if not specified the IP address will be assigned to all nodes on which the IRB is d...", title="Node")


class IRBInterfaceIngressActions(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at ingress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Ingress policy references to use at ingress.", title="QoS Ingress Policy")


class IRBInterfaceHostRoutePopulation(_EDABase):
    dynamic: bool | None = Field(True, description="Create host routes out of dynamic ARP entries.", title="Dynamic ARP Entries")
    evpn: bool | None = Field(False, description="Create host routes out of EVPN learned ARP entries.", title="EVPN Learned ARP Entries")
    static: bool | None = Field(True, description="Create host routes out of static ARP entries.", title="Static ARP Entries")


class IRBInterfaceEvpnRouteAdvertisementType(_EDABase):
    arp_dynamic: bool | None = Field(False, alias="arpDynamic", description="Advertise dynamic ARP entries.", title="Advertise Dynamic ARP")
    arp_static: bool | None = Field(False, alias="arpStatic", description="Advertise static ARP entries.", title="Advertise Static ARP")
    nd_dynamic: bool | None = Field(False, alias="ndDynamic", description="Advertise dynamic ND entries.", title="Advertise Dynamic ND")
    nd_static: bool | None = Field(False, alias="ndStatic", description="Advertise static ND entries.", title="Advertise Static ND")
    rfc9135_symmetric_mode: bool | None = Field(None, alias="rfc9135SymmetricMode", description="Use RFC9135-based symmetric mode for ARP/ND host route advertisements.", title="Use RFC9135 Symmetric Mode")


class IRBInterfaceEgressActions(_EDABase):
    filters: list[str] | None = Field(None, description="List of Filter references to use at egress.", title="Filters")
    qos_policy: list[str] | None = Field(None, alias="qosPolicy", description="List of QoS Egress policy references to use at egress.", title="QoS Egress Policy")


class IRBInterfaceBfdConfiguration(_EDABase):
    desired_min_transmit_int: int | None = Field(1000000, alias="desiredMinTransmitInt", description="The minimum interval in microseconds between transmission of BFD control packets.", title="Transmit Interval", ge=10000, le=100000000)
    detection_multiplier: int | None = Field(3, alias="detectionMultiplier", description="The number of packets that must be missed to declare this session as down.", title="Multiplier", ge=3, le=20)
    enabled: bool = Field(..., description="Enables Biforward Detection.", title="Enabled")
    min_echo_receive_interval: int | None = Field(0, alias="minEchoReceiveInterval", description="The minimum interval between echo packets the local node can receive.", title="Minimum Echo Receive Interval", ge=0, le=100000000)
    required_min_receive: int | None = Field(1000000, alias="requiredMinReceive", description="The minimum interval in microseconds between received BFD control packets that this system should support.", title="Receive Interval", ge=10000, le=100000000)
    ttl: int | None = Field(None, description="Sets custom IP TTL or Hop Limit for multi-hop BFD sessions packets. Not applicable to single-hop BFD sessions.", title="IP TTL/Hop Limit", ge=2, le=255)


class IRBInterfaceSpec(_EDABase):
    anycast_gateway_mac: str | None = Field(None, alias="anycastGatewayMAC", description="The gateway MAC to use on the anycast address, if left empty the node will automatically assign one.", title="Anycast GW MAC")
    arp_timeout: int | None = Field(14400, alias="arpTimeout", description="Duration of time that dynamic ARP entries remain in the ARP cache before they expire.", title="ARP Timeout")
    bfd: IRBInterfaceBfdConfiguration | None = Field(None, description="Enable BFD on the IRBInterface.", title="BFD Configuration")
    bridge_domain: str = Field(..., alias="bridgeDomain", description="Reference to a BridgeDomain.", title="Bridge Domain")
    description: str | None = Field(None, description="The description of the IRBInterface.", title="Description")
    egress: IRBInterfaceEgressActions | None = Field(None, description="Manages actions on traffic at Egress.", title="Egress Actions")
    evpn_route_advertisement_type: IRBInterfaceEvpnRouteAdvertisementType | None = Field(None, alias="evpnRouteAdvertisementType", description="Controls the type of ARP/ND entries to advertise.", title="EVPN Route Advertisement Type")
    host_route_populate: IRBInterfaceHostRoutePopulation | None = Field(None, alias="hostRoutePopulate", description="Configures host route population based on ARP entries.", title="Host Route Population")
    ingress: IRBInterfaceIngressActions | None = Field(None, description="Manages actions on traffic at Ingress.", title="Ingress Actions")
    ip_addresses: list[IRBInterfaceIpAddresses] | None = Field(None, alias="ipAddresses", title="IP Addresses")
    ip_mtu: int | None = Field(1500, alias="ipMTU", description="IP MTU for the IRBInterface [default=1500].", title="IP MTU", ge=1280, le=9486)
    ipv4_parameters: IRBInterfaceIpv4SpecificParameters | None = Field(None, alias="ipv4Parameters", description="Manages IPv4-specific additional parameters that are not applicable to IPv6.", title="IPv4-specific Parameters")
    ipv6_router_advertisement: IRBInterfaceIpv6RouterAdvertisement | None = Field(None, alias="ipv6RouterAdvertisement", description="Manages IPV6 Router Advertisement parameters.", title="IPv6 Router Advertisement")
    l3_proxy_arpnd: IRBInterfaceL3ProxyArpNd | None = Field(None, alias="l3ProxyARPND", description="L3 Proxy ARP and ND configuration.", title="L3 Proxy ARP/ND")
    learn_unsolicited: Literal['BOTH', 'GLOBAL', 'LINK-LOCAL', 'NONE'] | None = Field('NONE', alias="learnUnsolicited", description="Enable or disable learning of unsolicited ARPs.", title="Learn Unsolicited ARPs")
    router: str = Field(..., description="Reference to a Router.", title="Router")
    unnumbered: Literal['IPV6'] | None = Field(None, description="Enables the use of unnumbered interfaces on the IRBInterface.  If IPv6 is specified, no IP address are configured on ...", title="Unnumbered")
    virtual_ip_discovery: list[IRBInterfaceVirtualIpDiscovery] | None = Field(None, alias="virtualIPDiscovery", description="Configuration for Virtual IP discovery on the interface.", title="Virtual IP Discovery")


class RouterRouteLeaking(_EDABase):
    export_policy: str | None = Field(None, alias="exportPolicy", description="Reference to a Policy resource to use when evaluating route exports from the DefaultRouter.", title="Export Policy")
    import_policy: str | None = Field(None, alias="importPolicy", description="Reference to a Policy resource to use when evaluating route imports into the DefaultRouter.", title="Import Policy")


class RouterPrefix(_EDABase):
    hash_buckets_per_path: int = Field(..., alias="hashBucketsPerPath", description="The number of times each next-hop is repeated in the fill pattern if there are max-paths ECMP next-hops.", title="Max Paths", ge=1, le=32)
    max_ecmp: int = Field(..., alias="maxECMP", description="The maximum number of ECMP next-hops per route associated with the resilient-hash prefix.", title="Max ECMP", ge=1, le=64)
    prefix: str = Field(..., description="IPv4 or IPv6 prefix. Active routes in the FIB that exactly match this prefix or that are longer matches of this prefi...", title="Prefix")


class RouterResilientHashing(_EDABase):
    prefix: list[RouterPrefix] | None = Field(None, description="IPv4 or IPv6 prefix. Active routes in the FIB that exactly match this prefix or that are longer matches of this prefi...", title="Prefix")


class RouterIpv6Unicast(_EDABase):
    enabled: bool = Field(..., description="Enables the IPv6 unicast AFISAFI", title="Enabled")
    multipath: RouterMultipath | None = Field(None, description="Enable multipath", title="Multipath")


class RouterMultipath(_EDABase):
    allow_multiple_as: bool = Field(..., alias="allowMultipleAS", description="When set to true, BGP is allowed to build a multipath set using BGP routes with different neighbor AS (most recent AS...", title="Allow Multiple Autonomous Systems Per Path")
    max_allowed_paths: int = Field(..., alias="maxAllowedPaths", description="The maximum number of BGP ECMP next-hops for BGP routes with an NLRI belonging to the address family of this configur...", title="Maximum Number of Paths", ge=1, le=256)


class RouterIpv4Unicast(_EDABase):
    advertise_ipv6_next_hops: bool | None = Field(None, alias="advertiseIPV6NextHops", description="Enables advertisement of IPv4 Unicast routes with IPv6 next-hops to peers.", title="Advertise IPv6 Next Hops")
    enabled: bool = Field(..., description="Enables the IPv4 unicast AFISAFI.", title="Enabled")
    multipath: RouterMultipath | None = Field(None, description="Enable multipath.", title="Multipath")
    receive_ipv6_next_hops: bool | None = Field(None, alias="receiveIPV6NextHops", description="Enables the advertisement of the RFC 5549 capability to receive IPv4 routes with IPv6 next-hops.", title="Receive IPv6 Next Hops")


class RouterIpAliasNexthops(_EDABase):
    esi: str | None = Field('auto', description="10 byte Ethernet Segment Identifier, if not set a type 0 ESI is generated.", title="ESI")
    next_hop: str = Field(..., alias="nextHop", description="The nexthop IP address to track for the IP alias.", title="IP Alias Address")
    preferred_active_node: str | None = Field(None, alias="preferredActiveNode", description="When not set the ES is used in an all active mode. This references the TopoNode object and when set, the DF algorithm...", title="Preferred Active Node")


class RouterBgpConfiguration(_EDABase):
    autonomous_system: int | None = Field(None, alias="autonomousSystem", description="Autonomous System number for BGP.", title="Autonomous System", ge=1, le=4294967295)
    ebgp_preference: int | None = Field(170, alias="ebgpPreference", description="Preference to be set for eBGP [default=170].", title="eBGP Preference", ge=1, le=255)
    enabled: bool | None = Field(False, description="Enable or disable BGP.", title="Enable BGP")
    export_policy: list[str] | None = Field(None, alias="exportPolicy", description="Reference to a Policy CR that will be used to filter routes advertised to peers.", title="Export Policy")
    ibgp_preference: int | None = Field(170, alias="ibgpPreference", description="Preference to be set for iBGP [default=170].", title="iBGP Preference", ge=1, le=255)
    import_policy: list[str] | None = Field(None, alias="importPolicy", description="Reference to a Policy CR that will be used to filter routes received from peers.", title="Import Policy")
    ip_alias_nexthops: list[RouterIpAliasNexthops] | None = Field(None, alias="ipAliasNexthops", description="IP aliasing configuration.", title="IP Alias Nexthops")
    ipv4_unicast: RouterIpv4Unicast | None = Field(None, alias="ipv4Unicast", description="Parameters relating to the IPv4 unicast AFI/SAFI.", title="IPv4 Unicast")
    ipv6_unicast: RouterIpv6Unicast | None = Field(None, alias="ipv6Unicast", description="Parameters relating to the IPv6 unicast AFI/SAFI.", title="IPv6 Unicast")
    keychain: str | None = Field(None, description="Keychain to be used for authentication", title="Keychain")
    min_wait_to_advertise: int | None = Field(0, alias="minWaitToAdvertise", description="Minimum wait time before advertising routes post BGP restart.", title="Min Wait To Advertise Time", ge=0, le=3600)
    rapid_withdrawl: bool | None = Field(True, alias="rapidWithdrawl", description="Enable rapid withdrawal in BGP.", title="Enable Rapid Withdrawal")
    wait_for_fib_install: bool | None = Field(False, alias="waitForFIBInstall", description="Wait for FIB installation before advertising routes.", title="Wait for FIB Installation")


class RouterSpec(_EDABase):
    bgp: RouterBgpConfiguration | None = Field(None, description="BGP configuration.", title="BGP Configuration")
    configured_name: str | None = Field(None, alias="configuredName", description="The name of the Router to configure on the device.", title="Configured Name")
    description: str | None = Field(None, description="The description of the Router.", title="Description")
    ecmp: int | None = Field(None, description="Set the maximum number of ECMP paths for the Router. This is supported only by some platforms, and will be ignored fo...", title="Maximum ECMP Paths", ge=1, le=256)
    evi: int | None = Field(None, description="EVI for the Router; leave blank for auto-allocation from EVI pool.", title="EVI", ge=1, le=65535)
    evi_pool: str | None = Field('evi-pool', alias="eviPool", description="Reference to EVI pool for auto-allocation.", title="EVI Allocation Pool")
    export_target: str | None = Field(None, alias="exportTarget", description="Export route target in 'target:N:N' format, if not specified, the default value taken as \"target:1:<evi>\".", title="Export Target", pattern=r"^target.*$")
    import_target: str | None = Field(None, alias="importTarget", description="Import route target in 'target:N:N' format, if not specified, the default value taken as \"target:1:<evi>\".", title="Import Target", pattern=r"^target.*$")
    ip_load_balancing: RouterResilientHashing | None = Field(None, alias="ipLoadBalancing", description="Resilient Hashing configuration.", title="Resilient Hashing")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Node selectors for deployment constraints.  If Nodes are selected, the Router will only be deployed on the Nodes sele...", title="Node Selector")
    route_leaking: RouterRouteLeaking | None = Field(None, alias="routeLeaking", description="Route leaking controlled by routing policies in and out of the DefaultRouter.", title="Route Leaking")
    router_id: str | None = Field(None, alias="routerID", description="Router ID.", title="Router ID")
    tunnel_index_pool: str | None = Field('tunnel-index-pool', alias="tunnelIndexPool", description="Reference to tunnel index allocation pool.", title="Tunnel Index Pool")
    type: Literal['SIMPLE', 'EVPNVXLAN'] | None = Field('EVPNVXLAN', description="Select the type of Router.  Simple doesn't include any overlay control plane or dataplane properties (EVPN/VXLAN). EV...", title="Type")
    vni: int | None = Field(None, description="VNI for the Router; leave blank for auto-allocation from VNI pool.", title="VNI", ge=1, le=16777215)
    vni_pool: str | None = Field('vni-pool', alias="vniPool", description="Reference to VNI pool for auto-allocation.", title="VNI Allocation Pool")


class BridgeDomainMacDuplicationDetection(_EDABase):
    action: Literal['Blackhole', 'OperDown', 'StopLearning'] | None = Field('StopLearning', description="Action to take on the subinterface upon detecting at least one mac addresses as duplicate on the subinterface.", title="Action")
    enabled: bool | None = Field(False, description="Enables or disables Mac Duplication Detection.", title="Enabled")
    hold_down_time: int | None = Field(9, alias="holdDownTime", description="Time to wait in minutes from the moment a mac is declared duplicate to the mac is flushed from the bridge table.", title="Hold Down Time", ge=2, le=60)
    monitoring_window: int | None = Field(3, alias="monitoringWindow", description="Monitoring window in minutes for detecting duplication on a given mac address.", title="Monitoring Window", ge=1, le=15)
    num_moves: int | None = Field(5, alias="numMoves", description="Number of moves a mac is allowed within the monitoring-window, before it is declared duplicate.", title="Number of Moves", ge=3)


class BridgeDomainL2ProxyArpNdIpDuplicationDetection(_EDABase):
    enabled: bool | None = Field(False, description="Enables or disables IP Duplication.", title="Enabled")
    hold_down_time: int | None = Field(9, alias="holdDownTime", description="Time to wait in minutes from the moment an IP is declared duplicate to the time the IP is removed from the proxy ARP/...", title="Hold Down Time", ge=2, le=60)
    monitoring_window: int | None = Field(3, alias="monitoringWindow", description="Monitoring window for detecting duplication on a given IP address in the proxy ARP/ND table.", title="Monitoring Window", ge=1, le=15)
    num_moves: int | None = Field(5, alias="numMoves", description="Number of moves in the proxy ARP/ND table that an IP is allowed within the monitoring-window.", title="Number of Moves", ge=3, le=10)


class BridgeDomainDynamicLearning(_EDABase):
    age_time: int | None = Field(None, alias="ageTime", description="Aging timer value for the proxy entries in seconds. If not set, this indicates that the entries are never flushed.", title="Age Time", ge=60, le=86400)
    enabled: bool | None = Field(False, description="Enables or disables Dynamic Learning.", title="Enabled")
    send_refresh: int | None = Field(None, alias="sendRefresh", description="The interval determines the frequency at which the system generates three ARP Requests or Neighbor Solicitations with...", title="Send Refresh Interval", ge=120, le=86400)


class BridgeDomainL2ProxyArpNd(_EDABase):
    dynamic_learning: BridgeDomainDynamicLearning | None = Field(None, alias="dynamicLearning", title="Dynamic Learning")
    ip_duplication: BridgeDomainL2ProxyArpNdIpDuplicationDetection | None = Field(None, alias="ipDuplication", title="L2 Proxy ARP/ND IP Duplication Detection")
    proxy_arp: bool | None = Field(False, alias="proxyARP", description="Enables proxy ARP.", title="Proxy ARP")
    proxy_nd: bool | None = Field(False, alias="proxyND", description="Enables proxy ND.", title="Proxy ND")
    table_size: int | None = Field(250, alias="tableSize", description="Maximum number of entries allowed in the proxy table of the bridge domain.", title="L2 Proxy ARP/ND Table Size", ge=1, le=8192)


class BridgeDomainSpec(_EDABase):
    configured_name: str | None = Field(None, alias="configuredName", description="The name of the BridgeDomain to configure on the device.", title="Configured Name")
    description: str | None = Field(None, description="The description of the BridgeDomain.", title="Description")
    evi: int | None = Field(None, description="EVI to use for this BridgeDomain, can be optionally left blank to have it automatically allocated using the EVI Pool.", title="EVI", ge=1, le=65535)
    evi_pool: str | None = Field('evi-pool', alias="eviPool", description="Reference to an EVI pool to use for allocations if EVI is left blank.", title="EVI Allocation Pool")
    export_target: str | None = Field(None, alias="exportTarget", description="Export route target in 'target:N:N' format, if not specified, the default value taken as \"target:1:<evi>\".", title="Export Target", pattern=r"^target.*$")
    import_target: str | None = Field(None, alias="importTarget", description="Import route target in 'target:N:N' format, if not specified, the default value taken as \"target:1:<evi>\".", title="Import Target", pattern=r"^target.*$")
    l2proxy_arpnd: BridgeDomainL2ProxyArpNd | None = Field(None, alias="l2proxyARPND", description="Enables / Disabled Proxy ARP / Proxy ND.", title="L2 Proxy ARP/ND")
    mac_aging: int | None = Field(300, alias="macAging", description="Configurable aging time for dynamically learned mac addresses.", title="MAC Aging", ge=60, le=86400)
    mac_duplication_detection: BridgeDomainMacDuplicationDetection | None = Field(None, alias="macDuplicationDetection", description="Enable or disable MAC duplication detection and resolution mechanisms.", title="MAC Duplication Detection")
    mac_learning: bool | None = Field(True, alias="macLearning", description="Enable MAC learning for this BridgeDomain.", title="MAC Learning")
    mac_limit: int | None = Field(None, alias="macLimit", description="Sets the maximum number of MAC entries accepted in the bridge table.", title="MAC Limit", ge=1)
    tunnel_index_pool: str | None = Field('tunnel-index-pool', alias="tunnelIndexPool", description="Reference to a tunnel index pool to use for allocations.", title="Tunnel Index Allocation Pool")
    type: Literal['SIMPLE', 'EVPNVXLAN'] | None = Field('EVPNVXLAN', description="Select the type of BridgeDomain.  Simple doesn't include any overlay control plane or dataplane properties (EVPN/VXLA...", title="Type")
    vni: int | None = Field(None, description="VNI to use for this BridgeDomain, can be optionally left blank to have it allocated using the VNI Pool.", title="VNI", ge=1, le=16777215)
    vni_pool: str | None = Field('vni-pool', alias="vniPool", description="Reference to a VNI pool to use for allocations if VNI is left blank.", title="VNI Allocation Pool")
