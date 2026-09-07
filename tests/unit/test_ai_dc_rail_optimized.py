"""Unit tests for the rail-optimized AI-DC design and multi-namespace plumbing.

Two things are being pinned here. First, the *geometry*: the rail-optimized
property (server NIC *r* lands on leaf *r* of its stripe), the spine-port
allocation, and the capacity guards that stop the builder emitting port numbers
a platform does not have. Second, the *namespace* behaviour that this design is
the first consumer of, which the three single-namespace designs must not
regress on.
"""

import copy
from collections import Counter
from pathlib import Path

import pytest

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.designs import ai_dc_rail_optimized as design
from automation.executors.eda import EdaClient
from automation.generators import clab_generator

DESIGN_DIR = (
    Path(__file__).resolve().parents[2]
    / "validated-designs" / "ai-dc" / "rail-optimized"
)

BACKEND_NS = "dc1-backend"
FRONTEND_NS = "dc1-frontend"
SYSTEM_NS = "eda-system"


@pytest.fixture(scope="module")
def inputs():
    if not DESIGN_DIR.is_dir():
        pytest.skip("ai-dc/rail-optimized not present in checkout")
    return load_inputs(DESIGN_DIR)


@pytest.fixture
def topology(inputs):
    return copy.deepcopy(inputs[0])


@pytest.fixture
def services(inputs):
    return copy.deepcopy(inputs[1])


@pytest.fixture
def intent(topology, services):
    return design.build(topology, services)


def _build(topology, services, **backend_overrides):
    """Build with *backend_overrides* merged into the backend block."""
    topology = copy.deepcopy(topology)
    topology["backend"].update(backend_overrides)
    return design.build(topology, copy.deepcopy(services))


class TestDesignRegistration:
    def test_dispatches_through_the_shared_registry(self, topology, services):
        # build_intent is the entry point deploy.py uses; a design that only
        # works when imported directly is not actually registered.
        intent = build_intent(topology, services)
        assert intent.design == "ai-dc-rail-optimized"


