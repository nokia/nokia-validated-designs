"""Unit tests for the 26.4.x (services/protocols v2) EDA CR generator backend.

These assert the v2 spec *shapes* that differ from 25.12 — the same shapes
validated against a live EDA 26.4.2 cluster. They guard against regressions
without needing cluster access.
"""

import pytest
from pathlib import Path

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.core.models import FabricIntent
from automation.generators.eda_generator import generate
from automation.eda_models.profiles import get_registry


DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


@pytest.fixture()
def intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def crs_v2(intent) -> list[dict]:
    return generate(intent, registry=get_registry("26.4.2"))


def _by_kind(crs: list[dict], kind: str) -> list[dict]:
    return [c for c in crs if c["kind"] == kind]


class TestV2ApiVersions:
    def test_services_and_protocols_are_v2(self, crs_v2):
        av = {c["kind"]: c["apiVersion"] for c in crs_v2}
        assert av["BridgeDomain"] == "services.eda.nokia.com/v2"
        assert av["Router"] == "services.eda.nokia.com/v2"
        assert av["VLAN"] == "services.eda.nokia.com/v2"
        assert av["IRBInterface"] == "services.eda.nokia.com/v2"
        assert av["StaticRoute"] == "protocols.eda.nokia.com/v2"

    def test_graduated_groups_are_v1(self, crs_v2):
        av = {c["kind"]: c["apiVersion"] for c in crs_v2}
        assert av["Fabric"] == "fabrics.eda.nokia.com/v1"
        assert av["Interface"] == "interfaces.eda.nokia.com/v1"
        assert av["Init"] == "bootstrap.eda.nokia.com/v1"
        assert av["Configlet"] == "config.eda.nokia.com/v1"


class TestV2SpecShapes:
    def test_bridge_domain_vni_under_encap_options(self, crs_v2):
        bd = _by_kind(crs_v2, "BridgeDomain")[0]["spec"]
        assert bd["type"] == "EVPNVXLAN"
        assert bd["encapOptions"]["vxlan"]["vni"]  # vni moved under encapOptions
        assert "vni" not in bd  # not at the top level anymore
        assert "enabled" in bd["macLearning"]  # macLearning is now an object

    def test_router_node_selectors_plural_and_encap(self, crs_v2):
        spec = _by_kind(crs_v2, "Router")[0]["spec"]
        assert "nodeSelectors" in spec
        assert "nodeSelector" not in spec
        assert spec["encapOptions"]["vxlan"]["vni"]

    def test_vlan_interface_selectors_plural(self, crs_v2):
        spec = _by_kind(crs_v2, "VLAN")[0]["spec"]
        assert "interfaceSelectors" in spec
        assert "interfaceSelector" not in spec

    def test_irb_ipv4_block_and_addresses(self, crs_v2):
        spec = _by_kind(crs_v2, "IRBInterface")[0]["spec"]
        # arpTimeout moved into the ipv4 block
        assert "ipv4" in spec and "arpTimeoutSeconds" in spec["ipv4"]
        assert "arpTimeout" not in spec

    def test_interface_enums_recased(self, crs_v2):
        for iface in _by_kind(crs_v2, "Interface"):
            spec = iface["spec"]
            assert spec["type"] in ("Interface", "LAG", "Loopback")
            if "encapType" in spec:
                assert spec["encapType"] in ("Null", "Dot1q")

    def test_init_has_no_dhcp_mgmt(self, crs_v2):
        spec = _by_kind(crs_v2, "Init")[0]["spec"]
        assert spec.get("commitSave") is True
        assert "mgmt" not in spec or "ipv4DHCP" not in spec.get("mgmt", {})

    def test_toponode_production_address_is_cidr(self, crs_v2):
        """26.4 renders a static mgmt0 from productionAddress; SR Linux requires
        it as an ip-prefix (CIDR), so the bare mgmt IP must be zoned."""
        topo = _by_kind(crs_v2, "TopoNode")
        assert topo, "design emits no TopoNodes"
        for tn in topo:
            ipv4 = tn["spec"].get("productionAddress", {}).get("ipv4", "")
            if ipv4:
                assert "/" in ipv4, f"{tn['metadata']['name']} prodAddr not CIDR: {ipv4}"
                assert ipv4.endswith("/24")

    def test_default_mtu_renamed_fields(self, crs_v2):
        mtus = _by_kind(crs_v2, "DefaultMTU")
        if not mtus:
            pytest.skip("design emits no DefaultMTU")
        spec = mtus[0]["spec"]
        assert "nodeSelectors" in spec or "nodes" in spec
        assert "nodeSelector" not in spec
        if "layer2SubifMTU" in spec or "layer2SubinterfaceMTU" in spec:
            assert "layer2SubinterfaceMTU" in spec


class TestV2DefaultUnaffected:
    def test_default_registry_still_v1(self, intent):
        """The default profile must still emit v1 services CRs."""
        crs = generate(intent)  # default registry
        av = {c["kind"]: c["apiVersion"] for c in crs}
        assert av["BridgeDomain"] == "services.eda.nokia.com/v1"
        bd = next(c for c in crs if c["kind"] == "BridgeDomain")["spec"]
        assert "vni" in bd  # v1 keeps vni at the top level
