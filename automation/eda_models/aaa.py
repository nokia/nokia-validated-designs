"""
Auto-generated Pydantic v2 models for EDA aaa API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class NodeGroupTacacs(_EDABase):
    privilege_level: int | None = Field(None, alias="privilegeLevel", description="Set the privilege level for this group.", ge=0, le=15)


class NodeGroupRules(_EDABase):
    action: Literal['Deny', 'ReadWrite', 'Read'] = Field(..., description="Set the action for this entry.")
    match: str | None = Field(None, description="Set the match for this entry. This is a string to match input against - for example \"interface\" for srl or \"config...")
    operating_system: Literal['srl', 'sros'] = Field(..., alias="operatingSystem", description="Operating system to match against for this rule. Operating system to deploy this rule to.")


class NodeGroupSpec(_EDABase):
    group_name: str | None = Field(None, alias="groupName", description="Set the local name for this group. If not provided, the resource name will be used.")
    rules: list[NodeGroupRules] | None = Field(None, description="Rules for this group.")
    services: list[str] = Field(..., description="Enabled services for this group")
    superuser: bool | None = Field(None, description="Make members of this group superusers.")
    tacacs: NodeGroupTacacs | None = Field(None, description="TACACS configuration.")
