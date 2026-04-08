"""
Auto-generated Pydantic v2 models for EDA interfaces API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class InterfaceMembers(_EDABase):
    aggregate_id: str | None = Field(None, alias="aggregateId", description="When using a LAG, the aggregateId can be specified per set of interfaces on a node. LAG interface with which this int...", title="Aggregate ID")
    description: str | None = Field(None, description="Description of the member, inherited from the interface if not provided.", title="Description")
    enabled: bool | None = Field(True, description="Enable or disable this member.", title="Enabled")
    interface: str = Field(..., description="Reference to an interface in the normalized format. Ex: SRL ethernet-1/1 would be ethernet-1-1. SROS port 2/1/1 would...", title="Interface")
    lacp_port_priority: int | None = Field(32768, alias="lacpPortPriority", description="Configure the port priority for LACP. This value is used to determine which port should be activated with LACP fallba...", title="LACP Port Priority", ge=0, le=65535)
    node: str = Field(..., description="Node name.", title="Node Name")


class InterfaceMultiHoming(_EDABase):
    esi: str | None = Field('auto', description="10 byte Ethernet Segment Identifier, if not set a type 0 ESI is generated. [default=auto]", title="ESI")
    mode: Literal['all-active', 'single-active', 'port-active'] | None = Field('all-active', description="\"all-active\": All interfaces are active. \"single-active\": In a single active MH LAG, the active and standby funct...", title="Mode")
    preferred_active_node: str | None = Field(None, alias="preferredActiveNode", description="To be used in single-active or port-active modes.  This references the Node object and when set, the DF algorithm is ...", title="Preferred Active Node")
    reload_delay_timer: int | None = Field(100, alias="reloadDelayTimer", description="After the system boots, the reload-delay timer in seconds keeps an interface shut down with the laser off for a confi...", title="Reload Delay Timer", ge=1, le=86400)
    revertive: bool | None = Field(False, description="To be used in single-active or port-active modes.  When true, if there is a switch of active interface in the LAG and...", title="Revertive")


class InterfaceFallback(_EDABase):
    mode: Literal['static'] | None = Field('static', description="Specifies lacp-fallback mode if enabled.", title="Mode")
    timeout: int | None = Field(60, description="Specifies the LACP-fallback timeout interval in seconds. [default=60]", title="Timeout", ge=4, le=3600)


class InterfaceLacp(_EDABase):
    admin_key: int | None = Field(None, alias="adminKey", description="Configure the LACP admin-key to be advertised by the local system.", title="Admin Key", ge=1, le=65535)
    interval: Literal['fast', 'slow'] | None = Field('fast', description="Set the period between LACP messages, uses the lacp-period-type enumeration. [default=\"fast\"]", title="Interval")
    lacp_fallback: InterfaceFallback | None = Field(None, alias="lacpFallback", description="LACP fallback allows one or more designated links of an LACP controlled LAG to go into forwarding mode if LACP is not...", title="Fallback")
    mode: Literal['active', 'passive'] | None = Field('active', description="Active is to initiate the transmission of LACP PDUs. Passive is to wait for peer to initiate the transmission of LACP...", title="Mode")
    system_id_mac: str | None = Field(None, alias="systemIdMac", description="The MAC address portion of the Node's System ID. This is combined with the system priority to construct the 8-octet s...", title="System ID MAC")
    system_priority: int | None = Field(32768, alias="systemPriority", description="System priority used by the Node on this LAG interface. Lower value is higher priority for determining which Node is ...", title="System Priority", ge=0, le=65535)


class InterfaceLag(_EDABase):
    lacp: InterfaceLacp | None = Field(None, title="LACP")
    min_links: int | None = Field(1, alias="minLinks", description="The min-link threshold specifies the minimum number of member links that must be active in order for the LAG to be op...", title="Minimum Links", ge=1, le=64)
    multihoming: InterfaceMultiHoming | None = Field(None, title="Multi Homing")
    type: Literal['lacp', 'static'] | None = Field('lacp', description="This type defines whether whether it is a static or LACP LAG. [default=lacp]", title="Type")


class InterfaceStormControl(_EDABase):
    broadcast_rate: int | None = Field(None, alias="broadcastRate", description="Sets the maximum rate allowed for ingress broadcast frames on the interface.", title="Broadcast Rate", ge=0, le=100000000)
    enabled: bool | None = Field(None, description="Enables storm control.", title="Enabled")
    multicast_rate: int | None = Field(None, alias="multicastRate", description="Sets the maximum rate allowed for ingress multicast frames on the interface.", title="Multicast Rate", ge=0, le=100000000)
    units: Literal['kbps', 'percentage'] | None = Field(None, description="Set the units to be used for measurement.", title="Units")
    unknown_unicast_rate: int | None = Field(None, alias="unknownUnicastRate", description="Sets the maximum rate allowed for ingress unknown unicast frames on the interface.", title="Unknown Unicast Rate", ge=0, le=100000000)


class InterfaceConfig(_EDABase):
    exponent: int = Field(..., description="Threshold exponent for the signal degrade condition.", title="Threshold Exponent", ge=1, le=9)
    multiplier: int = Field(..., description="Threshold multiplier for the signal degrade condition.", title="Threshold Multiplier", ge=1, le=9)


class InterfaceCrcMonitor(_EDABase):
    enabled: bool | None = Field(None, description="Enables CRC monitoring on the interface.", title="Enabled")
    signal_degrade: InterfaceConfig | None = Field(None, alias="signalDegrade", description="Signal degrade threshold configuration. eda:ui:title=\"Signal Degrade\"")
    signal_failure: InterfaceConfig | None = Field(None, alias="signalFailure", description="Signal failure threshold configuration. eda:ui:title=\"Signal Failure\"")
    window_size_sec: int | None = Field(None, alias="windowSizeSec", description="Sliding window size over which CRC errors are measured, in number of seconds.", title="Window Size", ge=1)


class InterfaceEthernet(_EDABase):
    crc_monitor: InterfaceCrcMonitor | None = Field(None, alias="crcMonitor", description="Configuration of CRC monitoring on the interface.", title="CRC Monitor")
    fec: Literal['disabled', 'rs528', 'rs544', 'baser', 'rs108'] | None = Field(None, description="Sets the Forward Error Correction (FEC) on the members of the interface.", title="Forward Error Correction")
    hold_down_timer: int | None = Field(None, alias="holdDownTimer", description="The hold-time down behavior is triggered with events that try to bring the ethernet interface down and can change qui...", title="Hold Down Timer", ge=100, le=86400000)
    hold_up_timer: int | None = Field(None, alias="holdUpTimer", description="The hold-time up behavior is triggered with any event that tries to bring up the ethernet interface.  While the hold-...", title="Hold Up Timer", ge=100, le=86400000)
    loopback_mode: Literal['none', 'facility', 'terminal'] | None = Field(None, alias="loopbackMode", description="Enable dataplane loopback on the interface.", title="Loopback Mode")
    reload_delay_timer: int | None = Field(None, alias="reloadDelayTimer", description="After the system boots, the reload-delay timer in seconds keeps an interface shut down with the laser off for a confi...", title="Reload Delay Timer", ge=1, le=86400)
    speed: Literal['100G', '10G', '1G', '25G', '40G', '50G', '400G'] | None = Field(None, description="The speed of this interface, in human-readable format - e.g. 25G, 100G.", title="Speed")
    standby_signaling: Literal['lacp', 'power-off'] | None = Field(None, alias="standbySignaling", description="Indicates the standby-signaling used in the interface.", title="Standby Signaling")
    storm_control: InterfaceStormControl | None = Field(None, alias="stormControl", description="Enables storm control.", title="Storm Control")
    transparent_l2_cp_protocols: list[str] | None = Field(None, alias="transparentL2CPProtocols", description="A list of L2CP protocols to tunnel. Options: LLDP, LACP, xSTP, Dot1x, PTP, All.", title="Transparent L2CP Protocols")


class InterfaceSpec(_EDABase):
    ddm: bool | None = Field(None, description="Enables reporting of DDM events.", title="DDM")
    description: str | None = Field(None, description="Description of the interface.", title="Description")
    enabled: bool | None = Field(True, description="Enable or disable the interface.", title="Enabled")
    encap_type: Literal['null', 'dot1q'] | None = Field('null', alias="encapType", description="Enable or disable VLAN tagging on this interface. [default=\"null\"]", title="Encapsulation Type")
    ethernet: InterfaceEthernet | None = Field(None, description="Ethernet configuration options.", title="Ethernet")
    lag: InterfaceLag | None = Field(None, description="LAG configuration options.", title="LAG")
    lldp: bool | None = Field(True, description="Enable or disable LLDP on the members of the interface.", title="Link Layer Discovery Protocol")
    members: list[InterfaceMembers] = Field(..., description="List of members on which to apply properties, for single interface this would be a list of 1.", title="Members")
    mtu: int | None = Field(None, description="MTU to apply on the interface(s).", title="MTU", ge=1450, le=9500)
    type: Literal['lag', 'interface', 'loopback'] | None = Field('interface', description="Type defines whether the interface is a Lag or Interface.", title="Type")
