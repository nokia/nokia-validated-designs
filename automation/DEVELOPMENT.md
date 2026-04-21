# Development Guide

How to extend and maintain the NVD automation engine. Covers four scenarios:
upgrading EDA, adding new resource types, supporting new SR Linux versions,
and working with the unit test suite.

---

## 1. New EDA Version (API Changes)

When EDA releases a new version, API groups may graduate (e.g.,
`v1alpha1` to `v1`), endpoints may change, or new fields may appear in
existing CR specs.

### 1a. Update the CR type registry

**File:** `automation/eda_models/registry.py`

The `CRType` constants are the single source of truth for every
`apiVersion`, `kind`, and plural name. When an API group graduates:

```python
# Before (v1alpha1)
INTERFACE = CRType("interfaces.eda.nokia.com/v1alpha1", "Interface", "interfaces")

# After (v1)
INTERFACE = CRType("interfaces.eda.nokia.com/v1", "Interface", "interfaces")
```

Also update `EDA_VERSION` at the top of the file and add any newly
supported SR Linux versions to `SUPPORTED_SRL_VERSIONS`.

All generated CRs, executor queries, and destroy ordering flow through
these constants, so changing them here propagates everywhere.

### 1b. Regenerate Pydantic models from OpenAPI specs

The `automation/eda_models/` Pydantic models are auto-generated from EDA's
OpenAPI v3 specs using a code generator. When CR specs gain new fields or
change structure:

1. Download the updated OpenAPI specs from the new EDA instance:
   ```
   GET /openapi/v3/apis/{group}/{version}
   ```
   Save them to `automation/codegen/openapi_specs/`, replacing the existing
   files. The naming convention is `{group_with_underscores}_{version}.json`,
   e.g., `services_eda_nokia_com_v1.json`.

2. If a new API group was added, register it in `RESOURCE_MAP` inside
   `automation/codegen/generate_models.py`:
   ```python
   RESOURCE_MAP = {
       # existing entries...
       "security_eda_nokia_com_v1.json": (
           "security",
           ["ACL", "ACLFilter"],
       ),
   }
   ```

3. Run the code generator:
   ```bash
   python -m automation.codegen.generate_models
   ```
   This overwrites `automation/eda_models/{group}.py` files. Do not edit
   these generated files by hand.

4. If an API group graduated (e.g., `v1alpha1` to `v1`), the old spec
   file can be removed from `openapi_specs/` and the old entry removed from
   `RESOURCE_MAP`. The new spec file replaces it.

#### Known codegen drift — re-apply after regen

The generator collapses inline sub-schemas with the same property name
into a single class, and only keeps the fields from one side. When a
schema appears in two places with different fields (e.g. `underlayProtocol.bgp`
vs `overlayProtocol.bgp`), fields unique to one side are silently dropped
from the emitted model — and Pydantic's default `extra="ignore"` then drops
them again at construction time, so the bug is invisible at build time and
only surfaces as a missing field in the deployed CR.

Currently patched by hand (search for `MANUAL PATCH` in `eda_models/`):

- `FabricBgp.asn_pool` (`asnPool`) — present on underlay.bgp only; dropping
  it leaves `underlayProtocol.bgp: {}` on the Fabric CR, which stops EDA's
  reconciler from generating the derived eBGP underlay policies and leaves
  the fabric with BGP sessions up but zero routes exchanged.

After every codegen run, grep for `MANUAL PATCH` and re-apply. The real
fix is a generator change that merges inline sub-schemas by full JSON
pointer rather than by leaf property name.

### 1c. Update the executor if endpoint paths changed

**File:** `automation/executors/eda.py`

The executor builds REST URLs from `CRType` metadata:
```python
url = f"{self.url}/apps/{crt.api_version}/namespaces/{namespace}/{crt.plural}"
```

This pattern handles API version changes automatically via the registry.
If EDA changes its URL structure beyond this pattern (unlikely but
possible), update the URL construction in:

- `get_managed_resources()` — queries all managed CRs by label
- `_get_reference_node_profile()` — fetches a specific NodeProfile
- `_get_existing_toponode_names()` — lists existing TopoNodes
- `submit_transaction()` — posts to `/core/transaction/v2`
- `poll_transaction()` — polls `/core/transaction/v2/result/summary/{id}`

If the Keycloak authentication flow changes, update `authenticate()` and
`_discover_client_secret()`.

### 1d. Update phase mappings if new phases are introduced

The executor splits CRs into three deployment phases via `PHASE_KINDS`
and destroys them in reverse via `DESTROY_PHASE_KINDS` and
`DESTROY_ORDER`. If EDA introduces new dependency ordering requirements,
update these mappings.

### 1e. Run tests

```bash
python -m pytest tests/unit/ -v
```

The registry tests (`test_registry.py`) verify structural invariants
(all types have valid apiVersion format, no duplicate kinds, plurals are
lowercase). The EDA generator tests (`test_eda_generator.py`) verify that
all generated CRs use apiVersions from the registry and have the required
metadata fields.

---

## 2. Adding a New Resource Type (e.g., ACLs)

Adding a new EDA resource type requires changes across 5 layers: models,
schemas, design builders, EDA generator, and Ansible generator. Here is
the complete checklist using ACLs as an example.

### 2a. Add the intent model

**File:** `automation/core/models.py`

Define a Pydantic model for the new resource's design-agnostic intent:

```python
class AclIntent(BaseModel):
    """An ACL policy applied to interfaces."""
    name: str
    type: str = "ipv4"  # "ipv4" | "ipv6" | "mac"
    entries: list[dict] = Field(default_factory=list)
    interface_selector: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
```

Add the new field to `FabricIntent`:

```python
class FabricIntent(BaseModel):
    # ... existing fields ...
    acls: list[AclIntent] = Field(default_factory=list)
```

If ACLs reference other resources by name (e.g., a router), add
cross-reference validation in `validate_cross_references()`.

### 2b. Add the JSON schema

**Files:** `validated-designs/<design>/schemas/topology_schema.json` or
`services_schema.json`

Add a `$defs` entry for the new resource and reference it from the
appropriate top-level property (or from `extras`):

```json
"acls": {
  "type": "array",
  "items": { "$ref": "#/$defs/acl" }
}
```

### 2c. Update the design builders

**Files:** `automation/designs/three_stage_evpn_vxlan.py` and/or
`automation/designs/unconstrained_3_stage.py`

If the new resource maps 1:1 from input dict to intent model, add the
builder to `automation/designs/_common_builders.py` so both designs can
share it (edge interfaces, lags, routers, vlans, routed interfaces,
static routes, default MTUs, and banners live there today). Keep
design-specific variants (e.g. ones that enrich from topology defaults
or generate synthetic entries) in the per-design module.

Add a `_build_acls()` function that maps raw input dicts to
`AclIntent` objects, and call it from `build()`:

```python
acls = _build_acls(services.get("acls", []))
```

Pass the result to the `FabricIntent` constructor. If the resource
supports extras merging, add a merge function (or use `merge_by_name`
from `core/extras.py`).

### 2d. Register the CR type

**File:** `automation/eda_models/registry.py`

Add a new `CRType` constant:

```python
ACL = CRType("security.eda.nokia.com/v1", "ACL", "acls")
```

Add it to `ALL_TYPES`. Import it in `automation/executors/eda.py`.

### 2e. Add or generate the EDA Pydantic spec model

If the OpenAPI spec for the new API group exists, add it to
`codegen/openapi_specs/` and `codegen/generate_models.py`'s
`RESOURCE_MAP`, then run the code generator.

If the spec is not yet available, you can manually write a model in
`automation/eda_models/` following the `_EDABase` pattern with
camelCase aliases.

### 2f. Add the CR builder

**File:** `automation/generators/eda_generator.py`

