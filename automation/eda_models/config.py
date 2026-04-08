"""
Auto-generated Pydantic v2 models for EDA config API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class ConfigletConfigurations(_EDABase):
    config: str = Field(..., description="JSON-formatted string representing the configuration to apply.", title="Configuration")
    operation: Literal['Create', 'Update', 'Delete'] = Field(..., description="Indicates the operation in which to apply the configuration.", title="Operation")
    path: str = Field(..., description="Path to apply the configuration in jspath notation, including any keys if relevant, e.g. .system.information.", title="Path")


class ConfigletSpec(_EDABase):
    configs: list[ConfigletConfigurations] = Field(..., description="Configurations to apply, being sets of paths, operations and JSON configurations.", title="Configurations")
    endpoint_selector: list[str] | None = Field(None, alias="endpointSelector", description="Label selector to use to match targets to deploy Configlet to.", title="Target Selector")
    endpoints: list[str] | None = Field(None, description="Reference to targets to deploy Configlet to.", title="Targets")
    operating_system: Literal['srl', 'sros'] | None = Field(None, alias="operatingSystem", description="Operating system to match against when selecting targets.", title="Operating System")
    priority: int | None = Field(0, description="Priority of this Configlet, between -100 and 100. Higher priorities overwrite lower priorities in the event of confli...", title="Priority", ge=-100, le=100)
    version: str | None = Field(None, description="Version to match against when selecting targets.", title="Version")
