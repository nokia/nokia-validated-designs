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
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to use to match nodes that should be assigned to this tier.")
    tier: int = Field(..., description="The tier to assign to nodes that match the selector.")


class TopologyGroupingGroupSelectors(_EDABase):
    group: str = Field(..., description="The group to assign to nodes that match the selector.")
    node_selector: list[str] | None = Field(None, alias="nodeSelector", description="Label selector to use to match nodes that should be assigned to this group.")


class TopologyGroupingSpec(_EDABase):
    group_selectors: list[TopologyGroupingGroupSelectors] | None = Field(None, alias="groupSelectors", description="The set of selectors for assigning nodes to groups")
    tier_selectors: list[TopologyGroupingTierSelectors] | None = Field(None, alias="tierSelectors", description="The set of selectors for assigning nodes to tiers")
    ui_description: str | None = Field(None, alias="uiDescription", description="A description of the topology grouping to expose in the UI")
    ui_description_key: str | None = Field(None, alias="uiDescriptionKey", description="The translation key for the description of the topology grouping to expose in the UI")
    ui_name: str | None = Field(None, alias="uiName", description="The name of the topology grouping to expose in the UI")
    ui_name_key: str | None = Field(None, alias="uiNameKey", description="The translation key for the name of the topology grouping to expose in the UI")
