"""
Auto-generated Pydantic v2 models for EDA routingpolicies API.

DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _EDABase(BaseModel):
    """Base class for all EDA models — allows population by field name or alias."""
    model_config = ConfigDict(populate_by_name=True)

class PrefixSetPrefixes(_EDABase):
    end_range: int | None = Field(None, alias="endRange", description="The end range when using a range to match prefixes.", title="End Range", ge=0, le=128)
    exact: bool | None = Field(None, description="Indicates if it is an exact match. Ignores the StartRange and EndRange if this param is set.", title="Exact")
    prefix: str = Field(..., description="The IPv4 or IPv6 prefix in CIDR notation with mask.", title="Prefix")
    start_range: int | None = Field(None, alias="startRange", description="If specifying a range, this is the start of the range.", title="Start Range", ge=0, le=128)


class PrefixSetSpec(_EDABase):
    configured_name: str | None = Field(None, alias="configuredName", description="The name of the prefixset to configure on the device.", title="Configured Name")
    prefix: list[PrefixSetPrefixes] = Field(..., description="List of IPv4 or IPv6 prefixes in CIDR notation.", title="Prefixes")


class PolicyAsPath(_EDABase):
    as_path_expression: str | None = Field(None, alias="asPathExpression", description="A singular regular expression string to match against AS_PATH objects. Mutually exclusive with the ASPathSet reference.", title="AS Path Expression")
    as_path_set: str | None = Field(None, alias="asPathSet", description="Reference to an ASPathSet resource. Mutually exclusive with the ASPathExpression.", title="AS Path Set")
    match_set_options: Literal['Any', 'All', 'Invert'] | None = Field(None, alias="matchSetOptions", description="The matching criteria that applies to the members in the referenced set.", title="Match Set Options")


class PolicyMatch(_EDABase):
    bgp: PolicyBgp | None = Field(None, description="Configuration for BGP-specific policy match criteria.", title="BGP")
    family: list[str] | None = Field(None, description="Address families that the route belongs to.", title="Family")
    prefix_set: str | None = Field(None, alias="prefixSet", description="Reference to a PrefixSet resource.", title="Prefix Set")
    protocol: Literal['AGGREGATE', 'ARP_ND', 'BGP', 'BGP_EVPN', 'BGP_EVPN_IFF', 'BGP_EVPN_IFL_HOST', 'DHCP', 'GRIBI', 'HOST', 'ISIS', 'LOCAL', 'LINUX', 'NDK1', 'NDK2', 'OSPFV2', 'OSPFV3', 'STATIC'] | None = Field(None, description="The route protocol type to match.", title="Protocol")
    tags: PolicyTags | None = Field(None, description="Match based on the internal route tags associated with the route.", title="Tags")


class PolicyAction(_EDABase):
    bgp: PolicyBgp | None = Field(None, description="Actions related to the BGP protocol.", title="BGP")
    policy_result: Literal['accept', 'reject', 'NextPolicy', 'NextStatement'] | None = Field(None, alias="policyResult", description="Final disposition for the route.", title="Policy Result")
    tags: PolicyTags | None = Field(None, description="Manipulate internal route tags associated with the route.", title="Tags")


class PolicyStatements(_EDABase):
    action: PolicyAction | None = Field(None, description="Actions for routes that match the policy statement.", title="Action")
    match: PolicyMatch | None = Field(None, description="Match conditions of the policy statement.", title="Match")
    name: str = Field(..., description="Name of the policy statement.", title="Name")


class PolicyTags(_EDABase):
    tag_set: str | None = Field(None, alias="tagSet", description="Add tags to the route from the referenced Tag Set.", title="Set Tags")


class PolicySetMed(_EDABase):
    numerical_value: int | None = Field(None, alias="numericalValue", description="Fixed numerical value to set or add/subtract.", title="MED Value", ge=0, le=4294967295)
    operation: Literal['Set', 'Add', 'Subtract'] | None = Field(None, description="The operation to perform on the MED value.", title="Operation")
    value_type: Literal['Fixed', 'IGP'] | None = Field(None, alias="valueType", description="Use a fixed value or an IGP metric to adjust the MED.", title="Value Type")


class PolicyModifyCommunities(_EDABase):
    add: list[str] | None = Field(None, description="List of community sets to add to the route.", title="Add Communities")
    remove: list[str] | None = Field(None, description="List of community sets to remove from the route.", title="Remove Communities")
    replace: list[str] | None = Field(None, description="List of community sets to replace the existing communities with. Cannot be combined with Add or Remove.", title="Replace Communities")


class PolicyBgp(_EDABase):
    as_path_prepend: int | None = Field(None, alias="asPathPrepend", description="AS number to prepend to the AS Path attributes.", title="AS Path Prepend", ge=1, le=4294967295)
    as_path_remove: bool | None = Field(None, alias="asPathRemove", description="Clear the AS path to make it empty.", title="AS Path Remove")
    as_path_replace: list[int] | None = Field(None, alias="asPathReplace", description="Replace the existing AS path with a new AS_SEQUENCE containing the listed AS numbers.", title="AS Path Replace")
    community_set: PolicyModifyCommunities | None = Field(None, alias="communitySet", description="Modify BGP communities associated with the route using hybrid Community Sets.", title="Modify Communities")
    local_preference: int | None = Field(None, alias="localPreference", description="Set a new LOCAL_PREF value for matching BGP routes.", title="Set Local Preference", ge=0, le=4294967295)
    med: PolicySetMed | None = Field(None, description="Set a new MED value.", title="Set MED")
    set_origin: Literal['egp', 'igp', 'incomplete'] | None = Field(None, alias="setOrigin", description="Set a new ORIGIN attribute for matching BGP routes.", title="Set Origin")
    as_path_match: PolicyAsPath | None = Field(None, alias="asPathMatch", description="AS Path match criteria.", title="AS Path")
    evpn_route_type: list[int] | None = Field(None, alias="evpnRouteType", description="Match conditions for EVPN route types.", title="EVPN Route Type")


class PolicyDefaultAction(_EDABase):
    bgp: PolicyBgp | None = Field(None, description="Actions related to the BGP protocol.", title="BGP")
    policy_result: Literal['accept', 'reject', 'NextPolicy', 'NextStatement'] | None = Field(None, alias="policyResult", description="Final disposition for the route.", title="Policy Result")
    tags: PolicyTags | None = Field(None, description="Manipulate internal route tags associated with the route.", title="Tags")


class PolicySpec(_EDABase):
    configured_name: str | None = Field(None, alias="configuredName", description="The name of the policy to configure on the device.", title="Configured Name")
    default_action: PolicyDefaultAction | None = Field(None, alias="defaultAction", description="The default action to apply if no other actions are defined.", title="Default Action")
    statement: list[PolicyStatements] | None = Field(None, description="List of policy statements.", title="Statements")