class TestBackendGeometry:
    def test_leaf_count_is_stripes_times_rail_size(self, topology, intent):
        backend = topology["backend"]
        leaves = [n for n in intent.nodes if n.namespace == BACKEND_NS
                  and n.role == "leaf"]
        assert len(leaves) == backend["stripes"] * backend["rail_size"]

    def test_every_server_nic_lands_on_a_different_leaf_of_its_stripe(self, intent):
        # This is the rail-optimized property: rail r of every server in a
        # stripe terminates on leaf r, so a rail-local collective never leaves
        # the leaf.
        gpu_servers = [s for s in intent.servers if s.role == "gpu"]
        assert gpu_servers
        for server in gpu_servers:
            rail_links = [
                link for link in server.links
                if not any(
                    link.eth_index in bond.eth_indices for bond in server.bonds
                )
            ]
            assert len(rail_links) == 8
            assert len({link.node for link in rail_links}) == 8

    def test_all_servers_in_a_stripe_share_a_leaf_but_not_a_port(self, intent):
        by_leaf: dict[str, set[str]] = {}
        for server in intent.servers:
            if server.role != "gpu":
                continue
            bonded = {i for bond in server.bonds for i in bond.eth_indices}
            for link in server.links:
                if link.eth_index in bonded:
                    continue
                by_leaf.setdefault(link.node, set()).add(link.interface)
        # 2 servers per stripe, so every rail leaf carries exactly 2 GPU ports.
        assert {len(ports) for ports in by_leaf.values()} == {2}

    def test_gpu_ports_start_after_the_uplinks(self, topology, services):
        intent = _build(topology, services, uplinks_per_leaf=4)
        gpu_ifaces = {
            e.interface for e in intent.edge_interfaces
            if e.namespace == BACKEND_NS
        }
        # 4 uplinks occupy ports 1-4, so the 2 GPU ports are 5 and 6.
        assert gpu_ifaces == {"ethernet-1-5", "ethernet-1-6"}

    def test_uplinks_derived_from_oversubscription(self, topology):
        # 2 servers x 400G of GPU into 400G uplinks, non-blocking → 2 uplinks.
        # The spine layer is pinned at 2 so the rounding below does not lift
        # the result and hide the calculation being tested here.
        backend = copy.deepcopy(topology["backend"])
        backend["spine"]["count"] = 2
        backend["oversubscription_ratio"] = 1.0
        assert design._BackendPlan(backend).uplinks_per_leaf == 2

    def test_derived_uplinks_round_up_to_a_whole_round_of_spines(self, topology):
        # 4:1 oversubscription only needs 1 uplink, but a leaf cannot cable to
        # half the spine layer, so it is rounded up to one full round of 2.
        backend = copy.deepcopy(topology["backend"])
        backend["spine"]["count"] = 2
        backend["oversubscription_ratio"] = 4.0
        assert design._BackendPlan(backend).uplinks_per_leaf == 2

    def test_a_wider_spine_layer_raises_the_derived_uplink_count(self, topology):
        backend = copy.deepcopy(topology["backend"])
        backend["spine"]["count"] = 4
        backend["oversubscription_ratio"] = 4.0
        assert design._BackendPlan(backend).uplinks_per_leaf == 4

    def test_an_explicit_unbalanced_uplink_count_is_still_rejected(
        self, topology, services
    ):
        # Rounding is a courtesy for the derived path only: an explicit value
        # is what the operator asked for, so a value that cannot be cabled
        # evenly is an error rather than something to silently adjust.
        with pytest.raises(ValueError, match="does not spread evenly"):
            _build(topology, services, uplinks_per_leaf=3)

    def test_every_spine_carries_the_same_number_of_leaf_ports(self, intent):
        spines = {
            n.name for n in intent.nodes
            if n.namespace == BACKEND_NS and n.role == "spine"
        }
        per_spine = Counter(
            link.local_node for link in intent.links
            if link.local_node in spines
        )
        assert len(set(per_spine.values())) == 1

    def test_no_spine_port_is_cabled_twice(self, intent):
        used = Counter(
            (link.local_node, link.local_interface) for link in intent.links
        )
        assert [pair for pair, n in used.items() if n > 1] == []

    def test_scales_past_the_reference_two_stripes(self, topology, services):
        topology = copy.deepcopy(topology)
        topology["backend"]["stripes"] = 6
        # The per-stripe override only names stripe 2; drop it so all six
        # stripes use the same platform.
        topology["backend"]["leaf"]["platform_per_stripe"] = {}
        intent = design.build(topology, copy.deepcopy(services))
        leaves = [
            n for n in intent.nodes
            if n.namespace == BACKEND_NS and n.role == "leaf"
        ]
        assert len(leaves) == 6 * 8
        assert len([s for s in intent.servers if s.role == "gpu"]) == 6 * 2

    def test_per_stripe_platform_override_applies(self, intent):
        by_name = {n.name: n for n in intent.nodes}
        assert by_name["stripe1-leaf1"].platform == "7220 IXR-H4"
        assert by_name["stripe2-leaf1"].platform == "7220 IXR-H5-64D"


class TestCapacityGuards:
    def test_rejects_a_fabric_too_large_for_its_spines(self, topology, services):
        topology = copy.deepcopy(topology)
        topology["backend"]["stripes"] = 8
        topology["backend"]["servers_per_stripe"] = 16
        topology["backend"]["leaf"]["platform_per_stripe"] = {}
        with pytest.raises(ValueError, match="which only have"):
            design.build(topology, copy.deepcopy(services))

    def test_rejects_uplinks_that_do_not_spread_over_the_spines(
        self, topology, services
    ):
        with pytest.raises(ValueError, match="does not spread evenly"):
            _build(topology, services, uplinks_per_leaf=3)

    def test_rejects_a_platform_not_allowed_as_spine(self, topology, services):
        topology = copy.deepcopy(topology)
        topology["backend"]["spine"]["platform"] = "7220 IXR-D2L"
        with pytest.raises(ValueError, match="not allowed as spine"):
            design.build(topology, copy.deepcopy(services))

    def test_rejects_stripe_ids_above_what_eda_accepts(self, topology, services):
        # The Backend CRD caps stripeID at 256, and a step that overshoots it
        # would otherwise only surface as a validation error from the CR model,
        # after the whole intent had been built.
        with pytest.raises(ValueError, match="EDA allows"):
            _build(topology, services, stripe_id_step=design.EDA_MAX_STRIPE_ID)

    @pytest.mark.parametrize("field", ["stripes", "rail_size", "servers_per_stripe"])
    def test_rejects_a_zero_dimension(self, topology, services, field):
        with pytest.raises(ValueError, match="at least 1"):
            _build(topology, services, **{field: 0})


