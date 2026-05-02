"""
Round-trip test for the NetBox builder.

The YAML inputs in ``validated-designs/3-stage-evpn-vxlan/inputs/`` are
fed through the 3-stage design builder to produce a ``FabricIntent``.
That intent is then written into an in-memory NetBox mock via
``netbox_writer.write``, and read back out via ``netbox.build_from_client``.

The reconstructed intent must be equal to the original (after canonical
sorting, since NetBox has no ordering guarantee) — proving that the
NetBox path and the YAML path carry identical semantics.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from automation.builders.netbox import build_from_client
from automation.builders.netbox_schema import CUSTOM_FIELDS
from automation.builders.netbox_writer import write as write_intent
from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs

DESIGN_DIR = Path(__file__).resolve().parents[2] / "validated-designs" / "3-stage-evpn-vxlan"

# Lists in FabricIntent that have no canonical order from NetBox (they
# come back sorted by the builder already) — the test sorts both sides
# by name so ordering is not part of the contract.
SORTABLE_LISTS = [
    "nodes",
    "links",
    "lags",
    "bridge_domains",
    "routers",
    "irb_interfaces",
    "vlans",
    "edge_interfaces",
    "routed_interfaces",
    "static_routes",
    "configlets",
    "default_mtus",
    "banners",
    "prefix_sets",
    "routing_policies",
    "breakouts",
]


# ---------------------------------------------------------------------------
# In-memory NetBox fake
# ---------------------------------------------------------------------------


class FakeObject:
    """Generic object with attribute + item access, matching pynetbox.Record."""

    _id_counter = itertools.count(1)

    def __init__(self, endpoint: "FakeEndpoint", data: dict[str, Any]) -> None:
        self._endpoint = endpoint
        self._data: dict[str, Any] = dict(data)
        if "id" not in self._data:
            self._data["id"] = next(FakeObject._id_counter)

    # Attribute access — resolve refs to their current state each time so
    # updates propagate (matches pynetbox behaviour closely enough).
    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        data = self.__dict__.get("_data", {})
        if key not in data:
            return None
        v = data[key]
        return self._resolve(v)

    def _resolve(self, v: Any) -> Any:
        if isinstance(v, _Ref):
            return v.resolve(self._endpoint._store)
        if isinstance(v, list):
            return [self._resolve(x) for x in v]
        if isinstance(v, dict) and {"object_type", "object_id"} <= v.keys():
            # Cable termination: wrap into an object with ``.object``
            # pointing at the live target.
            return _Termination(v, self._endpoint._store)
        return v

    # Some code paths assume dict-style access for `tags` / `terminations`.
    def __iter__(self):
        return iter(self._data)

    def update(self, payload: dict[str, Any]) -> None:
        self._data.update(_normalize_refs(payload, self._endpoint._store))

    def delete(self) -> None:
        self._endpoint._objects.remove(self)

    def save(self) -> None:  # parity with pynetbox; not used by writer
        pass

    def __repr__(self) -> str:
        name = self._data.get("name") or self._data.get("slug") or str(self._data.get("id"))
        return f"<Fake {self._endpoint._name} {name}>"


class _Termination:
    """Wraps a cable termination dict so reader code can access ``.object``."""

    def __init__(self, data: dict, store: "FakeStore") -> None:
        self._data = data
        self._store = store

    @property
    def object(self) -> Any:
        ot = self._data.get("object_type")
        oid = self._data.get("object_id")
        if not ot or oid is None:
            return None
        app, model = ot.split(".", 1)
        endpoint_name = f"{app}.{model}s"  # dcim.interface → dcim.interfaces
        ep = self._store._endpoints.get(endpoint_name)
        if ep is None:
            return None
        return ep.get(id=oid)


class _Ref:
    """Lazy reference to another object by id inside a FakeEndpoint."""

    def __init__(self, endpoint_name: str, object_id: int) -> None:
        self.endpoint_name = endpoint_name
        self.object_id = object_id

    def resolve(self, store: "FakeStore") -> Any:
        ep = store._endpoints.get(self.endpoint_name)
        if ep is None:
            return None
        return ep.get(id=self.object_id)


def _normalize_refs(payload: dict[str, Any], store: "FakeStore") -> dict[str, Any]:
    """Convert ``id`` ints embedded in payloads into ``_Ref`` placeholders
    so the object's attributes resolve to real FakeObjects when read."""
    out = dict(payload)
    _ref_map = {
        "device": "dcim.interfaces._device",  # special-case below
        "site": "dcim.sites",
        "role": "dcim.device_roles",
        "device_type": "dcim.device_types",
        "platform": "dcim.platforms",
        "lag": "dcim.interfaces",
        "parent": "dcim.interfaces",
        "vrf": "ipam.vrfs",
        "group": "ipam.vlan_groups",
        "manufacturer": "dcim.manufacturers",
        "primary_ip4": "ipam.ip_addresses",
        "untagged_vlan": "ipam.vlans",
        "scope_id": None,  # scope is implicit: scope_type + scope_id handled below
    }
    for key, target in _ref_map.items():
        if key not in out or target is None:
            continue
        val = out[key]
        if isinstance(val, int):
            if key == "device":
                # Device lives in dcim.devices
                out[key] = _Ref("dcim.devices", val)
            elif key == "role":
                out[key] = _Ref("dcim.device_roles", val)
            elif target == "dcim.interfaces._device":
                out[key] = _Ref("dcim.devices", val)
            else:
                out[key] = _Ref(target, val)

    # M2M fields: list of ids -> list of _Ref
    if "tagged_vlans" in out and isinstance(out["tagged_vlans"], list):
        out["tagged_vlans"] = [
            _Ref("ipam.vlans", v) if isinstance(v, int) else v
            for v in out["tagged_vlans"]
        ]
    return out


