"""
Auto-generated Pydantic v2 models for EDA core API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class IPAllocationPoolReservations(_EDABase):
    end: str = Field(..., description="Value to reserve to.", title="End")
    start: str = Field(..., description="Value to start reserving.", title="Start")


class IPAllocationPoolAllocations(_EDABase):
    name: str = Field(..., description="Name of this allocation.", title="Name")
    pool_instance: str | None = Field(None, alias="poolInstance", description="Pool instance, if empty applies to all instances.", title="Pool Instance")
    value: str = Field(..., description="Allocation to reserve.", title="Value")


class IPAllocationPoolSegments(_EDABase):
    allocate_broadcast_address: bool | None = Field(None, alias="allocateBroadcastAddress", description="Permit the allocation of the broadcast address.", title="Allocate Broadcast Address")
    allocate_network_address: bool | None = Field(None, alias="allocateNetworkAddress", description="Permit the allocation of the network address.", title="Allocate Network Address")
    allocations: list[IPAllocationPoolAllocations] | None = Field(None, description="List of reservations to exclude from allocations from this segment.", title="Allocations")
    reservations: list[IPAllocationPoolReservations] | None = Field(None, description="List of ranges to exclude from allocations from this segment.", title="Reservations")
    subnet: str = Field(..., description="IPv4 or IPv6 subnet, e.g. 10.1.1.0/24.", title="Subnet")


class IPAllocationPoolSpec(_EDABase):
    publish_allocations: bool | None = Field(None, alias="publishAllocations", description="If true, allocations in segments will be published to EDB, available to query via EQL and trigger state applications ...", title="Publish Allocations")
    segments: list[IPAllocationPoolSegments] = Field(..., description="List of segments containing IPv4 or IPv6 addresses to allocate.", title="Segments")


class IndexAllocationPoolReservations(_EDABase):
    end: int = Field(..., description="Value to reserve to.", title="End", ge=0, le=4294967295)
    start: int = Field(..., description="Value to start reserving.", title="Start", ge=0, le=4294967295)


class IndexAllocationPoolAllocations(_EDABase):
    name: str = Field(..., description="Name of this allocation.", title="Name")
    pool_instance: str | None = Field(None, alias="poolInstance", description="Pool instance, if empty applies to all instances.", title="Pool Instance")
    value: int = Field(..., description="Index to reserve.", title="Value", ge=0, le=4294967295)


class IndexAllocationPoolSegments(_EDABase):
    allocations: list[IndexAllocationPoolAllocations] | None = Field(None, description="List of reservations to exclude from allocations from this segment.", title="Allocations")
    reservations: list[IndexAllocationPoolReservations] | None = Field(None, description="Range of reservations to exclude from allocations from this segment.", title="Reservations")
    size: int = Field(..., description="Number of elements in the segment.", title="Size", ge=0, le=4294967295)
    start: int = Field(..., description="Starting value of the segment.", title="Start", ge=0, le=4294967295)


class IndexAllocationPoolSpec(_EDABase):
    publish_allocations: bool | None = Field(None, alias="publishAllocations", description="If true, allocations in segments will be published to EDB, available to query via EQL and trigger state applications ...", title="Publish Allocations")
    segments: list[IndexAllocationPoolSegments] = Field(..., description="List of segments containing indexes to allocate.", title="Segments")


class NodeUserGroupBindings(_EDABase):
    groups: list[str] = Field(..., description="Assigned groups for this user.", title="Groups")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Selector to use when selecting TopoNodes to deploy this user to.", title="Node Selector")
    nodes: list[str] | None = Field(None, description=" TopoNodes to deploy this user to.", title="Nodes")


class NodeUserSpec(_EDABase):
    group_bindings: list[NodeUserGroupBindings] = Field(..., alias="groupBindings", description="Matching of this user to node-specific permissions via groups.", title="Group Bindings")
    password: str = Field(..., description="Password for this user.", title="Password")
    ssh_public_keys: list[str] | None = Field(None, alias="sshPublicKeys", description="SSH public keys to deploy for the user.", title="SSH Public Keys")
    username: str | None = Field(None, description="Name of this user. If not provided, the name of the resource will be used.", title="Username", max_length=32)


class NodeProfileImages(_EDABase):
    image: str = Field(..., description="URL hosting the software image, e.g. srlimages/srlinux-24.7.1.bin.", title="Image")
    image_md5: str | None = Field(None, alias="imageMd5", description="URL hosting the software image md5 hash. e.g. srlimages/srlinux-24.7.1.bin.md5.", title="Image MD5")


class NodeProfileDhcpv6Options(_EDABase):
    option: Literal['59-BootfileUrl', '56-NTPServers'] = Field(..., description="DHCPv6 option to return to the TopoNode.", title="Option")
    value: list[str] = Field(..., description="Value to return to the TopoNode for the specified option.", title="Value")


class NodeProfileDhcpv4Options(_EDABase):
    option: Literal['1-SubnetMask', '2-TimeOffset', '3-Router', '4-TimeServer', '5-NameServer', '6-DomainNameServer', '7-LogServer', '8-QuoteServer', '9-LPRServer', '10-ImpressServer', '11-ResourceLocationServer', '12-HostName', '13-BootFileSize', '14-MeritDumpFile', '15-DomainName', '16-SwapServer', '17-RootPath', '18-ExtensionsPath', '19-IPForwarding', '20-NonLocalSourceRouting', '21-PolicyFilter', '22-MaximumDatagramAssemblySize', '23-DefaultIPTTL', '24-PathMTUAgingTimeout', '25-PathMTUPlateauTable', '26-InterfaceMTU', '27-AllSubnetsAreLocal', '28-BroadcastAddress', '29-PerformMaskDiscovery', '30-MaskSupplier', '31-PerformRouterDiscovery', '32-RouterSolicitationAddress', '33-StaticRoutingTable', '34-TrailerEncapsulation', '35-ArpCacheTimeout', '36-EthernetEncapsulation', '37-DefaulTCPTTL', '38-TCPKeepaliveInterval', '39-TCPKeepaliveGarbage', '40-NetworkInformationServiceDomain', '41-NetworkInformationServers', '42-NTPServers', '43-VendorSpecificInformation', '44-NetBIOSOverTCPIPNameServer', '45-NetBIOSOverTCPIPDatagramDistributionServer', '46-NetBIOSOverTCPIPNodeType', '47-NetBIOSOverTCPIPScope', '48-XWindowSystemFontServer', '49-XWindowSystemDisplayManager', '50-RequestedIPAddress', '51-IPAddressLeaseTime', '52-OptionOverload', '53-DHCPMessageType', '54-ServerIdentifier', '55-ParameterRequestList', '56-Message', '57-MaximumDHCPMessageSize', '58-RenewTimeValue', '59-RebindingTimeValue', '60-ClassIdentifier', '61-ClientIdentifier', '62-NetWareIPDomainName', '63-NetWareIPInformation', '64-NetworkInformationServicePlusDomain', '65-NetworkInformationServicePlusServers', '66-TFTPServerName', '67-BootfileName', '68-MobileIPHomeAgent', '69-SimpleMailTransportProtocolServer', '70-PostOfficeProtocolServer', '71-NetworkNewsTransportProtocolServer', '72-DefaultWorldWideWebServer', '73-DefaultFingerServer', '74-DefaultInternetRelayChatServer', '75-StreetTalkServer', '76-StreetTalkDirectoryAssistanceServer', '77-UserClassInformation', '78-SLPDirectoryAgent', '79-SLPServiceScope', '80-RapidCommit', '81-FQDN', '82-RelayAgentInformation', '83-InternetStorageNameService', '85-NDSServers', '86-NDSTreeName', '87-NDSContext', '88-BCMCSControllerDomainNameList', '89-BCMCSControllerIPv4AddressList', '90-Authentication', '91-ClientLastTransactionTime', '92-AssociatedIP', '93-ClientSystemArchitectureType', '94-ClientNetworkInterfaceIdentifier', '95-LDAP', '97-ClientMachineIdentifier', '98-OpenGroupUserAuthentication', '99-GeoConfCivic', '100-IEEE10031TZString', '101-ReferenceToTZDatabase', '112-NetInfoParentServerAddress', '113-NetInfoParentServerTag', '114-URL', '116-AutoConfigure', '117-NameServiceSearch', '118-SubnetSelection', '119-DNSDomainSearchList', '120-SIPServers', '121-ClasslessStaticRoute', '122-CCC', '123-GeoConf', '124-VendorIdentifyingVendorClass', '125-VendorIdentifyingVendorSpecific', '128-TFTPServerIPAddress', '129-CallServerIPAddress', '130-DiscriminationString', '131-RemoteStatisticsServerIPAddress', '132-8021PVLANID', '133-8021QL2Priority', '134-DiffservCodePoint', '135-HTTPProxyForPhoneSpecificApplications', '136-PANAAuthenticationAgent', '137-LoSTServer', '138-CAPWAPAccessControllerAddresses', '139-OPTIONIPv4AddressMoS', '140-OPTIONIPv4FQDNMoS', '141-SIPUAConfigurationServiceDomains', '142-OPTIONIPv4AddressANDSF', '143-OPTIONIPv6AddressANDSF', '150-TFTPServerAddress', '151-StatusCode', '152-BaseTime', '153-StartTimeOfState', '154-QueryStartTime', '155-QueryEndTime', '156-DHCPState', '157-DataSource', '175-Etherboot', '176-IPTelephone', '177-EtherbootPacketCableAndCableHome', '208-PXELinuxMagicString', '209-PXELinuxConfigFile', '210-PXELinuxPathPrefix', '211-PXELinuxRebootTime', '212-OPTION6RD', '213-OPTIONv4AccessDomain', '220-SubnetAllocation', '221-VirtualSubnetAllocation', '224-Reserved', '225-Reserved', '226-Reserved', '227-Reserved', '228-Reserved', '229-Reserved', '230-Reserved', '231-Reserved', '232-Reserved', '233-Reserved', '234-Reserved', '235-Reserved', '236-Reserved', '237-Reserved', '238-Reserved', '239-Reserved', '240-Reserved', '241-Reserved', '242-Reserved', '243-Reserved', '244-Reserved', '245-Reserved', '246-Reserved', '247-Reserved', '248-Reserved', '249-Reserved', '250-Reserved', '251-Reserved', '252-Reserved', '253-Reserved', '254-Reserved', '255-End'] = Field(..., description="DHCPv4 option to return to the TopoNode.", title="Option")
    value: list[str] = Field(..., description="Value to return to the TopoNode for the specified option.", title="Value")


class NodeProfileDhcp(_EDABase):
    dhcp4_options: list[NodeProfileDhcpv4Options] | None = Field(None, alias="dhcp4Options", description="DHCPv4 options to return to TopoNodes referencing this NodeProfile.", title="DHCPv4 Options")
    dhcp6_options: list[NodeProfileDhcpv6Options] | None = Field(None, alias="dhcp6Options", description="DHCPv6 options to return to TopoNodes referencing this NodeProfile.", title="DHCPv6 Options")
    management_poolv4: str | None = Field(None, alias="managementPoolv4", description="IPInSubnetAllocationPool to use for IPv4 allocations of the management address for TopoNodes referencing this NodePro...", title="Management Pool - IPv4")
    management_poolv6: str | None = Field(None, alias="managementPoolv6", description="IPInSubnetAllocationPool to use for IPv6 allocations of the management address for TopoNodes referencing this NodePro...", title="Management Pool - IPv6")
    preferred_address_family: Literal['IPv4', 'IPv6'] | None = Field(None, alias="preferredAddressFamily", description="Preferred IP address family", title="Preferred Address Family")


class NodeProfileSpec(_EDABase):
    annotate: bool | None = Field(False, description="Indicates if NPP should annotate sent configuration.", title="Annotations")
    container_image: str | None = Field(None, alias="containerImage", description="Container image to use when simulating TopoNodes referencing this NodeProfile, e.g. ghcr.io/nokia/srlinux:24.7.1.", title="Container Image")
    dhcp: NodeProfileDhcp | None = Field(None, description="DHCP options to use when onboarding the TopoNode. Optional if not bootstrapping using EDA.", title="DHCP")
    image_pull_secret: str | None = Field(None, alias="imagePullSecret", description="Secret used to authenticate to the container registry where the container image is hosted.", title="Image Pull Secret")
    images: list[NodeProfileImages] | None = Field(None, description="URLs hosting software images for bootstrapping TopoNodes referencing this NodeProfile.", title="Images")
    license: str | None = Field(None, description="ConfigMap containing a license for TopoNodes referencing this NodeProfile.", title="License")
    llm_db: str | None = Field(None, alias="llmDb", description="URL containing LLDB  to use when interacting with LLM-DB and OpenAI for query autocompletion, e.g. http://eda-asvr/ll...", title="LLMDB")
    node_user: str = Field(..., alias="nodeUser", description="Reference to a NodeUser to use for authentication to TopoNodes referencing this NodeProfile.", title="Node User")
    onboarding_password: str = Field(..., alias="onboardingPassword", description="The password to use when onboarding TopoNodes referencing this NodeProfile, e.g. admin.", title="Onboarding Password")
    onboarding_username: str = Field(..., alias="onboardingUsername", description="The username to use when onboarding TopoNodes referencing this NodeProfile, e.g. admin.", title="Onboarding Username")
    operating_system: Literal['srl', 'sros', 'eos', 'sonic', 'ios-xr', 'nxos'] = Field(..., alias="operatingSystem", description="Sets the operating system of this NodeProfile, e.g. srl.", title="Operating System")
    platform_path: str | None = Field(None, alias="platformPath", description="JSPath to use for retrieving the version string from TopoNodes referencing this NodeProfile, e.g. .platform.chassis.t...", title="Platform Path")
    port: int | None = Field(57400, description="Port used to establish a connection to the TopoNode, e.g. 57400.", title="Port", ge=1, le=65535)
    serial_number_path: str | None = Field(None, alias="serialNumberPath", description="JSPath to use for retrieving the serial number string from TopoNodes referencing this NodeProfile, e.g. .platform.cha...", title="Serial Number Path")
    version: str = Field(..., description="Sets the software version of this NodeProfile, e.g. 24.7.1 (for srl), or 24.7.r1 (for sros).", title="Version")
    version_match: str | None = Field(None, alias="versionMatch", description="Regular expression to match the node-retrieved version string to TopoNode version, e.g. v0\\.0\\.0.*.", title="Version Match")
    version_path: str | None = Field(None, alias="versionPath", description="JSPath to use for retrieving the version string from TopoNodes referencing this NodeProfile, e.g. .system.information...", title="Version Path")
    yang: str = Field(..., description="URL containing YANG modules and schema profile to use when interacting with TopoNodes referencing this NodeProfile, e...", title="YANG")


class TopoLinkB(_EDABase):
    interface: str | None = Field(None, description="Normalized name of the interface/port, e.g. ethernet-1-1.", title="Interface")
    interface_resource: str = Field(..., alias="interfaceResource", description="Reference to a Interface.", title="Interface Resource")
    node: str = Field(..., description="Reference to a TopoNode.", title="Node")


class TopoLinkA(_EDABase):
    interface: str | None = Field(None, description="Normalized name of the interface/port, e.g. ethernet-1-1.", title="Interface")
    interface_resource: str = Field(..., alias="interfaceResource", description="Reference to a Interface.", title="Interface Resource")
    node: str = Field(..., description="Reference to a TopoNode.", title="Node")


class TopoLinkLinks(_EDABase):
    local: TopoLinkA = Field(..., description="Local, or \"A\" endpoint of the link.", title="A")
    remote: TopoLinkB | None = Field(None, description="Remote, or \"B\" endpoint of the link.", title="B")
    speed: Literal['800G', '400G', '200G', '100G', '50G', '40G', '25G', '10G', '2.5G', '1G', '100M'] | None = Field(None, description="Speed of the link.", title="Speed")
    type: Literal['edge', 'interSwitch', 'loopback'] = Field(..., description="Specify the type of link. If type is set to edge, topology information for the remote device can be set; when doing s...", title="Type")


class TopoLinkSpec(_EDABase):
    links: list[TopoLinkLinks] = Field(..., description="Define the set of physical links making up this TopoLink.", title="Links")


class TopoNodeUplinkInterfaces(_EDABase):
    host_port: str = Field(..., alias="hostPort", description="HostPort interface of the satellite uplink.", title="Host Port")
    satellite: str = Field(..., description="Satellite interface of the satellite uplink.", title="Satellite")


class TopoNodeUplinks(_EDABase):
    downlinks: list[str] | None = Field(None, description="Downlinks for the SatelliteUplink.", title="Downlinks")
    name: str = Field(..., description="The name of the SatelliteUplink.", title="Name")


class TopoNodeConnectors(_EDABase):
    kind: Literal['controlCard', 'lineCard', 'fabric', 'mda', 'connector', 'xiom', 'powerShelf', 'powerModule'] = Field(..., description="The kind of Component, e.g. lineCard.", title="Kind")
    slot: str | None = Field(None, description="The slot this Component resides in, unset for Components that do not have a slot or ID. e.g. 1 would denote the linec...", title="Slot")
    type: str = Field(..., description="Denotes the type of hardware being provisioned, e.g. xcm-x20.", title="Type")


class TopoNodePortTemplate(_EDABase):
    connectors: list[TopoNodeConnectors] | None = Field(None, description="List of connector components within the SatellitePortTemplate. Used to define the type and location of connectors.", title="Connectors")
    name: str = Field(..., description="The name of the SatellitePortTemplate.", title="Name")
    uplinks: list[TopoNodeUplinks] | None = Field(None, description="Uplinks for the SatellitePortTemplate.", title="Uplinks")


class TopoNodeSatelliteNodes(_EDABase):
    components: list[TopoNodeComponents] | None = Field(None, description="Components for the satellite node.", title="Components")
    id: str = Field(..., description="ID of the satellite node.", title="ID")
    license: str | None = Field(None, description="ConfigMap containing a license for this satellite node.", title="License")
    mac_address: str | None = Field(None, alias="macAddress", description=" MAC Address of the satellite node.", title="MAC Address")
    operating_system: Literal['srl', 'sros', 'eos', 'sonic', 'ios-xr', 'nxos'] | None = Field(None, alias="operatingSystem", description="Operating system for this satellite node.", title="Operating System")
    platform: str | None = Field(None, description="Platform of the satellite node.", title="Platform")
    port_template: TopoNodePortTemplate | None = Field(None, alias="portTemplate", description="Port template to be used for the satellite node.", title="Port Template")
    satellite_profile: str | None = Field(None, alias="satelliteProfile", description="Satellite node profile to be used for the satellite node.", title="Satellite Profile")
    type: str = Field(..., description="Type of the satellite node.", title="Type")
    uplink_interfaces: list[TopoNodeUplinkInterfaces] | None = Field(None, alias="uplinkInterfaces", description="Uplink interfaces to be created for the satellite node.", title="Uplink Interfaces")
    version: str | None = Field(None, description="Software version for this satellite node.", title="Version")


class TopoNodeProductionAddress(_EDABase):
    ipv4: str | None = Field(None, description="The IPv4 production address", title="IPv4")
    ipv6: str | None = Field(None, description="The IPv6 production address", title="IPv6")


class TopoNodeNpp(_EDABase):
    mode: Literal['normal', 'maintenance', 'null', 'emulate', 'monitor'] | None = Field('normal', description="The mode in which this TopoNode is functioning. \"normal\" (the default)    indicates that NPP is expecting an endpoi...", title="Mode")


class TopoNodeComponents(_EDABase):
    kind: Literal['controlCard', 'lineCard', 'fabric', 'mda', 'connector', 'xiom', 'powerShelf', 'powerModule'] = Field(..., description="The kind of Component, e.g. lineCard.", title="Kind")
    slot: str | None = Field(None, description="The slot this Component resides in, unset for Components that do not have a slot or ID. e.g. 1 would denote the linec...", title="Slot")
    type: str = Field(..., description="Denotes the type of hardware being provisioned, e.g. xcm-x20.", title="Type")


class TopoNodeSpec(_EDABase):
    component: list[TopoNodeComponents] | None = Field(None, description="List of components within the TopoNode. Used to define the type and location of linecards, fabrics (SFM), media adapt...", title="Components")
    license: str | None = Field(None, description="Reference to a ConfigMap containing a license for the TopoNode. Overrides the license set in the referenced NodeProfi...", title="License")
    mac_address: str | None = Field(None, alias="macAddress", description="MAC address to associate with this TopoNode. Typically the chassis MAC address, optionally sent by a node in DHCP req...", title="MAC Address")
    node_profile: str = Field(..., alias="nodeProfile", description="Reference to a NodeProfile to use with this TopoNode.", title="Node Profile")
    npp: TopoNodeNpp | None = Field(None, description="Options relating to NPP interactions with the node.", title="NPP")
    on_boarded: bool | None = Field(False, alias="onBoarded", description="Indicates if this TopoNode has been bootstrapped or is reachable via configured credentials. Set by BootstrapServer w...", title="Onboarded")
    operating_system: Literal['srl', 'sros', 'eos', 'sonic', 'ios-xr', 'nxos'] = Field(..., alias="operatingSystem", description="Operating system running on this TopoNode, e.g. srl.", title="Operating System")
    platform: str = Field(..., description="Platform type of this TopoNode, e.g. 7220 IXR-D3L.", title="Platform")
    production_address: TopoNodeProductionAddress | None = Field(None, alias="productionAddress", description="Production address of this TopoNode - this is the address the real, production instance of this TopoNode uses. If lef...", title="Production Address")
    satellite_nodes: list[TopoNodeSatelliteNodes] | None = Field(None, alias="satelliteNodes", description="List of satellite nodes associated with this TopoNode. Used to define the type and configuration of satellite nodes.", title="Satellite Nodes")
    serial_number: str | None = Field(None, alias="serialNumber", description="Serial number of this TopoNode, optionally sent by a node in DHCP requests. Not required when a TopoNode is not being...", title="Serial Number")
    system_interface: str | None = Field(None, alias="systemInterface", description="Deprecated: Name of the Interface resource representing the primary loopback on the TopoNode, this field will be remo...", title="System Interface")
    version: str = Field(..., description="Software version of this TopoNode, e.g. 24.7.1 (for srl), or 24.7.r1 (for sros).", title="Version")