Add a `_cr_acl()` function that converts `AclIntent` to a CR dict:

```python
def _cr_acl(acl: AclIntent, ns: str, design: str) -> dict:
    spec = AclSpec(type=acl.type, entries=acl.entries, ...)
    return _wrap_cr(CR_ACL.api_version, CR_ACL.kind, acl.name, ns, spec, origin=design)
```

Call it from `generate()` in the correct dependency position.

### 2g. Update phase mappings

**File:** `automation/executors/eda.py`

Add `"ACL"` to the appropriate phase in `PHASE_KINDS` (likely
`PHASE_SERVICES`), `DESTROY_PHASE_KINDS`, and insert `ACL` into
`DESTROY_ORDER` at the correct dependency position.

### 2h. Add Ansible support

**File:** `automation/generators/ansible_generator.py`

If ACLs are per-node, add resolution logic (similar to
`_resolve_configlets()` for label selector matching) and include the
result in host_vars.

**File:** `automation/generators/ansible_filter_plugins/srl_builders/default.py`

Add a `build_acl_updates()` function that produces the SR Linux JSON-RPC
paths and values. Call it from the appropriate phase entry point (likely
`build_services_updates()`). If the ACL JSON-RPC schema differs across
SR Linux versions, override the function in `v25_3.py` and/or `v26.py`.

### 2i. Add tests

- **Builder test:** In `tests/unit/test_builder.py`, verify the new
  field appears on the intent.
- **EDA generator test:** In `tests/unit/test_eda_generator.py`, verify
  the new kind appears in generated CRs with correct apiVersion.
- **Ansible generator test:** In `tests/unit/test_ansible_generator.py`,
  verify the new resource appears in host_vars and generates correct
  payloads.
- **Schema test:** The existing `test_schema_validator.py` tests will
  catch schema regressions automatically when the input files are
  updated to include the new resource.

### Summary of files touched

| File | Change |
|------|--------|
| `core/models.py` | New intent model + FabricIntent field |
| `schemas/*_schema.json` | New $def + property |
| `designs/*.py` | Builder function + extras merge |
| `eda_models/registry.py` | New CRType + ALL_TYPES |
| `eda_models/{group}.py` | Spec model (generated or manual) |
| `generators/eda_generator.py` | CR builder + generate() call |
| `executors/eda.py` | Phase/destroy mappings |
| `generators/ansible_generator.py` | host_vars integration |
| `generators/ansible_filter_plugins/srl_builders/default.py` | JSON-RPC builder |
| `tests/unit/test_*.py` | Coverage for the new resource |

---

## 3. Supporting a New SR Linux Version

A new SR Linux version may introduce YANG model changes that affect the
JSON-RPC payloads the Ansible generator produces, or it may need to be
registered as compatible with the current EDA release.

### 3a. Register the version as EDA-compatible

**File:** `automation/eda_models/registry.py`

Add the version string to `SUPPORTED_SRL_VERSIONS`:

```python
SUPPORTED_SRL_VERSIONS: list[str] = [
    # ... existing versions ...
    "26.3.1",
]
```

The `check_srl_version()` function gates deployment — if a version is
not in this list, `deploy.py` refuses to generate EDA CRs.

### 3b. Identify YANG model breaking changes

Compare the SR Linux YANG models between the old and new version.
Breaking changes that affect this engine typically fall into:

- **Path restructuring** — a leaf moves into a new container (e.g.,
  `multipath/maximum-paths` became `multipath/ebgp/maximum-paths` in
  25.3)
- **Leaf renames** — a field name changes in the YANG model
- **Leaf removal** — a field is removed or made implicit (e.g.,
  `advertise-arp-nd-only-with-mac-table-entry` removed in 26.3)
- **New required fields** — a new mandatory leaf that must be set

### 3c. Create a version-specific srl_builder (if needed)

**Directory:** `automation/generators/ansible_filter_plugins/srl_builders/`