class TestFrontendGeometry:
    @staticmethod
    def _attached_server_counts(topology) -> tuple[int, int]:
        """``(gpu, storage)`` server counts the frontend leaves have to host."""
        backend = topology["backend"]
        return (
            backend["stripes"] * backend["servers_per_stripe"],
            topology["frontend"]["storage_servers"]["count"],
        )

    def _frontend(self, topology, services, **leaf_overrides):
        topology = copy.deepcopy(topology)
        topology["frontend"]["leaf"].update(leaf_overrides)
        return design.build(topology, copy.deepcopy(services))

    def _frontend_links(self, intent):
        return [link for link in intent.links if link.namespace == FRONTEND_NS]

    def test_default_uplink_count_is_a_plain_full_mesh(self, intent):
        # 2 leaves x 2 spines.
        assert len(self._frontend_links(intent)) == 4

    def test_extra_uplinks_add_a_parallel_round_to_the_same_spines(
        self, topology, services
    ):
        intent = self._frontend(topology, services, uplinks_per_leaf=4)
        links = self._frontend_links(intent)
        assert len(links) == 8
        # Every declared uplink port is actually cabled — a port listed as an
        # uplink but left dark would also push the access ports up for nothing.
        leaf_ports = {
            link.remote_interface for link in links
            if link.remote_node.startswith("frontend-leaf")
        }
        assert leaf_ports == {f"ethernet-1-{n}" for n in range(1, 5)}

    def test_extra_uplinks_push_the_access_ports_up(self, topology, services):
        intent = self._frontend(topology, services, uplinks_per_leaf=4)
        ports = sorted(
            int(member.interface.rsplit("-", 1)[1])
            for lag in intent.lags
            for member in lag.members
        )
        # 4 uplinks take ports 1-4, then the GPU servers and the storage nodes.
        gpu, storage = self._attached_server_counts(topology)
        assert ports[0] == 5
        assert ports[-1] == 4 + gpu + storage

    def test_rejects_uplinks_that_are_not_whole_rounds_of_the_spine_layer(
        self, topology, services
    ):
        with pytest.raises(ValueError, match="positive multiple"):
            self._frontend(topology, services, uplinks_per_leaf=3)

    def test_rejects_fewer_uplinks_than_spines(self, topology, services):
        with pytest.raises(ValueError, match="positive multiple"):
            self._frontend(topology, services, uplinks_per_leaf=1)

    def test_gpu_and_storage_servers_are_multihomed_to_every_leaf(self, intent):
        for lag in intent.lags:
            assert len(lag.members) == 2
            assert lag.multihoming_mode == "all-active"
            assert len({m.node for m in lag.members}) == 2

    def test_gpu_lags_are_tagged_and_storage_lags_untagged(
        self, topology, intent
    ):
        tagged = [
            lag for lag in intent.lags
            if "eda.nokia.com/tagged-v100" in lag.labels
        ]
        untagged = [
            lag for lag in intent.lags
            if "eda.nokia.com/untagged-v100" in lag.labels
        ]
        gpu, storage = self._attached_server_counts(topology)
        assert len(tagged) == gpu
        assert len(untagged) == storage
        # No LAG may carry both: the VLAN selectors would match it twice.
        assert not set(id(l) for l in tagged) & set(id(l) for l in untagged)

    def test_lacp_admin_keys_are_unique(self, intent):
        keys = [lag.lacp.admin_key for lag in intent.lags]
        assert len(keys) == len(set(keys))