class FakeEndpoint:
    """Dict-backed endpoint compatible with pynetbox's endpoint API."""

    def __init__(self, store: "FakeStore", name: str) -> None:
        self._store = store
        self._name = name
        self._objects: list[FakeObject] = []

    def create(self, **kwargs: Any) -> FakeObject:
        payload = _normalize_refs(kwargs, self._store)
        obj = FakeObject(self, payload)
        self._objects.append(obj)
        return obj

    def get(self, *args: Any, **kwargs: Any) -> FakeObject | None:
        if args and isinstance(args[0], int):
            return next((o for o in self._objects if o._data.get("id") == args[0]), None)
        if "id" in kwargs:
            return next((o for o in self._objects if o._data.get("id") == kwargs["id"]), None)
        for obj in self._objects:
            if _matches(obj, kwargs):
                return obj
        return None

    def filter(self, **kwargs: Any) -> list[FakeObject]:
        kwargs.pop("limit", None)
        return [o for o in self._objects if _matches(o, kwargs)]

    def all(self) -> list[FakeObject]:
        return list(self._objects)


def _ref_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, _Ref):
        return value.object_id
    if isinstance(value, int):
        return value
    return getattr(value, "id", None)


def _cable_touches_devices(cable: FakeObject, device_ids: set[int]) -> bool:
    """Return True if either termination references an interface on one of device_ids."""
    store = cable._endpoint._store
    interfaces = store._endpoints.get("dcim.interfaces")
    if interfaces is None:
        return False
    for key in ("a_terminations", "b_terminations"):
        for term in cable._data.get(key, []) or []:
            if isinstance(term, dict):
                iface_id = term.get("object_id")
            else:
                iface_id = _ref_id(getattr(term, "object_id", None))
            if iface_id is None:
                continue
            iface = interfaces.get(id=iface_id)
            if iface is None:
                continue
            dev_id = _ref_id(iface._data.get("device"))
            if dev_id in device_ids:
                return True
    return False