The version dispatch in `__init__.py` resolves builders by trying
exact match, then minor, then major, then default:

```
26.3.1 → try v26_3_1, v26_3, v26, default
```

If the new version introduces breaking changes, create a new builder
module that inherits from the closest parent and overrides only the
affected functions:

```python
# srl_builders/v27.py
"""SR Linux payload builder for 27.x releases.

Breaking changes vs 26.x
~~~~~~~~~~~~~~~~~~~~~~~~~
- (document each change here)
"""
from . import v26 as _parent

# Re-export everything unchanged
build_interface_updates = _parent.build_interface_updates
build_subinterface_updates = _parent.build_subinterface_updates
build_bfd_updates = _parent.build_bfd_updates
build_tunnel_interface_updates = _parent.build_tunnel_interface_updates
build_system_updates = _parent.build_system_updates
build_prune_deletes = _parent.build_prune_deletes

# Override only what changed
def build_network_instance_updates(hv: dict, scope: str = "all"):
    # ... new implementation ...
```

The inheritance chain is:

```
default.py (24.10.x base)
  └── v25_3.py (overrides: multipath schema, prefix-set nesting)
        └── v26.py (overrides: local-pref format, mac-ip leaf removal)
              └── v27.py (your new overrides)
```

Each builder must expose the four phase entry points:
- `build_topology_updates(hv)`
- `build_fabric_updates(hv)`
- `build_services_updates(hv)`
- `build_prune_deletes(hv, device_state)`

### 3d. If no breaking changes

If the new version has no breaking changes to the JSON-RPC paths used
by this engine, no new builder is needed. The version dispatch will fall
through to the closest matching builder (e.g., `27.3.1` resolves to
`v26` if no `v27` exists, which resolves to `v25_3` for inherited
functions, etc.).

Just add the version to `SUPPORTED_SRL_VERSIONS` and verify with tests.

### 3e. EDA-side considerations

For EDA mode, SR Linux version differences are handled by EDA's intent
engine, not by this automation. The `NodeProfile` CR specifies the
version, and EDA translates intent to device config appropriately. The
only version-sensitive code in the EDA path is:

- `_cr_node_profile()` in `eda_generator.py` — sets the `version` field
- `_enrich_node_profiles()` in `eda.py` — copies `yang`,
  `versionMatch`, `versionPath`, and `llmDb` from the EDA-managed
  reference profile `srlinux-ghcr-{version}`

### 3f. Test the new version

```bash
# Unit tests
python -m pytest tests/unit/ -v

# Generate and inspect Ansible output — verify JSON-RPC paths are correct
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode ansible

# Inspect the generated host_vars and srl_builder output manually,
# or deploy to a containerlab with the new SR Linux version and verify
# the config is applied correctly.
```

---

## 4. Unit Test Structure

### Test suite layout

```
tests/
  conftest.py                    # Shared fixtures (e.g., clab topology loader)
  unit/
    test_builder.py              # Design builders: topology.yaml --> FabricIntent
    test_eda_generator.py        # EDA generator: FabricIntent --> CRs
    test_ansible_generator.py    # Ansible generator: FabricIntent --> project files
    test_registry.py             # CR type registry structural invariants
    test_schema_validator.py     # JSON Schema validation of real input files
    test_selectors.py            # Label selector matching logic
  test_connectivity.py           # Integration: live fabric connectivity checks
  test_mtu_loadbalance.py        # Integration: MTU and load-balancing verification
```

### What each test file covers

**`test_builder.py`** — Tests the design builders in isolation. Loads
the real `topology.yaml` and `services.yaml` from
`validated-designs/`, builds the `FabricIntent`, and verifies:

- Correct design name, fabric name
- Expected node counts, link counts (full-mesh calculation)
- Services (bridge domains, routers, IRBs, VLANs, static routes)
- Edge interfaces, LAGs, configlets, MTU settings
- ASN assignment uniqueness, system0 IP uniqueness
- Extras merge behavior (override existing, append new) for every
  resource type