class TestNamespaceSplit:
    def test_both_fabrics_plus_the_system_namespace_are_in_use(self, intent):
        assert intent.namespaces_in_use() == [BACKEND_NS, FRONTEND_NS, SYSTEM_NS]

    def test_namespaces_must_differ(self, topology, services):
        topology = copy.deepcopy(topology)
        topology["frontend"]["namespace"] = topology["backend"]["namespace"]
        with pytest.raises(ValueError, match="must differ"):
            design.build(topology, copy.deepcopy(services))

    def test_backend_and_frontend_resources_land_in_their_own_namespace(
        self, intent
    ):
        assert {b.namespace for b in intent.ai_backends} == {BACKEND_NS}
        assert {f.namespace for f in intent.fabrics} == {FRONTEND_NS}
        assert {q.namespace for q in intent.queues} == {BACKEND_NS}
        assert {v.namespace for v in intent.vlans} == {FRONTEND_NS}
        assert {b.namespace for b in intent.bridge_domains} == {FRONTEND_NS}

    def test_namespace_crs_are_created_in_the_system_namespace(self, intent):
        # A Namespace CR is itself namespaced: it lives inside its parent.
        assert {n.parent_namespace for n in intent.namespaces} == {SYSTEM_NS}
        assert {n.name for n in intent.namespaces} == {BACKEND_NS, FRONTEND_NS}

    def test_each_fabric_gets_its_own_copy_of_the_shared_pools(self, intent):
        asn_pools = [p for p in intent.index_pools if p.name == "asn-pool"]
        assert {p.namespace for p in asn_pools} == {BACKEND_NS, FRONTEND_NS}
        ip_pools = [p for p in intent.ip_pools if p.name == "systemipv4-pool"]
        assert {p.namespace for p in ip_pools} == {BACKEND_NS, FRONTEND_NS}

    def test_ns_of_falls_back_to_the_intent_default(self, intent):
        anonymous = copy.deepcopy(intent.queues[0])
        anonymous.namespace = ""
        assert intent.ns_of(anonymous) == intent.eda.namespace


class TestDesignOwnedNamespacesAreSeededWithEdaDefaultPools:
    """EDA seeds its own namespace with well-known index pools at install time,
    and resolves them by name inside the namespace of the resource being
    deployed.  A design that creates its own namespaces has to seed them, or the
    first BridgeDomain deployed there fails on a missing ``tunnel-index-pool``.
    """

    @pytest.fixture(scope="class")
    def pools_by_ns(self):
        from automation.eda_models.profiles import get_registry
        from automation.generators.eda_generator import generate

        topology, services = load_inputs(DESIGN_DIR)
        resources = generate(
            design.build(topology, services), registry=get_registry("25.12")
        )
        out: dict[str, set[str]] = {}
        for cr in resources:
            if cr["kind"] == "IndexAllocationPool":
                md = cr["metadata"]
                out.setdefault(md["namespace"], set()).add(md["name"])
        return out

    @pytest.mark.parametrize("ns", [BACKEND_NS, FRONTEND_NS])
    def test_every_eda_default_pool_exists_in_each_created_namespace(
        self, pools_by_ns, ns
    ):
        from automation.generators.eda_generator import _EDA_DEFAULT_INDEX_POOLS

        assert _EDA_DEFAULT_INDEX_POOLS.keys() <= pools_by_ns[ns]

    def test_the_pool_the_storage_bridge_domain_needs_is_seeded(
        self, pools_by_ns
    ):
        assert "tunnel-index-pool" in pools_by_ns[FRONTEND_NS]

    def test_a_design_declared_pool_is_not_duplicated_by_a_seeded_default(
        self, pools_by_ns
    ):
        # leafindex-pool carries pinned per-leaf allocations, so a seeded
        # default would silently drop them.
        from automation.generators.eda_generator import _EDA_DEFAULT_INDEX_POOLS

        assert "leafindex-pool" not in _EDA_DEFAULT_INDEX_POOLS
        assert "asn-pool" not in _EDA_DEFAULT_INDEX_POOLS

    def test_seeding_only_applies_to_namespaces_the_design_creates(self, intent):
        from automation.eda_models.profiles import get_registry
        from automation.generators.eda_generator import generate

        stripped = copy.deepcopy(intent)
        stripped.namespaces = []
        seeded = {
            cr["metadata"]["name"]
            for cr in generate(stripped, registry=get_registry("25.12"))
            if cr["kind"] == "IndexAllocationPool"
        }
        assert seeded == {"asn-pool", "leafindex-pool"}


class TestCrossReferenceValidationIsPerNamespace:
    def test_a_vlan_may_not_reference_a_bridge_domain_in_another_namespace(
        self, topology, services
    ):
        services = copy.deepcopy(services)
        services["vlans"][0]["bridge_domain"] = "does-not-exist"
        with pytest.raises(Exception):
            design.build(copy.deepcopy(topology), services)