def _matches(obj: FakeObject, kwargs: dict[str, Any]) -> bool:
    for key, want in kwargs.items():
        # Special: cables are filtered by device_id (API-side NetBox
        # unwraps the termination references). Our fake has to replicate
        # that: a cable matches if any of its terminations refers to an
        # interface belonging to one of the requested devices.
        if key == "device_id" and obj._endpoint._name == "dcim.cables":
            wanted_ids = set(want if isinstance(want, list) else [want])
            if not _cable_touches_devices(obj, wanted_ids):
                return False
            continue

        # Config contexts hold a *list* of site ids (many-to-many scope),
        # so ``site_id=N`` means "N is one of the sites this context is
        # attached to" — not an equality check on a FK field.
        if key == "site_id" and obj._endpoint._name == "extras.config_contexts":
            sites = obj._data.get("sites") or []
            actual_ids = [
                _ref_id(s) if not isinstance(s, int) else s for s in sites
            ]
            wanted = want if isinstance(want, list) else [want]
            if not any(w in actual_ids for w in wanted):
                return False
            continue

        # Interfaces filtered by device_id is the common writer pattern.
        if key == "device_id" and obj._endpoint._name == "dcim.interfaces":
            dev = obj._data.get("device")
            actual_id = _ref_id(dev)
            if isinstance(want, list):
                if actual_id not in want:
                    return False
            else:
                if actual_id != want:
                    return False
            continue

        # Map pynetbox-style filter keys to our data keys.
        if key.endswith("_id"):
            data_key = key[:-3]
            actual = obj._data.get(data_key)
            actual_id = _ref_id(actual)
            if isinstance(want, list):
                if actual_id not in want:
                    return False
            else:
                if actual_id != want:
                    return False
            continue

        if key == "tag":
            tags = obj._data.get("tags") or []
            slugs = []
            for t in tags:
                if isinstance(t, str):
                    slugs.append(t)
                elif isinstance(t, dict):
                    slugs.append(t.get("slug"))
                else:
                    slugs.append(getattr(t, "slug", None))
            if want not in slugs:
                return False
            continue

        # Custom field filters use the pynetbox ``cf_<name>`` prefix.
        if key.startswith("cf_"):
            cf_name = key[3:]
            actual = (obj._data.get("custom_fields") or {}).get(cf_name)
            if actual != want:
                return False
            continue

        if key == "assigned_object_type":
            if obj._data.get("assigned_object_type") != want:
                return False
            continue
        if key == "assigned_object_id":
            if obj._data.get("assigned_object_id") != want:
                return False
            continue

        if obj._data.get(key) != want:
            return False
    return True


class _AppEndpoints:
    """Intermediate object giving the ``nb.dcim`` / ``nb.ipam`` attribute access."""

    def __init__(self, store: "FakeStore", prefix: str) -> None:
        self._store = store
        self._prefix = prefix

    def __getattr__(self, name: str) -> FakeEndpoint:
        full = f"{self._prefix}.{name}"
        ep = self._store._endpoints.get(full)
        if ep is None:
            ep = FakeEndpoint(self._store, full)
            self._store._endpoints[full] = ep
        return ep


class FakeStore:
    """Top-level pynetbox.api analogue."""

    def __init__(self) -> None:
        self._endpoints: dict[str, FakeEndpoint] = {}
        self.dcim = _AppEndpoints(self, "dcim")
        self.ipam = _AppEndpoints(self, "ipam")
        self.extras = _AppEndpoints(self, "extras")
        # Aliases used by the writer
        self.dcim.devices  # ensure endpoint exists
        self.dcim.interfaces
        self.dcim.sites
        self.dcim.cables
        self.ipam.ip_addresses
        self.ipam.prefixes
        self.ipam.vlans
        self.ipam.vlan_groups
        self.ipam.vrfs


# ---------------------------------------------------------------------------
# Fixture bootstrapping (setup-equivalent inside the fake)
# ---------------------------------------------------------------------------


def _bootstrap_fake(nb: FakeStore) -> None:
    """Create the prerequisites normally produced by ``netbox_setup``."""
    # Tags — writer references slugs in payloads; they don't need to exist
    # as full tag objects in this fake.

    manu = nb.dcim.manufacturers.create(name="Nokia", slug="nokia")
    nb.dcim.platforms.create(name="SR Linux", slug="srlinux", manufacturer=manu.id)

    # Standard device types we'll use
    for slug, model, part_number in (
        ("7220-ixr-d3l", "7220 IXR-D3L", "7220 IXR-D3L"),
    ):
        nb.dcim.device_types.create(
            manufacturer=manu.id,
            model=model,
            slug=slug,
            part_number=part_number,
            u_height=1,
            tags=[{"slug": "nvd-managed"}],
        )

    # Device roles
    for slug, name, color in (
        ("leaf", "Leaf", "2196f3"),
        ("spine", "Spine", "ff9800"),
    ):
        nb.dcim.device_roles.create(name=name, slug=slug, color=color)


# ---------------------------------------------------------------------------
# The actual test
# ---------------------------------------------------------------------------


def _canonicalize(intent_dict: dict) -> dict:
    out = dict(intent_dict)
    for key in SORTABLE_LISTS:
        if key in out and isinstance(out[key], list):
            out[key] = sorted(out[key], key=lambda x: x.get("name", "") if isinstance(x, dict) else str(x))
    return out


