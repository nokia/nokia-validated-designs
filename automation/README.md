# NVD Automation Engine

Transforms high-level YAML inputs into fully deployed SR Linux DC fabrics
via either **EDA** (Nokia Event-Driven Automation) or **Ansible**.

## Pipeline Overview

```
topology.yaml + services.yaml
        |
        v
+----------------------+
|  1. Schema Validator  |  JSON Schema validation
|  2. Design Builder    |  Design-specific --> design-agnostic intent
+----------+-----------+
           |  FabricIntent (Pydantic model)
           v
+----------------------+
|  3a. EDA Generator    |  --> K8s-style CRs --> EDA Transaction API
|  3b. Ansible Generator|  --> host_vars/group_vars --> srl_builders --> JSON-RPC
|  3c. Clab Generator   |  --> containerlab YAML + client configs
+----------------------+
```

## Directory Structure

```
automation/
  core/
    models.py            # FabricIntent and all sub-models (Pydantic)
    fabric_builder.py    # Design dispatcher — routes to the right builder
    schema_validator.py  # JSON Schema validation for input files
    extras.py            # Generic merge-by-name utility for extras overlays
    selectors.py         # Label selector matching (K8s-style)
    platforms.py         # Platform definitions (port counts, breakout modes)
  designs/
    _common_builders.py        # Shared passthrough builders (edges, lags, routers, vlans, etc.)
    three_stage_evpn_vxlan.py  # Constrained builder — auto-generates from counts
    unconstrained_3_stage.py   # Passthrough builder — explicit node/link input
  generators/
    eda_generator.py     # FabricIntent --> EDA Custom Resources
    ansible_generator.py # FabricIntent --> Ansible project
    clab_generator.py    # FabricIntent --> containerlab topology
    ansible_filter_plugins/
      srl_builders/      # Version-dispatched SR Linux payload builders
        default.py       # Targets 24.10.x
        v25_3.py         # Targets 25.3.x
        v26.py           # Targets 26.x
  executors/
    eda.py               # EDA API client (auth, transactions, polling)
  eda_models/
    registry.py          # CR type registry (apiVersion, kind, plural)
    fabrics.py           # Pydantic models for Fabric CR specs
    services.py          # Pydantic models for service CR specs
    protocols.py         # Pydantic models for protocol CR specs
  schemas/               # Generated JSON Schemas for EDA CR validation
  deploy.py              # CLI entry point
```

## Layer 1: Input Validation

Every design directory contains a `schemas/` folder with JSON Schema files.
`load_inputs()` in `core/schema_validator.py`:

1. Loads `inputs/topology.yaml` and `inputs/services.yaml` as raw dicts
2. Validates each against `schemas/topology_schema.json` and `schemas/services_schema.json`
3. Schemas enforce structure (`additionalProperties: false`), types, enums, and patterns

This catches malformed input before any transformation happens.

## Layer 2: Design Builder

### Dispatch

`fabric_builder.py` reads `topology["design"]`, looks up the module in
`DESIGN_BUILDERS`, and calls `module.build(topology, services)`. Currently
two designs are registered:

| Design | Module | Strategy |
|--------|--------|----------|
| `3-stage-evpn-vxlan` | `three_stage_evpn_vxlan.py` | **Constrained** -- auto-generates nodes, links, ASN, IPs from counts |
| `unconstrained-3-stage` | `unconstrained_3_stage.py` | **Passthrough** -- every node/link/IP is explicit in input |

### Constrained builder (3-stage-evpn-vxlan)

From minimal input (`spines: {count: 2}`, `leafs: {count: 8}`), the builder:

1. **Generates nodes** -- creates `NodeIntent` objects with auto-assigned names
   (`leaf1`..`leafN`, `spine1`..`spineN`), ASNs (leaf_asn_start + i - 1),
   system0 IPs (from the prefix, leafs at offset .11+, spines at .101+), and
   labels (`eda.nokia.com/role=leaf`) for selector-based targeting.

2. **Generates ISL links** -- computes full-mesh leaf-to-spine connectivity with
   port allocation from the highest interface index downward (platform-aware),
   handling breakout ports (e.g., 400G to 4x100G) when configured.

3. **Passes through services** -- bridge domains, routers, IRBs, VLANs, static
   routes come directly from `services.yaml` with minimal transformation.

4. **Generates configlets** -- design-locked device config like BGP-EVPN
   rapid-update and node-isolation event-handlers.

5. **Merges extras** -- the `extras` section in topology/services YAML allows
   users to add/override resources outside the locked design. The
   `merge_by_name()` utility in `core/extras.py` does name-keyed merging with
   `model_copy(update=...)` for existing resources or `model_cls(...)` for new
   ones. Resources from extras get `origin="extras"` for provenance tracking.

