"""
Auto-generated Pydantic v2 models for EDA topologies API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class TopologyGroupingTierSelectors(_EDABase):
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to use to match nodes that should be assigned to this tier.", title="Node Selector")
    tier: int = Field(..., description="The tier to assign to nodes that match the selector.", title="Tier")


class TopologyGroupingGroupSelectors(_EDABase):
    group: str = Field(..., description="The group to assign to nodes that match the selector.  Primarily this is used as a unique key to identify which nodes...", title="Group")
    group_ui_name: str = Field(..., alias="groupUIName", description="The name of the group to show in the UI.  If not set, then the UI will display the group string above.", title="UI Name")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to use to match nodes that should be assigned to this group.", title="Node Selector")


class TopologyGroupingSpec(_EDABase):
    group_selectors: list[TopologyGroupingGroupSelectors] | None = Field(None, alias="groupSelectors", description="The set of selectors for assigning nodes to groups", title="Group Selectors")
    tier_selectors: list[TopologyGroupingTierSelectors] | None = Field(None, alias="tierSelectors", description="The set of selectors for assigning nodes to tiers", title="Tier Selectors")
    ui_description: str | None = Field(None, alias="uiDescription", description="A description of the topology grouping to expose in the UI", title="UI Description")
    ui_description_key: str | None = Field(None, alias="uiDescriptionKey", description="The translation key for the description of the topology grouping to expose in the UI", title="UI Description Key")
    ui_name: str | None = Field(None, alias="uiName", description="The name of the topology grouping to expose in the UI", title="UI Name")
    ui_name_key: str | None = Field(None, alias="uiNameKey", description="The translation key for the name of the topology grouping to expose in the UI", title="UI Name Key")