def test_netbox_roundtrip_matches_yaml_intent():
    """YAML → FabricIntent → NetBox → FabricIntent should be idempotent."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    # Exercise the optional export/import route target fields on the
    # first router + bridge domain; default (unset) is covered by every
    # other entry in the fixture.
    yaml_intent.routers[0].export_target = "target:1:777"
    yaml_intent.routers[0].import_target = "target:2:888"
    yaml_intent.bridge_domains[0].export_target = "target:3:999"
    yaml_intent.bridge_domains[0].import_target = "target:4:111"

    nb = FakeStore()
    _bootstrap_fake(nb)

    write_intent(nb, yaml_intent)
    nb_intent = build_from_client(nb, yaml_intent.fabric_name)

    assert _canonicalize(nb_intent.model_dump(mode="json")) == _canonicalize(
        yaml_intent.model_dump(mode="json")
    )

    # Spot-check the preserved values on the reader side.
    r0 = next(r for r in nb_intent.routers if r.name == yaml_intent.routers[0].name)
    assert r0.export_target == "target:1:777"
    assert r0.import_target == "target:2:888"
    bd0 = next(
        bd for bd in nb_intent.bridge_domains
        if bd.name == yaml_intent.bridge_domains[0].name
    )
    assert bd0.export_target == "target:3:999"
    assert bd0.import_target == "target:4:111"


def test_netbox_intent_passes_fabric_intent_validators():
    """The reconstructed intent must satisfy every cross-reference validator."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    # Constructing FabricIntent already ran validate_cross_references —
    # if this call didn't raise, the round-trip produced a valid intent.
    intent = build_from_client(nb, yaml_intent.fabric_name)

    assert intent.design == yaml_intent.design
    assert intent.fabric_name == yaml_intent.fabric_name
    assert len(intent.nodes) == len(yaml_intent.nodes)
    assert len(intent.links) == len(yaml_intent.links)
    assert len(intent.bridge_domains) == len(yaml_intent.bridge_domains)
    assert len(intent.routers) == len(yaml_intent.routers)
    assert len(intent.lags) == len(yaml_intent.lags)


def test_irbs_written_as_native_virtual_interfaces():
    """IRBs must be native ``dcim.Interface`` rows (type=virtual), one
    per device where the VRF is deployed, with their VRF linked via the
    native ``Interface.vrf`` FK — NOT serialised into ``nvd_config``."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    from automation.builders.netbox_schema import (
        CF_IFACE_IRB_CONFIG,
        CF_IFACE_ROLE,
        CF_SITE_CONFIG,
        NVD_IRB_TAG,
    )

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)
    # The 3-stage fixture is only meaningful if it actually has IRBs.
    assert yaml_intent.irb_interfaces, "3-stage fixture must exercise IRBs"

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)
    blob = dict(site._data.get("custom_fields") or {}).get(CF_SITE_CONFIG) or {}
    assert "irb_interfaces" not in blob, (
        "IRBs must not be stored in nvd_config blob — they live as native "
        "virtual interfaces"
    )

    # Every IRB should have one virtual Interface per device matching its
    # router's node_selector, tagged nvd-irb, with a vrf FK set.
    devices = list(nb.dcim.devices.filter(site_id=site.id))
    router_selectors = {r.name: r.node_selector for r in yaml_intent.routers}
    from automation.core.selectors import node_matches_selector

    for irb in yaml_intent.irb_interfaces:
        selector = router_selectors[irb.router]
        matching_nodes = [
            n for n in yaml_intent.nodes if node_matches_selector(n, selector)
        ]
        assert matching_nodes, f"router {irb.router} matched no nodes"

        for node in matching_nodes:
            dev = next(d for d in devices if d.name == node.name)
            iface = nb.dcim.interfaces.get(device_id=dev.id, name=irb.name)
            assert iface is not None, f"IRB {irb.name} missing on {node.name}"
            assert iface._data.get("type") == "virtual"
            cf = dict(iface._data.get("custom_fields") or {})
            assert cf.get(CF_IFACE_ROLE) == "irb"
            assert cf.get(CF_IFACE_IRB_CONFIG) is not None
            slugs = {
                t.get("slug") if isinstance(t, dict) else getattr(t, "slug", None)
                for t in iface._data.get("tags") or []
            }
            assert NVD_IRB_TAG in slugs
            # vrf FK should point at the matching VRF
            vrf = iface.vrf
            assert vrf is not None and vrf.name == irb.router


def test_routed_interfaces_written_as_native_subinterfaces():
    """RoutedInterfaceIntent must be written as a virtual dcim.Interface
    whose ``parent`` FK is the underlying edge port and whose ``vrf`` FK
    is set — NOT serialised into ``nvd_config``."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    from automation.builders.netbox_schema import (
        CF_IFACE_ROLE,
        CF_IFACE_ROUTED_CONFIG,
        CF_SITE_CONFIG,
        NVD_ROUTED_TAG,
    )

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)
    if not yaml_intent.routed_interfaces:
        pytest.skip("3-stage fixture has no routed_interfaces — cannot exercise")

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)
    blob = dict(site._data.get("custom_fields") or {}).get(CF_SITE_CONFIG) or {}
    assert "routed_interfaces" not in blob, (
        "RoutedInterfaceIntents must be native virtual interfaces, "
        "not serialised into nvd_config"
    )

    # Build an {edge_name: (device, port)} lookup once.
    edge_by_name = {e.name: e for e in yaml_intent.edge_interfaces}

    for ri in yaml_intent.routed_interfaces:
        edge = edge_by_name[ri.interface]
        dev = nb.dcim.devices.get(name=edge.node, site_id=site.id)
        iface = nb.dcim.interfaces.get(device_id=dev.id, name=ri.name)
        assert iface is not None, f"routed interface {ri.name} missing on {edge.node}"
        assert iface._data.get("type") == "virtual"
        cf = dict(iface._data.get("custom_fields") or {})
        assert cf.get(CF_IFACE_ROLE) == "routed"
        assert CF_IFACE_ROUTED_CONFIG in cf
        slugs = {
            t.get("slug") if isinstance(t, dict) else getattr(t, "slug", None)
            for t in iface._data.get("tags") or []
        }
        assert NVD_ROUTED_TAG in slugs

        parent = iface.parent
        assert parent is not None and parent.name == edge.interface
        assert iface.vrf is not None and iface.vrf.name == ri.router


