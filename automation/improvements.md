# NVD Automation — Improvement Analysis

Focus: maximizing code reuse across NVDs and making the engine version-aware
(EDA release + SR Linux 24.10 onwards). This reviews `automation/` as of
commit `8c46af4`.

## Summary

The two themes that drove the original analysis are now resolved.

**Version awareness.** The engine was single-EDA-version by construction. It
now selects an EDA release at runtime: `eda_models/profiles.py` holds
version-keyed `Registry` profiles (25.12 and 26.4), chosen by `--eda-version`
or discovered from the live cluster. SR Linux gating moved from an exact-match
allow-list to a floor-plus-trains window carried by each EDA profile, so
"24.10 onwards" is real semantics rather than a frozen list, and the Ansible
path enforces the floor instead of failing open.

**Reuse across NVDs.** Shared schema `$defs`, shared builder helpers and a
data-driven extras merge are all wired up; the copy-pasted duplicates between
the three engine-managed designs are gone.

One item remains open: **§2.4** — ai-dc and the reference designs still live
outside the engine as hand-maintained artifacts.

---

## Part 1 — Version awareness (resolved)

### 1.1 EDA-version-keyed registry profiles — done

`eda_models/profiles.py` models each supported EDA release as a `Registry`:
a `group → apiVersion` map plus that release's SR Linux support window. The
`CRType` descriptors are built from one stable `_CR_SKELETON` (attr, group,
kind, plural), so only the version strings vary per profile.

- `25.12` — mixed graduated/pre-release groups (the historical default).
- `26.4` — verified against a fresh 26.4.2 install: `services`/`protocols` at
  **v2**, every other group graduated `v1alpha1` → **v1**. Note an *in-place
  upgrade* from 25.12 keeps serving the old apiVersions, so an upgraded
  cluster is not a reliable reference for what 26.4 natively expects.

`registry.py` is now a back-compat facade binding the module-level constants to
the default profile. `deploy.py` takes `--eda-version` (matched by
`major.minor`, so `26.4`, `26.4.2` and `v26.4.2-2605212019-g73187ba6` all
resolve), falling back to probing the cluster via `EDAClient.get_eda_version()`
and then to the default profile.

The 26.4 v2 spec shapes diverged enough from 25.12 that apiVersion strings
alone were not sufficient, so a profile also carries a `generator_variant`:
`"v2"` selects the `eda_generator_v2` backend and the `eda_models.eda_26_4`
model package, while `"v1"` keeps the original generator. Adding a future
release is a profile entry, plus a generator variant only if its CR spec
shapes actually change.

### 1.2 Floor + train-based SR Linux gate — done

`SrlSupport` replaces the exact-match allow-list: a `floor` (24.10) plus known
`SrlTrain`s (24.10 / 25.3 / 25.7 / 25.10 / 26.3) with optional per-train
min/max patch. A version passes when it is at or above the floor *and* lands in
a known train with the patch in range, so maintenance releases no longer need
hand-registering. The support window hangs off the EDA profile, so the gate
answers "does the selected EDA support this SR Linux version".

This also closed the divergence the analysis flagged: 26.3 is a known train, so
`srl_builders/v26.py` is reachable from EDA mode. `SUPPORTED_SRL_VERSIONS`
survives only as the list of explicitly *tested* exact versions, used by
acceptance tests.

### 1.3 Version floor on the Ansible path — done, with one residual

Ansible mode in `deploy.py` now runs `check_srl_floor()` over every node
version before generating, so a sub-24.10 node is rejected rather than silently
falling through to the 24.10 base builder. `get_builder()` logs which module a
version resolved to (INFO on first resolution, DEBUG on cache hits) including
the fall-through to `default`, and `v24_10.py` was added as an explicit alias
so the floor is visible in the module list.

**Residual:** the gate lives in `deploy.py`, not in `ansible_generator` or the
`srl_config` filter. Driving the generator as a library — or running the filter
plugin directly from a playbook — still bypasses the floor check. Small fix:
move the check into `ansible_generator.generate()`.

### 1.4 Codegen field-drop — done

