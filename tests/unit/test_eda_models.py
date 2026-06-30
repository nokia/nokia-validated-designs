"""Regression tests for generated EDA Pydantic models.

These guard against the codegen field-drop bug: when two inline sub-schemas
collapse to the same class name (e.g. ``underlayProtocol.bgp`` and
``overlayProtocol.bgp`` both -> ``FabricBgp``), fields unique to one side used
to be silently dropped. The generator now unions colliding sub-schemas; if a
future regen regresses, these assertions fail in CI instead of surfacing as a
fabric with BGP up and zero routes in production.
"""

from automation.eda_models.fabrics import FabricBgp
from automation.eda_models.protocols import StaticRouteBfd
from automation.eda_models.routingpolicies import PolicyBgp


# (model class, field name) pairs that must survive codegen. Each was dropped
# by the old leaf-property-name de-dup and is now recovered by the union.
REQUIRED_FIELDS = [
    # underlay-only field on the shared FabricBgp class
    (FabricBgp, "asn_pool"),
    # overlay-only fields — present so the union didn't regress the other side
    (FabricBgp, "autonomous_system"),
    (FabricBgp, "cluster_id"),
    # recovered on the shared StaticRouteBfd class
    (StaticRouteBfd, "local_discriminator"),
    (StaticRouteBfd, "remote_discriminator"),
    # recovered on the shared PolicyBgp class
    (PolicyBgp, "as_path_match"),
    (PolicyBgp, "evpn_route_type"),
]


class TestNoDroppedFields:
    def test_required_fields_present(self):
        missing = [
            f"{cls.__name__}.{field}"
            for cls, field in REQUIRED_FIELDS
            if field not in cls.model_fields
        ]
        assert not missing, f"codegen dropped fields: {missing}"

    def test_fabric_bgp_unions_underlay_and_overlay(self):
        # asn_pool is underlay-only; autonomous_system is overlay-only.
        fields = FabricBgp.model_fields
        assert "asn_pool" in fields and "autonomous_system" in fields

    def test_asn_pool_serializes_with_camel_alias(self):
        bgp = FabricBgp(asnPool="leaf-asn")
        assert bgp.model_dump(by_alias=True, exclude_none=True) == {
            "asnPool": "leaf-asn"
        }