### Unconstrained builder

Pure passthrough -- every node, link, IP, and ASN is explicitly defined in the
input YAML. The builder maps dicts to Pydantic models with no auto-generation.

### The output: FabricIntent

Both builders produce a single `FabricIntent` (defined in `core/models.py`),
which is a design-agnostic Pydantic model containing everything needed to fully
describe a fabric:

- `nodes`, `links`, `breakouts` (underlay)
- `edge_interfaces`, `lags` (access)
- `bridge_domains`, `routers`, `irb_interfaces`, `vlans`, `routed_interfaces`,
  `static_routes` (services)
- `configlets`, `default_mtus`, `banners` (siteinfo/overrides)
- `credentials`, `eda` settings, `mgmt_subnet`

On construction, the `validate_cross_references` model validator checks that all
name-based references resolve (e.g., every IRB's `bridge_domain` must exist in
`bridge_domains`).

## Layer 3a: EDA Mode

### CR Generation (eda_generator.py)

Converts `FabricIntent` into an ordered list of EDA Custom Resource dicts --
Kubernetes-style manifests with `apiVersion`, `kind`, `metadata`, `spec`.

`generate()` walks the intent sequentially, producing CRs in dependency order:

| Step | CRs Generated | EDA Kind |
|------|--------------|----------|
| 1 | Commit-save init | `Init` |
| 2 | Device credentials | `NodeUser` |
| 3 | SRL image profile | `NodeProfile` |
| 4 | One per node | `TopoNode` |
| 5 | Two per ISL (each endpoint) | `Interface` (role=interSwitch) |
| 6 | One per edge port | `Interface` (role=edge) |
| 7 | LAG members + LAG aggregates | `Interface` (role=lag) |
| 8 | One per ISL | `TopoLink` |
| 9-10 | ASN/IP pools | `IndexAllocationPool`, `IPAllocationPool` |
| 11 | Fabric definition | `Fabric` |
| 12-18 | Services | `BridgeDomain`, `Router`, `IRBInterface`, `VLAN`, `RoutedInterface`, `StaticRoute` |
| 19-21 | Siteinfo | `Configlet`, `DefaultMTU`, `Banner` |

Each CR is built using `_wrap_cr()` which sets `apiVersion` and `kind` from
the `CRType` registry, sets metadata (name, namespace, managed-by labels), and
serializes the spec from typed Pydantic models using
`model_dump(by_alias=True, exclude_none=True)` for camelCase output.

### EDA Deployment (executors/eda.py)

The `EdaClient.apply()` method executes a three-phase deployment:

**Phase 1 (topology):** Init, NodeProfile, NodeUser, pools, TopoNode, TopoLink,
Interface.

- TopoNodes get special handling: existing nodes are skipped (not replaced)
  because EDA re-applies would trigger full re-onboarding.
- After submission, polls `_wait_for_nodes_sync()` -- loops up to 600s checking
  `status.node-state == "Synced"` on each TopoNode. Downstream phases fail if
  nodes are not fully synced.

**Phase 2 (fabric):** Fabric CR only. Defines underlay protocol (EBGP), overlay
protocol (EBGP), BFD settings, ASN pools, leaf/spine node selectors.

**Phase 3 (services):** All remaining CRs plus any prune deletes. If `--prune`
is active, the diff is computed against live state and stale resources are
included as `delete` operations in the same atomic transaction.

Each phase submits via `POST /core/transaction/v2` with CRs wrapped as `create`
or `replace` operations, then polls for completion.

### Authentication

Uses a two-step Keycloak flow:

1. Get admin token from master realm (public admin-cli client)
2. Discover the `eda-api-server` client secret from the Keycloak admin API
3. Get a scoped token for the EDA API using the discovered secret

Token expiry is tracked with a 30-second safety margin, and all API calls go
through a `_request()` wrapper that retries once on 401 (handles mid-session
expiry).

## Layer 3b: Ansible Mode

The Ansible path must produce device-level configuration itself because there
is no EDA intent engine to translate. This is a two-stage process.

### Stage 1: Intent to host_vars (generation time)

`generate()` in `ansible_generator.py`:

1. **Resolves label selectors to nodes** -- `_resolve_placement()` determines
   which services land on which node by matching `node_selector` labels against
   each node's labels. `_resolve_configlets()` and `_resolve_default_mtus()` do
   the same for configlets and MTU settings. This step freezes the topology
   snapshot, which is why the generated project includes a staleness warning.

2. **Builds per-node host_vars** -- `_build_leaf_host_vars()` and
   `_build_spine_host_vars()` produce YAML dicts with node identity, underlay
   interfaces, edge interfaces, LAGs, bridge domains, routers, and other
   services.

