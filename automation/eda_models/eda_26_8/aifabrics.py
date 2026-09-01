"""
Auto-generated Pydantic v2 models for EDA aifabrics API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class BackendBackendType(_EDABase):
    overlay: Literal['EVPN', 'RouteLeaking'] = Field(..., description="Overlay type for the AI Fabric. Route Leaking does not use any encapsulation of the overlay traffic. EVPN uses VXLAN ...", title="Overlay", min_length=1, max_length=253)


class BackendStripes(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Optional reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  If left blank, AS...", title="Autonomous System Pool", min_length=1, max_length=253)
    gpu_vlan: int | None = Field(None, alias="gpuVLAN", description="The VLAN used on interfaces facing the GPU servers.", title="GPU VLAN", ge=1, le=4094)
    name: str = Field(..., description="The name of the Stripe.", title="Stripe Name", min_length=1, max_length=253)
    node_selectors: list[str] = Field(..., alias="nodeSelectors", description="Node selector to select the nodes to be used for this stripe.", title="Node Selector")
    stripe_id: int = Field(..., alias="stripeID", description="Unique ID for a stripe", title="Stripe ID", ge=0, le=256)
    system_pool_i_pv4: str | None = Field(None, alias="systemPoolIPv4", description="Optional reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces. If l...", title="IPv4 Pool - System IP", min_length=1, max_length=253)


class BackendStripeConnector(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.", title="Autonomous System Pool", min_length=1, max_length=253)
    link_selectors: list[str] = Field(..., alias="linkSelectors", description="Selects TopoLinks to include in this AI Fabric, the selected TopoLinks will be used to create ISLs between the stripe...", title="Link Selector")
    name: str = Field(..., description="The name of the Stripe Connector.", title="Stripe Connector Name", min_length=1, max_length=253)
    node_selectors: list[str] = Field(..., alias="nodeSelectors", description="Node selector to select the nodes to be used for this stripe connector.", title="Node Selector")
    system_pool_i_pv4: str | None = Field(None, alias="systemPoolIPv4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces for the stripe...", title="IPv4 Pool - System IP", min_length=1, max_length=253)


class BackendRocev2Qos(_EDABase):
    ecn_max_drop_probability_percent: int | None = Field(100, alias="ecnMaxDropProbabilityPercent", description="If the queue depth is between min and max threshold then this the probability with which packets are dropped or marked.", title="ECN Max Drop Probability Percent")
    ecn_slope_max_threshold_percent: int | None = Field(80, alias="ecnSlopeMaxThresholdPercent", description="The maximum threshold parameter for a RED-managed queue in percent. When the average queue length exceeds the max val...", title="ECN Max Threshold Percent")
    ecn_slope_min_threshold_percent: int | None = Field(5, alias="ecnSlopeMinThresholdPercent", description="The minimum threshold parameter for a RED-managed queue in percent. When the average queue length is less than min, a...", title="ECN Min Threshold Percent")
    pfc_deadlock_detection_timer_ms: int | None = Field(750, alias="pfcDeadlockDetectionTimerMs", description="Number of milliseconds during which outgoing interface is receiving pfc-pause-frames before triggering recovery-timer.", title="PFC Deadlock Detection Timer", ge=100, le=1500)
    pfc_deadlock_recovery_timer_ms: int | None = Field(750, alias="pfcDeadlockRecoveryTimerMs", description="Number of milliseconds during which the pfc-pause-frames will be ignored.", title="PFC Deadlock Recovery Timer", ge=100, le=1500)
    queue_maximum_burst_size_bytes: int | None = Field(1024000, alias="queueMaximumBurstSizeBytes", description="Maximum amount of shared buffer memory available to the queue in bytes.", title="Maximum Burst Size", ge=0, le=268435456)


class BackendIpv6Pool(_EDABase):
    name: str = Field(..., description="Reference to an IPv6 allocation pool to use for prefix allocation.", title="IPv6 Pool Name")


class BackendGpuIsolationGroups(_EDABase):
    interface_selectors: list[str] = Field(..., alias="interfaceSelectors", title="Interface Selector")
    ipv6_pool: BackendIpv6Pool | None = Field(None, alias="ipv6Pool", description="IPv6 Pool reference for allocating IPv6 addresses to GPU facing interfaces in this IsolationGroup. Can be used only w...", title="IPv6 Pool")
    name: str = Field(..., description="Name of the IsolationGroup.", title="Isolation Group", min_length=1, max_length=253)


class BackendDynamicLoadBalancing(_EDABase):
    flowset_size: Literal[256, 512, 1024, 2048, 4096, 8192, 16384, 32768] | None = Field(256, alias="flowsetSize", description="The number of flowset entries reserved for each aggregate ECMP group.", title="Flowset Size", ge=256, le=32768)
    inactivity_timer_us: int | None = Field(50, alias="inactivityTimerUs", description="The flow inactivity timer in microseconds.", title="Inactivity Timer", ge=1, le=65535)
    mode: Literal['Dynamic', 'PerPacket'] | None = Field('Dynamic', description="The dynamic load balancing mode. Dynamic mode means that flows will be dynamically assigned to the available interfac...", title="Mode", min_length=1, max_length=253)
    sampling_interval_us: int | None = Field(5, alias="samplingIntervalUs", description="The sampling interval of interface state, in microseconds.", title="Sampling Interval", ge=1, le=255)


class BackendGlobalPoolAllocationProperties(_EDABase):
    name: str = Field(..., description="Reference to an IPv6 allocation pool to use for prefix allocation.", title="IPv6 Pool Name")


class BackendEdaManagedAllocationProperties(_EDABase):
    leaf_index_pool_scope: Literal['Global', 'Fabric', 'Stripe'] | None = Field('Global', alias="leafIndexPoolScope", description="Leaf Index Pool Allocation scope (used for IP Address allocation). Global scope means that the leaf index will be all...", title="Leaf Index Pool Scope", min_length=1, max_length=253)
    prefix_length: Literal['64', '96'] | None = Field('64', alias="prefixLength", description="IPv6 Prefix Length.", title="Prefix Length", min_length=1, max_length=3)


class BackendAddressAllocation(_EDABase):
    eda_managed_i_pv6: BackendEdaManagedAllocationProperties | None = Field(None, alias="edaManagedIPv6", description="EDA managed IPv6 allocation configuration.", title="EDA Managed Allocation Properties")
    global_i_pv6_pool: BackendGlobalPoolAllocationProperties | None = Field(None, alias="globalIPv6Pool", description="Global IPv6 pool allocation configuration.", title="Global Pool Allocation Properties")
    type: Literal['EDAManagedIPv6', 'GlobalIPv6Pool', 'PerTenantIPv6Pool'] | None = Field('EDAManagedIPv6', description="Type of address allocation strategy.", title="Type", min_length=1, max_length=253)


class BackendSpec(_EDABase):
    address_allocation: BackendAddressAllocation | None = Field({'edaManagedIPv6': {'leafIndexPoolScope': 'Global', 'prefixLength': '64'}, 'type': 'EDAManagedIPv6'}, alias="addressAllocation", description="Address allocation profile for GPU endpoints.", title="Address Allocation")
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...", title="Autonomous System Pool", min_length=1, max_length=253)
    dynamic_load_balancing: BackendDynamicLoadBalancing | None = Field(None, alias="dynamicLoadBalancing", description="Dynamic Load Balancing configuration for the Backend. This configuration enables dynamic load balancing for the entir...", title="Dynamic Load Balancing")
    gpu_isolation_groups: list[BackendGpuIsolationGroups] = Field(..., alias="gpuIsolationGroups", description="GPU Isolation Groups are used to isolate GPU traffic over the network, GPUs in different GPU isolation groups will no...", title="GPU Isolation Groups")
    ip_mtu: int | None = Field(4200, alias="ipMTU", description="IP MTU for this fabric.", title="IP MTU", ge=1500, le=9000)
    rocev2_qo_s: BackendRocev2Qos | None = Field({}, alias="rocev2QoS", description="Set of properties to configure the RoCEv2 QoS.", title="RoCEv2 QoS")
    stripe_connector: BackendStripeConnector | None = Field(None, alias="stripeConnector", description="StripeConnector is the spine layer interconnecting multiple stripes.", title="Stripe Connector")
    stripes: list[BackendStripes] = Field(..., description="A list of stripes, stripes contain a set of nodes (rails).", title="Stripes")
    system_pool_i_pv4: str | None = Field(None, alias="systemPoolIPv4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  If specified...", title="IPv4 Pool - System IP", min_length=1, max_length=253)
    type: BackendBackendType | None = Field(None, description="Type of the Backend to configure, can be used to select non-default types such as EVPN-VXLAN.", title="Backend Type")
