"""
Auto-generated Pydantic v2 models for EDA siteinfo API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class DefaultMTUSpec(_EDABase):
    interface_mtu: int | None = Field(None, alias="interfaceMTU", description="Configures the Default MTU value for Ethernet interfaces. Includes Ethernet headers but excludes 4-byte FCS trailer.", title="Interface MTU", ge=1500, le=11000)
    layer2_subif_mtu: int | None = Field(None, alias="layer2SubifMTU", description="Configures the Default MTU value for Layer 2 (bridged) interfaces. Includes Ethernet headers but excludes 4-byte FCS trailer.", title="Layer 2 Sub-interface MTU", ge=1500, le=11000)
    layer3_mtu: int | None = Field(None, alias="layer3MTU", description="Configures the Default IP MTU value for Layer 3 interfaces. Includes IP headers but excludes Ethernet headers.", title="IP MTU (Layer 3 MTU)", ge=1280, le=11000)
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to select nodes on which to configure the defaults.", title="Node Selector")
    nodes: list[str] | None = Field(None, description="List of nodes on which to configure the defaults.", title="Nodes")

class BannerSpec(_EDABase):
    login_banner: str | None = Field(None, alias="loginBanner", description="Login banner displayed before a user has logged into the Node.", title="Login Banner")
    motd: str | None = Field(None, description="MOTD banner displayed after a user has logged into the Node.", title="MOTD")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to select nodes on which to configure the banners.", title="Node Selector")
    nodes: list[str] | None = Field(None, description="List of nodes on which to configure the banners.", title="Nodes")