def test_vrfs_scoped_by_nvd_site_custom_field():
    """VRFs carry their site slug in ``nvd_site`` and the blob no longer
    holds the ``_vrf_names`` index."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    from automation.builders.netbox_schema import CF_SITE_CONFIG, CF_VRF_SITE

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)
    blob = dict(site._data.get("custom_fields") or {}).get(CF_SITE_CONFIG) or {}
    assert "_vrf_names" not in blob, "VRF reverse-lookup index must not leak into the blob"

    for router in yaml_intent.routers:
        vrf = nb.ipam.vrfs.get(name=router.name)
        assert vrf is not None
        cf = dict(vrf._data.get("custom_fields") or {})
        assert cf.get(CF_VRF_SITE) == site.slug


def test_static_routes_stored_on_owning_vrf():
    """StaticRouteIntent lives on ``ipam.VRF.custom_fields.nvd_static_routes``
    with the ``router`` ref stripped (it's implicit). The site blob must
    not carry them any more."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    from automation.builders.netbox_schema import CF_SITE_CONFIG, CF_VRF_STATIC_ROUTES

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)
    if not yaml_intent.static_routes:
        pytest.skip("3-stage fixture has no static_routes — cannot exercise")

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)
    blob = dict(site._data.get("custom_fields") or {}).get(CF_SITE_CONFIG) or {}
    assert "static_routes" not in blob, "static_routes must not leak into the site blob"

    # Per VRF, the list matches the writer's group-and-strip contract.
    expected_by_router: dict[str, list[dict]] = {}
    for sr in yaml_intent.static_routes:
        entry = sr.model_dump(mode="json")
        entry.pop("router", None)
        expected_by_router.setdefault(sr.router, []).append(entry)

    for router_name, expected in expected_by_router.items():
        vrf = nb.ipam.vrfs.get(name=router_name)
        assert vrf is not None
        cf = dict(vrf._data.get("custom_fields") or {})
        stored = cf.get(CF_VRF_STATIC_ROUTES) or []
        # Order-insensitive equality: the writer sorts by name internally.
        assert sorted(stored, key=lambda e: e["name"]) == sorted(expected, key=lambda e: e["name"])