class TestGeneratedCrs:
    @pytest.fixture(scope="class")
    def resources(self):
        from automation.eda_models.profiles import get_registry
        from automation.generators.eda_generator import generate

        topology, services = load_inputs(DESIGN_DIR)
        intent = design.build(topology, services)
        return generate(intent, registry=get_registry("25.12"))

    def test_every_cr_carries_an_explicit_namespace(self, resources):
        assert all(cr["metadata"].get("namespace") for cr in resources)

    def test_namespaces_are_discovered_in_deployment_order(self, resources):
        # The system namespace must come first: the Namespace CRs that create
        # the other two live in it.
        assert EdaClient._extract_namespaces(resources)[0] == SYSTEM_NS

    def test_the_backend_cr_describes_one_stripe_per_stripe(
        self, inputs, resources
    ):
        planned = inputs[0]["backend"]
        count = planned["stripes"]
        step = planned.get("stripe_id_step", 1)
        backend = next(cr for cr in resources if cr["kind"] == "Backend")
        assert backend["metadata"]["namespace"] == BACKEND_NS
        stripes = backend["spec"]["stripes"]
        assert [s["name"] for s in stripes] == [
            f"stripe{n}" for n in range(1, count + 1)
        ]
        # Stripe IDs land in the rail addressing, so they have to be the
        # stepped values the containerlab twin puts on the GPU NICs.
        assert [s["stripeID"] for s in stripes] == [
            n * step for n in range(1, count + 1)
        ]
        assert stripes[-1]["stripeID"] <= design.EDA_MAX_STRIPE_ID

    def test_the_frontend_is_an_explicit_fabric_cr(self, resources):
        fabric = next(cr for cr in resources if cr["kind"] == "Fabric")
        assert fabric["metadata"]["namespace"] == FRONTEND_NS
        assert fabric["spec"]["underlayProtocol"]["protocol"] == ["EBGP"]

    def test_same_named_resources_in_two_namespaces_do_not_collide(self):
        # compute_diff keys on namespace/kind:name. Keying on kind:name alone
        # would make one asn-pool shadow the other and the second would be
        # reported as unchanged — or deleted.
        def pool(namespace: str) -> dict:
            return {
                "apiVersion": "core.eda.nokia.com/v1",
                "kind": "IndexAllocationPool",
                "metadata": {"name": "asn-pool", "namespace": namespace},
                "spec": {"segments": [{"start": 100, "size": 3000}]},
            }

        client = EdaClient(url="https://eda.example.com")
        backend, frontend = pool(BACKEND_NS), pool(FRONTEND_NS)

        # Both already live: neither may be reported as missing.
        plan = client.compute_diff([backend, frontend], [backend, frontend])
        assert plan.total_ops == 0
        assert len(plan.unchanged) == 2

        # Only the backend copy is live: the frontend one is a create, not a
        # match against its same-named sibling.
        plan = client.compute_diff([backend, frontend], [backend])
        assert len(plan.creates) == 1
        assert plan.creates[0]["metadata"]["namespace"] == FRONTEND_NS


