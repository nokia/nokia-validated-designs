"""Unit tests for the 26.4.x / 26.8.x (services/protocols v2) CR generator backend.

These assert the v2 spec *shapes* that differ from 25.12 — the same shapes
validated against live EDA 26.4.2 and 26.8.1 clusters. They guard against
regressions without needing cluster access.

26.8 shares the backend and models with 26.4 on the fabric path (its spec
shapes are a strict superset), and diverges on the AI-fabric kinds, which get
their own section below.
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


@pytest.fixture()
def crs_26_8(intent) -> list[dict]:
    return generate(intent, registry=get_registry("26.8.1"))


@pytest.fixture()
def ai_intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "ai-dc" / "rail-optimized"
    if not design_dir.exists():
        pytest.skip("ai-dc/rail-optimized design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def ai_crs_26_8(ai_intent) -> list[dict]:
    return generate(ai_intent, registry=get_registry("26.8.1"))


@pytest.fixture()
def ai_crs_25_12(ai_intent) -> list[dict]:
    return generate(ai_intent, registry=get_registry("25.12.1"))


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


class TestFabricPathIdenticalOn26_8:
    """26.8's fabric-path spec shapes are a strict superset of 26.4's, so the
    same design must produce byte-identical CRs on both profiles. This is the
    assertion that justifies reusing eda_generator_v2 + eda_models.eda_26_4."""

    def test_3_stage_crs_identical_between_26_4_and_26_8(self, crs_v2, crs_26_8):
        assert crs_26_8 == crs_v2


class TestAiFabricKindsOn26_8:
    """The AI-fabric kinds are where 26.8 diverges. All four groups graduated
    and Backend carries breaking renames vs the 25.12 v1alpha1 shape."""

    def test_refused_on_26_4_but_generated_on_26_8(self, ai_intent):
        with pytest.raises(ValueError) as exc:
            generate(ai_intent, registry=get_registry("26.4.2"))
        assert "ai_backends" in str(exc.value)
        # ...while 26.8 emits them.
        crs = generate(ai_intent, registry=get_registry("26.8.1"))
        assert _by_kind(crs, "Backend")

    def test_ai_group_api_versions(self, ai_crs_26_8):
        av = {c["kind"]: c["apiVersion"] for c in ai_crs_26_8}
        assert av["Backend"] == "aifabrics.eda.nokia.com/v1"
        assert av["Queue"] == "qos.eda.nokia.com/v2"
        assert av["ForwardingClass"] == "qos.eda.nokia.com/v2"
        assert av["NodeGroup"] == "aaa.eda.nokia.com/v1"
        assert av["TopologyGrouping"] == "topologies.eda.nokia.com/v1"

    def test_backend_selector_fields_are_plural(self, ai_crs_26_8):
        spec = _by_kind(ai_crs_26_8, "Backend")[0]["spec"]
        for stripe in spec["stripes"]:
            assert "nodeSelectors" in stripe
            assert "nodeSelector" not in stripe
        for group in spec["gpuIsolationGroups"]:
            assert "interfaceSelectors" in group
            assert "interfaceSelector" not in group
        connector = spec.get("stripeConnector")
        if connector:
            assert "nodeSelectors" in connector and "linkSelectors" in connector
            assert "nodeSelector" not in connector
            assert "linkSelector" not in connector

    def test_backend_system_pool_recased(self, ai_crs_26_8):
        """``systemPoolIPV4`` -> ``systemPoolIPv4`` on the spec and everywhere
        it is nested. The old casing is silently dropped by the API server."""
        spec = _by_kind(ai_crs_26_8, "Backend")[0]["spec"]
        blocks = [spec, *spec["stripes"]]
        if spec.get("stripeConnector"):
            blocks.append(spec["stripeConnector"])
        for block in blocks:
            assert "systemPoolIPV4" not in block
        assert spec["systemPoolIPv4"]

    def test_backend_gpu_vlan_recased(self, ai_crs_26_8):
        for stripe in _by_kind(ai_crs_26_8, "Backend")[0]["spec"]["stripes"]:
            assert "gpuVLAN" in stripe
            assert "gpuVlan" not in stripe

    def test_rocev2_qos_fields_carry_units(self, ai_crs_26_8):
        qos = _by_kind(ai_crs_26_8, "Backend")[0]["spec"]["rocev2QoS"]
        assert "pfcDeadlockDetectionTimerMs" in qos
        assert "pfcDeadlockRecoveryTimerMs" in qos
        assert "queueMaximumBurstSizeBytes" in qos
        for old in (
            "pfcDeadlockDetectionTimer",
            "pfcDeadlockRecoveryTimer",
            "queueMaximumBurstSize",
        ):
            assert old not in qos

    def test_backend_omits_unset_26_8_only_blocks(self, ai_crs_26_8):
        """``type`` is omitted rather than half-filled: its ``overlay``
        sub-field is mandatory once the block is present."""
        spec = _by_kind(ai_crs_26_8, "Backend")[0]["spec"]
        assert "type" not in spec
        assert "addressAllocation" not in spec

    def test_queue_type_enum_recased_to_pfc(self, ai_crs_26_8):
        types = {q["spec"]["queueType"] for q in _by_kind(ai_crs_26_8, "Queue")}
        assert "PFC" in types
        assert "Pfc" not in types
        assert types <= {"Normal", "PFC"}

    def test_topology_grouping_carries_required_group_ui_name(self, ai_crs_26_8):
        """``groupUIName`` is new in 26.8 and required; it falls back to the
        group key, which is what the UI would have displayed anyway."""
        spec = _by_kind(ai_crs_26_8, "TopologyGrouping")[0]["spec"]
        for gs in spec["groupSelectors"]:
            assert gs["groupUIName"] == gs["group"]

    def test_multi_namespace_resources_land_in_their_namespace(self, ai_crs_26_8):
        namespaces = {c["metadata"]["namespace"] for c in ai_crs_26_8}
        assert namespaces == {"eda-system", "dc1-backend", "dc1-frontend"}
        # Namespace CRs themselves are created in eda-system.
        for ns_cr in _by_kind(ai_crs_26_8, "Namespace"):
            assert ns_cr["metadata"]["namespace"] == "eda-system"

    def test_explicit_fabrics_emitted_per_definition(self, ai_intent, ai_crs_26_8):
        fabrics = _by_kind(ai_crs_26_8, "Fabric")
        assert len(fabrics) == len(ai_intent.fabrics)
        for fab in fabrics:
            assert fab["apiVersion"] == "fabrics.eda.nokia.com/v1"
            assert "leafNodeSelectors" in fab["spec"]["leafs"]
            assert "leafNodeSelector" not in fab["spec"]["leafs"]


_DLB_CONFIGLETS = {"dlb", "ip-load-balance-network-instance"}


def _configlet_names(crs: list[dict]) -> set[str]:
    return {c["metadata"]["name"] for c in _by_kind(crs, "Configlet")}


def _dlb_configlet(crs: list[dict], name: str) -> dict:
    return [c for c in _by_kind(crs, "Configlet") if c["metadata"]["name"] == name][0]


class TestDynamicLoadBalancing:
    """Dynamic load balancing is intent on the Backend, rendered as configlets.

    26.8's Backend CR has a native ``dynamicLoadBalancing`` block, but using it
    regresses this design: EDA renders the prefix binding into every network
    instance the Backend owns, including the GPU isolation-group VRF whose
    routes are leaked from ``default``. On 26.8.1 that cost every rail its ECMP
    paths and left GPUs unable to reach their rail gateway, so both generator
    backends render the balancer as configlets instead.
    """

    def test_native_block_not_emitted_on_26_8(self, ai_crs_26_8):
        assert "dynamicLoadBalancing" not in _by_kind(ai_crs_26_8, "Backend")[0]["spec"]

    def test_25_12_backend_has_no_native_block(self, ai_crs_25_12):
        assert "dynamicLoadBalancing" not in _by_kind(ai_crs_25_12, "Backend")[0]["spec"]

    @pytest.mark.parametrize("profile", ["25.12", "26.8"])
    def test_both_profiles_render_the_configlets(self, request, profile):
        crs = request.getfixturevalue(f"ai_crs_{profile.replace('.', '_')}")
        assert _DLB_CONFIGLETS <= _configlet_names(crs)

    def test_prefix_binding_stays_on_the_default_network_instance(self, ai_crs_26_8):
        """The whole reason the native block is avoided: the balancer must not
        be bound inside the GPU isolation-group VRF."""
        cfg = _dlb_configlet(ai_crs_26_8, "ip-load-balance-network-instance")
        assert cfg["spec"]["configs"][0]["path"] == '.network-instance{.name=="default"}'

    def test_configlets_target_the_rail_leaves(self, ai_crs_25_12):
        """25.12's Configlet spells the selector singular; the design's leaf
        selector has to survive the trip through the intent to reach it."""
        seen = {
            c["metadata"]["name"]: c["spec"]["endpointSelector"]
            for c in _by_kind(ai_crs_25_12, "Configlet")
            if c["metadata"]["name"] in _DLB_CONFIGLETS
        }
        assert seen == {name: ["eda.nokia.com/role=leaf"] for name in _DLB_CONFIGLETS}

    def test_26_8_configlets_use_plural_selectors(self, ai_crs_26_8):
        cfg = _dlb_configlet(ai_crs_26_8, "dlb")
        assert cfg["spec"]["endpointSelectors"] == ["eda.nokia.com/role=leaf"]
        assert "endpointSelector" not in cfg["spec"]

    def test_tunables_reach_the_rendered_config(self, ai_intent):
        """The flowset size is a string in SR Linux config."""
        ai_intent.ai_backends[0].dynamic_load_balancing.flowset_size = 4096
        for profile in ("25.12.1", "26.8.1"):
            crs = generate(ai_intent, registry=get_registry(profile))
            config = _dlb_configlet(crs, "dlb")["spec"]["configs"][0]["config"]
            assert '"flowset-size": "4096"' in config

    def test_per_packet_mode_maps_to_srl_syntax(self, ai_intent):
        ai_intent.ai_backends[0].dynamic_load_balancing.mode = "PerPacket"
        crs = generate(ai_intent, registry=get_registry("26.8.1"))
        config = _dlb_configlet(crs, "dlb")["spec"]["configs"][0]["config"]
        assert '"mode": "packet-based"' in config

    def test_disabled_emits_nothing(self, ai_intent):
        ai_intent.ai_backends[0].dynamic_load_balancing = None
        for profile in ("25.12.1", "26.8.1"):
            crs = generate(ai_intent, registry=get_registry(profile))
            assert _configlet_names(crs) & _DLB_CONFIGLETS == set()
            assert "dynamicLoadBalancing" not in _by_kind(crs, "Backend")[0]["spec"]