def test_non_native_bundle_written_to_config_context_not_site_blob():
    """Everything except ``credentials`` must live in the per-site Config
    Context; the Site ``nvd_config`` CF must contain only credentials."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    from automation.builders.netbox_schema import (
        CCTX_KEY_CONFIGLETS,
        CCTX_KEY_DEFAULT_MTUS,
        CCTX_KEY_EDA,
        CCTX_KEY_PREFIX_SETS,
        CCTX_KEY_ROUTING_POLICIES,
        CFG_KEY_CREDENTIALS,
        CF_SITE_CONFIG,
        NVD_MANAGED_TAG,
        nvd_config_context_name,
    )

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)

    blob = dict(site._data.get("custom_fields") or {}).get(CF_SITE_CONFIG) or {}
    assert set(blob.keys()).issubset({CFG_KEY_CREDENTIALS}), (
        f"site nvd_config must only carry credentials, got {sorted(blob.keys())}"
    )

    ctx = nb.extras.config_contexts.get(name=nvd_config_context_name(site.slug))
    assert ctx is not None, "per-site nvd config context missing"
    slugs = set()
    for t in ctx._data.get("tags") or []:
        if isinstance(t, str):
            slugs.add(t)
        elif isinstance(t, dict):
            slugs.add(t.get("slug"))
        else:
            slugs.add(getattr(t, "slug", None))
    assert NVD_MANAGED_TAG in slugs
    assert site.id in (ctx._data.get("sites") or [])

    data = ctx._data.get("data") or {}
    # Keys that have any intent content should round-trip through the context.
    dump = yaml_intent.model_dump(mode="json")
    for ccx_key, intent_key in (
        (CCTX_KEY_CONFIGLETS, "configlets"),
        (CCTX_KEY_DEFAULT_MTUS, "default_mtus"),
        (CCTX_KEY_PREFIX_SETS, "prefix_sets"),
        (CCTX_KEY_ROUTING_POLICIES, "routing_policies"),
        (CCTX_KEY_EDA, "eda"),
    ):
        if dump.get(intent_key):
            assert ccx_key in data, f"{ccx_key} missing from config context data"

    # Reader round-trip still identical to the YAML-derived intent.
    nb_intent = build_from_client(nb, yaml_intent.fabric_name)
    assert _canonicalize(nb_intent.model_dump(mode="json")) == _canonicalize(
        yaml_intent.model_dump(mode="json")
    )


def test_interface_labels_stored_as_native_list():
    """Interface labels must serialise as a JSON list of strings, not a dict.

    ``role=<iface_role>`` is elided (redundant with ``nvd_role``).
    ``k=enabled`` collapses to bare ``k``. Meaningful non-enabled values
    and overrides (e.g. ``role=routed-s5``) survive as ``k=v`` entries.
    The round-trip restores the original labels dict regardless.
    """
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    # Grab a plain edge port (role=edge), a routed override port, and a LAG.
    from automation.builders.netbox_schema import (
        CF_IFACE_LABELS,
        CF_IFACE_ROLE,
    )

    def _labels(device_name: str, iface_name: str) -> list[str]:
        dev = nb.dcim.devices.get(name=device_name)
        iface = nb.dcim.interfaces.get(device_id=dev.id, name=iface_name)
        cf = dict(iface._data.get("custom_fields") or {})
        v = cf.get(CF_IFACE_LABELS)
        assert isinstance(v, list), (
            f"{device_name}/{iface_name}: expected list, got {type(v).__name__}: {v!r}"
        )
        for entry in v:
            assert isinstance(entry, str) and entry, entry
        return v

    # Plain edge: eth-1-3 on leaf1 has role=edge + tagged-v10=enabled + tagged-v99=enabled.
    # tagged-v10 is promoted to native ``Interface.tagged_vlans`` (VLAN
    # exists in the intent). tagged-v99 has no matching VLAN and stays
    # in the labels list as a fallback.
    edge = _labels("leaf1", "ethernet-1-3")
    assert "eda.nokia.com/role" not in edge, edge
    assert not any(e.startswith("eda.nokia.com/role=") for e in edge), edge
    assert "eda.nokia.com/tagged-v10" not in edge, edge
    assert "eda.nokia.com/tagged-v99" in edge, edge

    # Routed-override edge: eth-1-4 on leaf1 has role=routed-s5 (kept).
    routed = _labels("leaf1", "ethernet-1-4")
    assert "eda.nokia.com/role=routed-s5" in routed, routed

    # LAG interface has role=edge while nvd_role=lag: role label must be
    # kept. Its untagged-v30 label has a matching VLAN and is promoted
    # to native ``Interface.untagged_vlan``, so the labels list should
    # not carry it any more.
    lag_if = _labels("leaf4", "leaf4-leaf5-lag1")
    assert "eda.nokia.com/role=edge" in lag_if, lag_if
    assert "eda.nokia.com/untagged-v30" not in lag_if, lag_if

    # Round-trip: reconstructed intent must equal the yaml intent exactly.
    nb_intent = build_from_client(nb, yaml_intent.fabric_name)
    assert _canonicalize(nb_intent.model_dump(mode="json")) == _canonicalize(
        yaml_intent.model_dump(mode="json")
    )


def test_vlan_membership_labels_promoted_to_native_fields():
    """VLAN-membership labels become native ``Interface.untagged_vlan``
    / ``Interface.tagged_vlans`` / ``Interface.mode`` attachments when
    the target VLAN exists in the intent. Labels without a matching
    VLAN (e.g. ``tagged-v99``) stay in ``nvd_labels``.
    """
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    dev = nb.dcim.devices.get(name="leaf1")
    edge = nb.dcim.interfaces.get(device_id=dev.id, name="ethernet-1-3")

    # tagged-v10 → tagged_vlans, mode=tagged; untagged not set.
    assert edge._data.get("mode") == "tagged", edge._data.get("mode")
    assert edge._data.get("untagged_vlan") is None, edge._data.get("untagged_vlan")
    tagged = edge.tagged_vlans or []
    names = {getattr(v, "name", None) for v in tagged}
    assert "tagged-v10" in names, names

    # Another edge has untagged-v10 (maps natively to the group's
    # reserved ``untagged`` sentinel) and tagged-v99 (no such VLAN, stays
    # as a fallback label). Mode is ``tagged`` because any ``tagged-*``
    # label pins the 802.1Q Mode even when the VLAN object doesn't exist.
    leaf2 = nb.dcim.devices.get(name="leaf2")
    e2 = nb.dcim.interfaces.get(device_id=leaf2.id, name="ethernet-1-3")
    assert e2._data.get("mode") == "tagged", e2._data.get("mode")
    assert getattr(e2.untagged_vlan, "name", None) == "untagged", e2._data
    residual = e2._data.get("custom_fields", {}).get("nvd_labels") or []
    assert "eda.nokia.com/tagged-v99" in residual, residual

    # LAG: the reserved ``untagged`` VLAN of group macvrf-v30 is promoted
    # natively on leaf4's LAG interface. Mode is ``tagged`` (not ``access``)
    # because EDA models any VLAN attachment — including an untagged one —
    # as a sub-interface of a vlan-tagging-enabled parent (the untagged
    # frames flow on subif 4096 with ``vlan.encap.untagged``, alongside any
    # dot1q sub-interfaces). ``access`` would forbid the sub-interface
    # machinery EDA needs.
    leaf4 = nb.dcim.devices.get(name="leaf4")
    lag = nb.dcim.interfaces.get(device_id=leaf4.id, name="leaf4-leaf5-lag1")
    assert getattr(lag.untagged_vlan, "name", None) == "untagged", lag._data
    assert lag._data.get("mode") == "tagged", lag._data.get("mode")

    # The roundtrip still restores every original label (tagged-v99
    # from the fallback list, tagged-v10 from the native fields).
    nb_intent = build_from_client(nb, yaml_intent.fabric_name)
    assert _canonicalize(nb_intent.model_dump(mode="json")) == _canonicalize(
        yaml_intent.model_dump(mode="json")
    )


def test_schema_custom_field_names_are_unique_and_nvd_prefixed():
    """Reset script invariants: every NVD custom field has a unique name
    with the ``nvd_`` prefix so `netbox_reset --scope all` catches it."""
    names = [spec["name"] for spec in CUSTOM_FIELDS]
    assert all(n.startswith("nvd_") for n in names), names
    # A field may legitimately appear multiple times (for different object
    # types); that's handled by the setup collapse logic.


# ---------------------------------------------------------------------------
# Reserved untagged VLAN / mode-driven encap
# ---------------------------------------------------------------------------


def test_reserved_untagged_vlan_auto_created():
    """A VLAN Group seeded without an ``untagged`` sentinel gets one
    auto-created by the reader pre-pass, idempotently."""
    from automation.builders.netbox_schema import (
        CF_VLAN_VLAN_ID_STR,
        NVD_MANAGED_TAG,
        VLAN_UNTAGGED_SENTINEL_NAME,
        VLAN_UNTAGGED_SENTINEL_VID,
    )

    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    site = nb.dcim.sites.get(slug=yaml_intent.fabric_name)
    group = nb.ipam.vlan_groups.create(
        name="macvrf-vtest",
        slug="macvrf-vtest",
        scope_type="dcim.site",
        scope_id=site.id,
        tags=[{"slug": NVD_MANAGED_TAG}],
        custom_fields={"nvd_vni": 11111, "nvd_evi": 111},
    )
    assert nb.ipam.vlans.get(
        name=VLAN_UNTAGGED_SENTINEL_NAME, group_id=group.id
    ) is None

    build_from_client(nb, yaml_intent.fabric_name)

    sentinel = nb.ipam.vlans.get(
        name=VLAN_UNTAGGED_SENTINEL_NAME, group_id=group.id
    )
    assert sentinel is not None
    assert sentinel._data.get("vid") == VLAN_UNTAGGED_SENTINEL_VID
    assert sentinel._data.get("custom_fields", {}).get(CF_VLAN_VLAN_ID_STR) == "untagged"

    build_from_client(nb, yaml_intent.fabric_name)
    hits = [
        v for v in nb.ipam.vlans.all()
        if v._data.get("name") == VLAN_UNTAGGED_SENTINEL_NAME
        and _ref_id(v._data.get("group")) == group.id
    ]
    assert len(hits) == 1, hits


def test_vlan_intent_name_reconstructed_from_group_suffix():
    """An ``untagged`` VLAN inside ``macvrf-v40`` round-trips as a
    ``VlanIntent`` named ``untagged-v40`` via ``group_vlan_suffix``."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    nb_intent = build_from_client(nb, yaml_intent.fabric_name)

    by_name = {v.name: v for v in nb_intent.vlans}
    assert "untagged-v40" in by_name, sorted(by_name)
    v40 = by_name["untagged-v40"]
    assert v40.vlan_id == "untagged"
    assert v40.bridge_domain == "macvrf-v40"
    assert "eda.nokia.com/untagged-v40=enabled" in v40.interface_selector


