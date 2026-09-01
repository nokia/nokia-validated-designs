"""
Auto-generated Pydantic v2 models for EDA qos API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class QueueSpec(_EDABase):
    queue_id: int | None = Field(None, alias="queueID", description="The ID of the queue on which to apply the properties.  This is mandatory for usage of queus on SROS and is ignored on...", title="Queue ID")
    queue_type: Literal['Normal', 'PFC'] = Field(..., alias="queueType", description="QueueType specifies whether this is a normal queue or a PFC queue", title="Queue Type")
    traffic_type: Literal['Unicast', 'Multicast'] = Field(..., alias="trafficType", description="The traffic type of the queue, either unicast or multicast.", title="Traffic Type")