The generator no longer silently drops fields when two inline sub-schemas
collapse to the same class name: `_collect_class()` in
`codegen/generate_models.py` **unions** their `properties`/`required` instead of
letting one shadow the other. A regression test in `tests/unit/test_eda_models.py`
asserts the previously-dropped fields (`FabricBgp.asn_pool`,
`StaticRouteBfd.local_discriminator`/`remote_discriminator`,
`PolicyBgp.as_path_match`/`evpn_route_type`) are present, so a regression fails
CI rather than surfacing as a fabric with BGP up and zero routes. The
`MANUAL PATCH` step is gone from the regen workflow in `DEVELOPMENT.md`.

(The original recommendation was to key inline sub-schemas by full JSON
pointer. Unioning was chosen instead: it keeps the generated class names stable
— and therefore the hand-written imports across the generators — while
removing the same failure mode.)

### 1.5 Shared SR Linux builder phase composition — done

`srl_builders/_phases.py` defines the three phase entry points
(`build_topology_updates` / `build_fabric_updates` / `build_services_updates`)
once. Each version module calls `make_phase_entrypoints(sys.modules[__name__])`,
which resolves the underlying `build_*` builders on that module namespace at
call time, so per-version overrides and re-exports both work and phase ordering
is identical across versions by construction. Used by `default.py`, `v25_3.py`
and `v26.py`.

---

## Part 2 — Code reuse across NVDs

### 2.1 Shared schema defs — done

All three engine-managed designs alias the common `$defs` instead of
duplicating them:

- `common_topology_defs.json` — `labels`, `edgeInterface`, `lag`, `defaultMtu`,
  `banner`, `configlet`, `eda`, `credentials`, `prefixSet`, `extras`
- `common_services_defs.json` — `router`, `vlan`, `routingPolicy`,
  `extendedRouter`, `extendedVlan`

Design schemas reference these by absolute `$id` (e.g.
`https://nvd.nokia.com/schemas/common/services-defs#/$defs/router`).
`schema_validator._validate()` resolves them through a `referencing` registry,
and `bundle_common_refs()` inlines them when generating the on-disk
`*_fragment_schema.json`, so editor validation stays self-contained. This
removed ~500 duplicated lines while leaving every design's validation contract
byte-identical (verified by fully expanding the effective schema before and
after; the only deltas were two `default` annotations that collapsed-spine and
unconstrained-3-stage previously lacked).

What deliberately stays local: `bridgeDomain`, `irbInterface`,
`routedInterface`, `staticRoute`, `nodeGroup`/`nodeOverride`/`link`/`node`, and
`underlay`. These are not cosmetic copies — collapsed-spine adds a `SIMPLE`
bridge-domain type with a conditional `required`, unconstrained-3-stage exposes
protocol/BFD knobs the locked designs don't, and 3-stage carries `extended*`
variants for extras overrides. Unifying them needs a base-plus-extension split
(common base def + per-design `allOf` narrowing), which changes what each
design accepts and so wants its own review rather than a mechanical `$ref` swap.

### 2.2 Shared design builder helpers — done

`_common_builders.py` owns everything that was copy-pasted between designs:

- node scaffolding — `validate_unique_names`, `validate_mgmt_ips`,
  `apply_node_overrides`, `increment_ip`
- `default_routing_policies()` — the eBGP ISL prefix-set plus import/export
  policy defaults, previously ~70 duplicated lines per design
- `build_configlets()` / `merge_extras_configlets()` — raw-dict→`ConfigletIntent`
  mapping
- `build_policy_statements()` / `build_prefix_entries()` and the
  `normalize_policy_update()` / `normalize_prefix_set_update()` merge hooks
- `derive_default_ip_mtu()`, `build_credentials()`, `build_eda_settings()`

Consolidating the merge hooks fixed two real collapsed-spine defects the
duplication had hidden: it called `merge_by_name` without the normalization
hooks, so overriding a design-default routing policy by name crashed in
`FabricIntent` cross-reference validation (`'dict' object has no attribute
'match'`), and an overridden default kept `internal=True` so EDA would skip
emitting it.

