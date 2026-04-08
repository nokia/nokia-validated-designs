"""
Auto-generated Pydantic v2 models for EDA bootstrap API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class InitStaticRoutes(_EDABase):
    next_hop: str | None = Field(None, alias="nextHop", description="Static route next hop.", title="Next Hop")
    prefix: str | None = Field(None, description="Static route prefix.", title="Prefix")


class InitMgmt(_EDABase):
    ip_mtu: int | None = Field(None, alias="ipMTU", description="Set the management interface IP MTU.", title="IP MTU")
    ipv4_dhcp: bool | None = Field(None, alias="ipv4DHCP", description="Enable IPv4 DHCP client.", title="IPv4 DHCP Client")
    ipv6_dhcp: bool | None = Field(None, alias="ipv6DHCP", description="Enable IPv6 DHCP client.", title="IPv6 DHCP Client")
    static_routes: list[InitStaticRoutes] | None = Field(None, alias="staticRoutes", description="Optional list of static routes to add to the management network instance as part of the initial configuration.", title="Static Routes")


class InitSpec(_EDABase):
    commit_save: bool | None = Field(None, alias="commitSave", description="Save a startup configuration after each commit.", title="Commit Save")
    mgmt: InitMgmt | None = Field(None, description="Optional management interface settings. Allows setting DHCP clients or static IPs as well as the IP MTU.", title="Mgmt")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Optional node selectors to perform initial configuration for. If not provided initialization is performed for all nodes.", title="Node Selector")
