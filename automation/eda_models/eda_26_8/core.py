"""
Auto-generated Pydantic v2 models for EDA core API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class NamespaceBootstrap(_EDABase):
    from_namespace: str | None = Field(None, alias="fromNamespace", description="The namespace from which to bootstrap resources.  If empty, bootstrap resources are taken from the installed applicat...", title="From namespace")


class NamespaceSpec(_EDABase):
    bootstrap: NamespaceBootstrap | None = Field(None, description="Bootstrap configuration for the namespace - if empty no bootstrapping is performed and namespace will be empty.", title="Bootstrap")
    description: str | None = Field(None, description="An optional description of the use of the namespace.", title="Description")