What stays per-design: the *content* of design-generated configlets (3-stage's
node-isolation event handler vs collapsed-spine's ESI DF timers), bridge-domain
and IRB enrichment (SIMPLE BD type, dual-stack), and topology generation
(collapsed-spine's explicit ISLs vs 3-stage's full-mesh allocation).

### 2.3 Data-driven extras merge — done

The per-resource `_merge_extras_<resource>` functions are gone. `core/extras.py`
provides `apply_extras(current, extras, table)` driven by a
`resource → ExtrasSpec(model_cls, pre_process)` table, and the table lives once
in `_common_builders.service_extras_table()` behind
`apply_service_extras(current, extras, default_ip_mtu=...)`. Both locked designs
call that one function, so adding a service resource to the extras contract is a
single table entry rather than a function per resource per design.

Configlets stay outside the table on purpose: they replace by name rather than
overlaying field-by-field (a raw config patch is only meaningful as a whole),
which is what `merge_extras_configlets()` does.

Collapsed-spine extras were wired up as part of this: `topology.extras.configlets`
was accepted by its schema but silently dropped by the builder, and
`services.extras` was a free-form `additionalProperties: true` bag that nothing
read. Its services schema now declares the six extras resource lists against
`extended*` partial-override defs (only `name` required, cross-field
conditionals such as the EVPNVXLAN `vni`/`evi` requirement dropped so a partial
override need not restate the resource). `extendedRouter` and `extendedVlan`
were identical to 3-stage's, so they were promoted to
`common_services_defs.json`; the bridge-domain, IRB, routed-interface and
static-route variants stay local because collapsed-spine's base shapes differ.

### 2.4 ai-dc and the reference designs bypass the engine — OPEN

`SUPPORTED_DESIGNS` registers three designs, but the repository ships more.

**ai-dc — the real gap.** All three variants
(`lenovo-amd-nokia-ai-fabric`, `two-stripe-rail-optimized`,
`two-stripe-rail-optimized-nvidia`) are deployed via hand-written
`eda-manifests/*.yaml` plus `deploy-*.sh` — roughly 57 manifest files and
~10.8k lines of raw CRs that re-encode by hand the very objects (`Init`,
`TopoNode`, pools, `Interface`, `Fabric`, services) the generator already
produces from intent. The two `two-stripe-rail-optimized*` manifest trees are
**byte-identical** to each other (21 files, 4814 lines each), so nearly half of
that total is a verbatim copy maintained twice. None of it benefits from the
engine's validation, diff, prune or sync-gating.

Recommendation: add an `ai-dc` design module. Nodes, pools, services and
interfaces are already modeled by `FabricIntent`; rail-optimized link
generation is the main new logic. The two stripe variants should differ only by
input, not by a second manifest tree.

**Reference designs — cheaper than first assessed.** `reference-designs/`
carries eBGP-underlay/iBGP-overlay and OSPF-underlay/iBGP-overlay 3-stage
variants plus a 4-way collapsed spine, as static containerlab topologies and
checked-in device configs (~75 files). The original recommendation was to
parameterize the underlay/overlay protocol in the 3-stage builder — but that
work already exists elsewhere: `FabricIntent` models
`underlay_protocol` (`EBGP`/`OSPFv2`/`OSPFv3`) and `overlay_protocol`
(`IBGP`/`EBGP`), and `unconstrained-3-stage` already exposes both in its
schema. These reference designs can therefore become *input sets* for the
existing unconstrained builder with no engine change; the locked
`3-stage-evpn-vxlan` builder can keep hardcoding eBGP/eBGP.

**dci — no longer in scope.** `validated-designs/dci/` is now only
`dci-without-eda` (a containerlab lab with its own tests/tools, no EDA
manifests and no deploy script), so there is nothing here for the engine to
absorb. DCI config generation remains a separate capability.

---

## Priority

| # | Item | Effort | Payoff |
|---|------|--------|--------|
| 2.4a | Bring ai-dc under the engine as a design module | L | Removes ~10.8k lines of hand-maintained manifests |
| 2.4b | Re-express reference designs as `unconstrained-3-stage` inputs | M | Retires 3 static config trees; no engine change needed |
| 1.3 | Move the SRL floor check into `ansible_generator.generate()` | S | Closes the library-call bypass |

S = hours, M = a day or two, L = multi-day.

## What's already good (keep)

The `FabricIntent` abstraction driving both EDA and Ansible from one input; the
EDA-version-keyed profile registry with its `generator_variant` escape hatch for
releases that change CR shapes; the SR Linux exact→minor→major→default builder
dispatch with documented breaking-change overrides and resolution logging; the
fragment-merge input system; the shared schema `$defs` and `_common_builders`
layer; and the layered unit tests that load real design inputs.