class TestContainerlabTwin:
    @pytest.fixture(scope="class")
    def plan(self):
        # The rail addressing and the server-facing port numbers are geometry,
        # so they are asserted against the plan rather than restated as
        # literals that a resized reference design would invalidate.
        topology, _ = load_inputs(DESIGN_DIR)
        return design._BackendPlan(topology["backend"])

    @pytest.fixture(scope="class")
    def clab(self, tmp_path_factory):
        import yaml

        topology, services = load_inputs(DESIGN_DIR)
        intent = design.build(topology, services)
        out = tmp_path_factory.mktemp("clab")
        path = clab_generator.generate(intent, out)
        return yaml.safe_load(path.read_text())["topology"], out

    def test_switches_and_servers_are_all_present(self, inputs, clab):
        topo, _ = clab
        backend, frontend = inputs[0]["backend"], inputs[0]["frontend"]
        gpu = backend["stripes"] * backend["servers_per_stripe"]
        storage = frontend["storage_servers"]["count"]
        switches = (
            backend["stripes"] * backend["rail_size"]
            + backend["spine"]["count"]
            + frontend["leaf"]["count"]
            + frontend["spine"]["count"]
        )
        assert len(topo["nodes"]) == switches + gpu + storage
        linux = {
            name for name, spec in topo["nodes"].items()
            if spec.get("kind") == "linux"
        }
        assert linux == {f"s{i}" for i in range(1, gpu + 1)} | {
            f"weka{i}" for i in range(1, storage + 1)
        }

    def test_declared_servers_replace_the_derived_clients(self, clab):
        topo, _ = clab
        # A design that declares its servers has said exactly how they are
        # cabled; synthetic cl-* clients would cable the same ports twice.
        assert not [name for name in topo["nodes"] if name.startswith("cl-")]

    def test_no_fabric_port_is_cabled_twice(self, clab):
        topo, _ = clab
        endpoints = [ep for link in topo["links"] for ep in link["endpoints"]]
        assert len(endpoints) == len(set(endpoints))

    def test_gpu_servers_bond_onto_a_tagged_subinterface(self, clab):
        _, out = clab
        script = (out / "client-configs" / "s1.sh").read_text()
        assert "ip link add bond0 type bond mode 802.3ad" in script
        # The leaf-side VLAN selects on the tag, so an untagged frame would
        # land in no bridge domain at all.
        assert "name bond0.100 type vlan id 100" in script
        assert "ip addr add 172.16.10.1/24 dev bond0.100" in script

    def test_storage_nodes_bond_untagged(self, clab):
        _, out = clab
        script = (out / "client-configs" / "weka1.sh").read_text()
        assert "ip addr add 172.16.10.11/24 dev bond0" in script
        assert "type vlan" not in script

    def test_rail_nics_are_addressed_individually_not_bonded(self, plan, clab):
        _, out = clab
        script = (out / "client-configs" / "s1.sh").read_text()
        # Each rail NIC goes to a different leaf, so bonding them would be
        # wrong; they each carry their own rail address.
        port = plan.gpu_port_index(1)
        for rail in range(1, plan.rail_size + 1):
            assert (
                f"ip -6 addr add fd00:{plan.stripe_id(1)}:{rail}"
                f":1:0:{port}:0:2/96 dev eth{rail}.100"
            ) in script

    def test_rail_nics_are_tagged_with_the_gpu_vlan(self, clab):
        _, out = clab
        script = (out / "client-configs" / "s1.sh").read_text()
        # The leaf's rail sub-interface is tagged, so an untagged frame from
        # the NIC would never reach the rail's IP-VRF.
        for rail in range(1, 9):
            assert (
                f"ip link add link eth{rail} name eth{rail}.100 "
                f"type vlan id 100"
            ) in script

    def test_rail_nic_mtu_matches_the_fabric_ip_mtu(self, clab):
        _, out = clab
        script = (out / "client-configs" / "s1.sh").read_text()
        # A NIC MTU above the rail ip-mtu silently drops large RDMA frames.
        assert "ip link set dev eth1 mtu 4136" in script
        assert "ip link set dev eth1.100 mtu 4136" in script

    def test_second_stripe_rails_use_global_rail_numbering(self, plan, clab):
        _, out = clab
        # The first server of stripe 2 follows the stripe-1 servers.
        first_of_stripe2 = plan.servers_per_stripe + 1
        script = (
            out / "client-configs" / f"s{first_of_stripe2}.sh"
        ).read_text()
        # EDA numbers rails across the whole fabric, so stripe 2 owns rails
        # 9..16 — restarting at 1 per stripe would miss the leaf's subnet.
        port = plan.gpu_port_index(1)
        for rail in range(1, plan.rail_size + 1):
            assert (
                f"ip -6 addr add fd00:{plan.stripe_id(2)}"
                f":{plan.global_rail(2, rail)}:1:0:{port}:0:2/96 "
                f"dev eth{rail}.100"
            ) in script

    def test_platform_types_are_mapped_not_defaulted(self, clab):
        topo, _ = clab
        assert topo["nodes"]["stripe1-leaf1"]["type"] == "ixr-h4"
        assert topo["nodes"]["stripe2-leaf1"]["type"] == "ixr-h5-64d"
        assert topo["nodes"]["frontend-spine1"]["type"] == "ixr-h5-64o"


class TestPlatformTypeMapping:
    def test_known_platforms_map_directly(self):
        assert clab_generator._clab_platform_type("7220 IXR-H5-64D") == \
            "ixr-h5-64d"

    def test_unknown_platform_falls_back_to_the_mechanical_transform(self):
        # A silent wrong-but-plausible default would build a lab whose port
        # count does not match the design, so the fallback follows the same
        # rule every mapped entry does.
        assert clab_generator._clab_platform_type("7220 IXR-X9-99Z") == \
            "ixr-x9-99z"

    def test_unmappable_platform_fails_loudly(self):
        with pytest.raises(ValueError, match="PLATFORM_TYPE_MAP"):
            clab_generator._clab_platform_type("mystery")
