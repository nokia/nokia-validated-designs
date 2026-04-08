"""
Auto-generated Pydantic v2 models for EDA protocols API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class StaticRouteNexthops(_EDABase):
    bfd: StaticRouteBfd | None = Field(None, description="Enables BFD to the next-hops in the group. This overrides the configuration at the group.", title="BFD")
    ip_prefix: str = Field(..., alias="ipPrefix", description="Address to use.", title="IP Prefix")
    resolve: bool | None = Field(False, description="If set to true the next-hops can be destinations which are resolved in the route table. This overrides the configurat...", title="Resolve")


class StaticRouteBfd(_EDABase):
    enabled: bool | None = Field(False, description="Defines whether BFD should be enabled towards the nexthops.", title="Enabled")
    local_address: str | None = Field(None, alias="localAddress", description="Defines the local address to use when establishing the BFD session with the nexthop.", title="Local Address")


class StaticRouteNexthopGroup(_EDABase):
    bfd: StaticRouteBfd | None = Field(None, description="Enables BFD to the next-hops in the group. Local and Remote discriminator parameters have been deprecated at this lev...", title="BFD")
    blackhole: bool | None = Field(False, description="If set to true all traffic destined to the prefixes will be blackholed.  If enabled, next-hops are ignored and this t...", title="Blackhole")
    blackhole_send_icmp: bool | None = Field(None, alias="blackholeSendICMP", description="When enabled, the router will generate ICMP Unreachable messages for packets destined to the blackhole route.", title="Blackhole ICMP Generation")
    nexthops: list[StaticRouteNexthops] | None = Field(None, description="Ordered list of nexthops.", title="Nexthops")
    resolve: bool | None = Field(False, description="If set to true the next-hops can be destinations which are resolved in the route table.", title="Resolve")


class StaticRouteSpec(_EDABase):
    configured_name: str | None = Field(None, alias="configuredName", description="The name of the static route to configure on the device.", title="Configured Name")
    nexthop_group: StaticRouteNexthopGroup = Field(..., alias="nexthopGroup", description="Group of nexthops for the list of prefixes.", title="Nexthop Group")
    nodes: list[str] | None = Field(None, description="List of nodes on which to configure the static routes. An AND operation is executed against the nodes in this list an...", title="Nodes")
    preference: int | None = Field(None, description="Defines the route preference.", title="Preference")
    prefixes: list[str] = Field(..., description="List of destination prefixes and mask to use for the static routes.", title="Prefixes")
    router: str = Field(..., description="Reference to a Router on which to configure the static routes.  If no Nodes are provided then the static routes will ...", title="Router")