- Both `3-stage-evpn-vxlan` and `unconstrained-3-stage` designs

**`test_eda_generator.py`** — Tests the EDA CR generator. Generates CRs
from a real intent and verifies:

- All CRs have required fields (apiVersion, kind, metadata.name,
  metadata.namespace, spec)
- `managed-by` label present on every CR
- `nvd-design` label matches `intent.design`
- No duplicate CRs (unique kind:name keys)
- apiVersions match the central registry
- Namespace propagation from `intent.eda.namespace`
- Expected set of CR kinds are present
- JSON output file is written correctly

**`test_ansible_generator.py`** — Tests the Ansible generator at
multiple levels:

- `_jspath_to_jsonrpc()` — path conversion (simple, predicate, slash
  passthrough)
- `_resolve_configlets()` — extras-only filtering, label selector
  matching, endpoint targeting
- `_resolve_default_mtus()` — selector resolution, explicit node
  targeting
- `_resolve_placement()` — leaves get services, spines do not
- `_build_leaf_host_vars()` / `_build_spine_host_vars()` — correct
  node section, underlay interfaces
- Spine overrides — MTU and configlets reach spine nodes
- End-to-end `generate()` — expected files exist, inventory has correct
  structure, host_vars per node, timestamp in playbook, staleness
  warning in README

**`test_registry.py`** — Tests the CR type registry invariants:

- `ALL_TYPES` is populated
- `BY_KIND` is consistent with `ALL_TYPES`
- No duplicate kinds
- apiVersion format is `group/version`
- Plurals are lowercase
- `CRType` instances are immutable (frozen dataclass)
- `check_srl_version()` accepts supported versions, rejects others

**`test_schema_validator.py`** — Tests JSON Schema validation:

- Valid data passes validation
- Invalid data raises `ValidationError`
- Real input files from both designs load and validate successfully
- Missing input files raise `FileNotFoundError`

**`test_selectors.py`** — Tests the label selector matching logic:

- Exact match, no match, OR logic
- Empty selectors, empty labels
- Whitespace handling, selectors without `=`
- `node_matches_selector()` integration

### Running tests

```bash
# All unit tests
python -m pytest tests/unit/ -v

# Specific test file
python -m pytest tests/unit/test_builder.py -v

# Specific test class
python -m pytest tests/unit/test_builder.py::TestExtrasMerge -v

# Specific test
python -m pytest tests/unit/test_builder.py::TestExtrasMerge::test_merge_extras_routers_override -v

# With coverage
python -m pytest tests/unit/ --cov=automation --cov-report=term-missing
```

### Test fixture pattern

All tests that need a `FabricIntent` follow the same pattern: load the
real input files from `validated-designs/`, validate against schemas,
and build via the design builder. This ensures tests exercise the full
pipeline and catch regressions across layers:

```python
DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"

@pytest.fixture()
def three_stage_intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)
```

If the design directory is missing (e.g., in a CI environment without the
full repo), the test is skipped rather than failing.

### When to add tests

- **New resource type** — Add builder tests (expected count, extras
  merge), EDA generator tests (kind present, apiVersion correct),
  Ansible generator tests (appears in host_vars).
- **New SR Linux version** — If a new srl_builder is added, test that
  the overridden functions produce correct paths. The existing
  end-to-end `TestGenerate` tests catch structural regressions.
- **New EDA version** — The registry tests verify invariants
  automatically. Add specific tests if the deployment flow changes.
- **Bug fixes** — Add a test that reproduces the bug before fixing it.

### Integration tests

The `tests/test_connectivity.py` and `tests/test_mtu_loadbalance.py`
files are integration tests that run against a live containerlab
topology. They use the `fcli` MCP tools to query device state (BGP
peers, ARP tables, VXLAN tunnels, etc.) and verify end-to-end fabric
behavior. These are not part of the unit test suite and require a
running lab.