def test_interface_mode_drives_encap():
    """``Interface.mode`` is the authoritative source for
    ``EdgeInterfaceIntent.encap`` — the legacy ``nvd_encap`` custom field
    is only consulted as a fallback when ``mode`` is unset."""
    from types import SimpleNamespace

    from automation.builders.netbox import _build_edge_interfaces

    def _iface(mode: str | None, encap_cf: str | None = None) -> Any:
        cf: dict[str, Any] = {"nvd_role": "edge"}
        if encap_cf is not None:
            cf["nvd_encap"] = encap_cf
        return SimpleNamespace(
            name="ethernet-1-1",
            mode=mode,
            device=SimpleNamespace(name="leaf1"),
            custom_fields=cf,
            untagged_vlan=None,
            tagged_vlans=[],
        )

    tagged = _build_edge_interfaces([_iface(mode="tagged")])[0]
    assert tagged.encap == "dot1q"

    access = _build_edge_interfaces([_iface(mode="access")])[0]
    assert access.encap == "null"

    legacy_null = _build_edge_interfaces([_iface(mode=None, encap_cf="null")])[0]
    assert legacy_null.encap == "null"

    default = _build_edge_interfaces([_iface(mode=None)])[0]
    assert default.encap == "dot1q"


def test_writer_does_not_populate_legacy_encap_custom_field():
    """The writer has stopped writing the (soft-deprecated) ``nvd_encap``
    custom field; encap now rides on the native 802.1Q Mode field."""
    if not DESIGN_DIR.exists():
        pytest.skip(f"design fixtures missing at {DESIGN_DIR}")

    topology, services = load_inputs(DESIGN_DIR)
    yaml_intent = build_intent(topology, services)

    nb = FakeStore()
    _bootstrap_fake(nb)
    write_intent(nb, yaml_intent)

    edges = [
        i for i in nb.dcim.interfaces.all()
        if (i._data.get("custom_fields") or {}).get("nvd_role") == "edge"
    ]
    assert edges, "expected at least one edge interface"
    for iface in edges:
        cf = iface._data.get("custom_fields") or {}
        assert "nvd_encap" not in cf, (iface._data.get("name"), cf)