3. **Writes the complete project** -- inventory, group_vars (shared BGP/fabric
   config), host_vars, roles, filter plugins, playbook.

Example leaf host_vars:

```yaml
node:
  hostname: leaf1
  role: leaf
  router_id: 192.0.2.11
  asn: 65411
underlay_interfaces:
  - name: ethernet-1/32
    peer_asn: 65400
edge_interfaces:
  - name: ethernet-1/3
    encap: dot1q
bridge_domains:
  - name: macvrf-v10
    vni: 10010
    evi: 10
```

### Stage 2: host_vars to SR Linux JSON-RPC payloads (playbook runtime)

This happens inside the `srl_builders` filter plugins during Ansible execution:

1. The **detect** role queries each device for its software version via
   `nokia.srlinux.get`.

2. Each phase role calls
   `hostvars[inventory_hostname] | srl_config(sw_version, 'topology')` -- this
   invokes the version-dispatched builder (`srl_builders/__init__.py` resolves
   `v25_3`, `v26`, or `default` based on version).

3. The builder reads the host_vars dict and produces a list of
   `{"path": ..., "value": ..., "op": "update"|"replace"}` entries -- these are
   SR Linux JSON-RPC paths:

   ```python
   {
       "path": "/network-instance[name=macvrf-v10]",
       "value": {
           "type": "mac-vrf",
           "admin-state": "enable",
           "bridge-table": {"mac-learning": {"admin-state": "enable"}},
           "vxlan-interface": ["vxlan0.500"],
           "protocols": {"bgp-evpn": {"bgp-instance": [{"id": 1, "evi": 10}]}},
       },
       "op": "replace",
   }
   ```

4. Payloads are accumulated across phases (topology, fabric, services,
   overrides) into three buckets: `config_update`, `config_replace`,
   `config_delete`.

5. The **configure** role applies everything in a single `nokia.srlinux.config`
   call with update, replace, and delete lists.

6. The **purge** role (opt-in via `--tags full`) fetches the live device state,
   compares against intent, and adds delete paths for stale network-instances,
   VXLAN interfaces, and LAG interfaces.

### Operation semantics

- **update** -- merge leaves only (physical interfaces, hostname, BFD). Safe
  for shared paths.
- **replace** -- full subtree ownership (subinterfaces, network-instances,
  routing-policy, VXLAN, LAG, ethernet-segments). The device removes anything
  under the path not in the intent.
- **delete** -- explicit removal of stale resources during pruning.

### Playbook phases

| Tag | Scope |
|-----|-------|
| `topology` | system0, underlay interfaces, BFD, hostname, LLDP |
| `fabric` | default NI (BGP underlay/overlay), routing-policy |
| `services` | edge interfaces, LAGs, IRBs, VXLAN, mac-vrf, ip-vrf, ES |
| `overrides` | raw SR Linux config patches from `extras.configlets` |

Running without `--tags` applies all phases. With `--tags full`, pruning of
stale resources is also performed.

## EDA vs Ansible: Where the Smarts Live

| Concern | EDA Mode | Ansible Mode |
|---------|----------|-------------|
| Node-to-device config mapping | EDA intent engine (server-side) | srl_builders filter plugins (client-side) |
| Config diff computation | EDA computes minimal delta | `replace` semantics + explicit prune |
| Declarative enforcement | CR-level: submit desired state | Path-level: replace subtrees |
| Version awareness | EDA handles internally | Version-dispatched builders (v25_3, v26, default) |
| Execution model | Atomic transactions with rollback | Sequential per-node with optional confirmed-commit |
| Pruning | `compute_diff()` with delete CRs in same transaction | `build_prune_deletes()` compares intent vs. live state |

EDA mode operates at **intent level** (declare what you want, EDA figures out
how). Ansible mode operates at **device config level** (the tooling must produce
the exact JSON-RPC paths and values). The `FabricIntent` abstraction lets the
same input drive both paths.

## CLI Usage

```bash
# Generate EDA CRs (no deployment)
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --generate-only

# Preview changes against live EDA
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --diff \
  --eda-url https://eda.example.com

# Deploy to EDA
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda \
  --eda-url https://eda.example.com

# Deploy specific phases
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --phase topology --phase fabric \
  --eda-url https://eda.example.com

# Deploy with pruning (remove stale resources)
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --prune --yes \
  --eda-url https://eda.example.com

# Destroy all managed resources
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --destroy \
  --eda-url https://eda.example.com

# Generate Ansible project
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode ansible

# Generate containerlab topology
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda --generate-clab --generate-only
```
