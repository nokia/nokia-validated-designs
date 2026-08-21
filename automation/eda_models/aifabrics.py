"""
Auto-generated Pydantic v2 models for EDA aifabrics API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class BackendStripes(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Optional reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  If left blank, AS...")
    gpu_vlan: int = Field(..., alias="gpuVlan", description="The VLAN used on interfaces facing the GPU servers.")
    name: str = Field(..., description="The name of the Stripe.")
    node_selector: list[str] = Field(..., alias="nodeSelector", description="Node selector to select the nodes to be used for this stripe.")
    stripe_id: int = Field(..., alias="stripeID", description="Unique ID for a stripe")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Optional reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces. If l...")


class BackendStripeConnector(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.")
    link_selector: list[str] = Field(..., alias="linkSelector", description="Selects TopoLinks to include in this AI Fabric, the selected TopoLinks will be used to create ISLs between the stripe...")
    name: str = Field(..., description="The name of the Stripe Connector.")
    node_selector: list[str] = Field(..., alias="nodeSelector", description="Node selector to select the nodes to be used for this stripe connector.")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces for the stripe...")


class BackendRocev2QoS(_EDABase):
    ecn_max_drop_probability_percent: int = Field(..., alias="ecnMaxDropProbabilityPercent", description="If the queue depth is between min and max threshold then this the probability with which packets are dropped or marked.")
    ecn_slope_max_threshold_percent: int = Field(..., alias="ecnSlopeMaxThresholdPercent", description="The maximum threshold parameter for a RED-managed queue in percent. When the average queue length exceeds the max val...")
    ecn_slope_min_threshold_percent: int = Field(..., alias="ecnSlopeMinThresholdPercent", description="The minimum threshold parameter for a RED-managed queue in percent. When the average queue length is less than min, a...")
    pfc_deadlock_detection_timer: int = Field(..., alias="pfcDeadlockDetectionTimer", description="Number of milliseconds during which outgoing interface is receiving pfc-pause-frames before triggering recovery-timer.")
    pfc_deadlock_recovery_timer: int = Field(..., alias="pfcDeadlockRecoveryTimer", description="Number of milliseconds during which the pfc-pause-frames will be ignored.")
    queue_maximum_burst_size: int = Field(..., alias="queueMaximumBurstSize", description="Maximum amount of shared buffer memory available to the queue in bytes.", ge=0, le=4294967295)


class BackendGpuIsolationGroups(_EDABase):
    interface_selector: list[str] = Field(..., alias="interfaceSelector")
    name: str = Field(..., description="Name of the IsolationGroup.")


class BackendSpec(_EDABase):
    asn_pool: str | None = Field(None, alias="asnPool", description="Reference to an IndexAllocationPool pool to use for Autonomous System Number allocations.  Used when eBGP is configur...")
    gpu_isolation_groups: list[BackendGpuIsolationGroups] = Field(..., alias="gpuIsolationGroups", description="GPU Isolation Groups are used to isolate GPU traffic over the network, GPUs in different GPU isolation groups will no...")
    ip_mtu: int | None = Field(4136, alias="ipMTU", description="IP MTU for this fabric. Default is 4136 bytes.", ge=1500, le=9000)
    rocev2_qo_s: BackendRocev2QoS = Field(..., alias="rocev2QoS", description="Set of properties to configure the RoCEv2 QoS.")
    stripe_connector: BackendStripeConnector | None = Field(None, alias="stripeConnector", description="StripeConnector is the spine layer interconnecting multiple stripes.")
    stripes: list[BackendStripes] = Field(..., description="A list of stripes, stripes contain a set of nodes (rails).")
    system_pool_ipv4: str | None = Field(None, alias="systemPoolIPV4", description="Reference to an IPAllocationPool used to dynamically allocate an IPv4 address to system/lo0 interfaces.  If specified...")
