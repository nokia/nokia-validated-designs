# NVD Automation — Improvement Analysis
 
Focus: maximizing code reuse across NVDs and making the engine version-aware
(EDA release + SR Linux 24.10 onwards). This reviews `automation/` as of
commit `3d01fe5`.
 
## Summary
 
The architecture is sound: a single design-agnostic `FabricIntent`, a central
EDA type registry, a clean SR-Linux version-dispatch with an inheritance chain,
and good test/doc coverage. The improvements below build on those existing
patterns rather than replacing them.
 
Two themes dominate. First, **the engine is single-EDA-version by
construction** — there is no way to target more than one EDA release, while SR
Linux versioning is handled well. Second, **reuse infrastructure exists but is
only half-wired** — shared schema defs and a `_common_builders` module are in
place, yet designs still carry large copy-pasted duplicates that have begun to
drift. (The schema half of that second theme is now resolved; see §2.1.)
 
---
 
## Part 1 — Version awareness
 
### 1.1 EDA version is hardcoded, not selectable (highest impact)
 
`eda_models/registry.py` pins one release (`EDA_VERSION = "25.12.4"`) and one
set of apiVersions, mixing graduated (`services…/v1`, `core…/v1`) and
pre-release groups (`interfaces…/v1alpha1`, `fabrics…/v1alpha1`,
`config…/v1alpha1`, `siteinfo…/v1alpha1`, `routingpolicies…/v1alpha1`). There is
no analog to the SR-Linux `get_builder()` dispatch for EDA. Per `DEVELOPMENT.md`,
moving to a new EDA release is a *destructive hand-edit* of the registry plus a
model regen plus re-applying a manual patch — so the codebase can only ever
describe one EDA version at a time.
 
This is the central gap against the "version-aware, including EDA version" goal.
Several of those `v1alpha1` groups graduate to `v1` across releases; today that
is an irreversible edit rather than a selectable target.
 
Recommendation: mirror the SR-Linux pattern for EDA. Introduce an
EDA-version-keyed profile — e.g. `registry_25_12.py`, `registry_26_x.py`, or a
single table mapping `(eda_version) → {group: apiVersion}` — selected by a
`--eda-version` flag (or discovered from the live cluster's `/openapi/v3`
discovery doc). The `CRType` descriptors stay; only the apiVersion strings vary
per profile. `eda.py` already parses `apiVersion` generically when building URLs,
so the executor needs no structural change.
 
### 1.2 `check_srl_version` is an exact-match allow-list
 
`SUPPORTED_SRL_VERSIONS` is a hand-maintained list of exact strings, and
`check_srl_version()` does `version in SUPPORTED_SRL_VERSIONS`. Consequences:
 
- Every maintenance release (24.10.5, 25.3.4, …) must be added by hand or EDA
  deployment is refused — there is no "24.10 onwards" floor or per-train range
  semantics, despite that being the stated requirement.
- The list and the actual builder coverage have **already diverged**: a
  `srl_builders/v26.py` exists, but no 26.x string is in
  `SUPPORTED_SRL_VERSIONS`, so EDA mode would reject the very versions the
  Ansible side is built for.
- The list conflates two different questions — "which SRL releases does *this
  EDA* support" (an EDA-mode gate) and "do we have a builder" (an Ansible
  concern).
Recommendation: replace the flat list with a minimum-floor + supported-trains
model (e.g. floor `24.10`, with known trains `24.10 / 25.3 / 25.7 / 25.10 / 26.3`
and optional per-train min/max patch). Derive supported SRL ranges *per EDA
profile* (ties into 1.1) so the gate answers "does the selected EDA support this
SRL version" rather than matching a frozen string.
 
### 1.3 Ansible mode has no version floor and fails open
 
EDA mode calls `check_srl_version`; the Ansible path does not. `get_builder()`
resolves exact → minor → major → `default`, and `default.py` *is* the 24.10
builder. So a sub-24.10 node (e.g. 23.10) silently falls through to the 24.10
builder and produces payloads that may be wrong for that device, with no warning.
The "24.10 onwards" constraint is documented but not enforced on this path.
 
Recommendation: run the same version gate in `ansible_generator` / the
`srl_config` filter, and have `get_builder()` log which module it resolved to
(it caches silently today). Consider renaming `default.py` to make the floor
explicit — e.g. add a `v24_10.py` alias — so "default" doesn't quietly mean "24.10
base" forever.
 
### 1.4 Codegen `MANUAL PATCH` drift is a version-upgrade landmine
 
`DEVELOPMENT.md` documents that the model generator collapses inline sub-schemas
by leaf property name and silently drops fields (e.g. `FabricBgp.asn_pool`),
which Pydantic's `extra="ignore"` then drops again — invisible at build time,
surfacing only as a fabric with BGP up and zero routes. The mitigation is "grep
for `MANUAL PATCH` and re-apply after every regen." Since model regen is part of
every EDA upgrade, this couples a correctness bug to the version workflow.
 
Recommendation: fix the generator to key inline sub-schemas by full JSON pointer
(as the doc itself suggests), or add a post-generation assertion/test that fails
if a known-required field is missing — so a dropped field breaks CI instead of
production.
 
### 1.5 Minor: SR-Linux builder boilerplate is duplicated per version
 
The four phase entry points (`build_topology_updates`, `build_fabric_updates`,
`build_services_updates`) and the "re-export the unchanged functions" block are
copied verbatim across `default.py`, `v25_3.py`, and `v26.py`. They can drift
independently. A small base (a class, or a shared `compose_phase()` helper that
each module parameterizes with its overridden builders) removes ~30 lines of
duplicated dispatch per version and keeps phase composition identical across
versions by construction.
 
---
 
## Part 2 — Code reuse across NVDs
 
### 2.1 Shared schema defs — wired up (was: wired to nothing)

**Status: done for every validation-equivalent def.** All three designs now
alias the common `$defs` instead of duplicating them:

- `common_topology_defs.json` — `labels`, `edgeInterface`, `lag`, `defaultMtu`,
  `banner`, `configlet`, `eda`, `credentials`, `prefixSet`, `extras`
- `common_services_defs.json` — `router`, `vlan`, `routingPolicy`

`schema_validator._validate()` resolves the external `$ref`s through a
`referencing` registry, and `bundle_common_refs()` inlines them when generating
the on-disk `*_fragment_schema.json`, so editor validation stays self-contained.
The last round removed ~500 duplicated lines while leaving every design's
validation contract byte-identical (verified by fully expanding the effective
schema before and after; the only deltas were two `default` annotations that
collapsed-spine and unconstrained-3-stage previously lacked).

What deliberately stays local: `bridgeDomain`, `irbInterface`,
`routedInterface`, `staticRoute`, `nodeGroup`/`nodeOverride`/`link`/`node`, and
`underlay`. These are not cosmetic copies — collapsed-spine adds a `SIMPLE`
bridge-domain type with a conditional `required`, unconstrained-3-stage exposes
protocol/BFD knobs the locked designs don't, and 3-stage carries `extended*`
variants for extras overrides. Unifying them needs a base-plus-extension split
(common base def + per-design `allOf` narrowing), which changes what each design
accepts and so wants its own review rather than a mechanical `$ref` swap.
 
### 2.2 Design builders duplicate near-identical helpers — resolved

**Status: done.** `_common_builders.py` now owns everything that was
copy-pasted between designs:

- node scaffolding — `validate_unique_names`, `validate_mgmt_ips`,
  `apply_node_overrides`, `increment_ip`
- `default_routing_policies()` — the eBGP ISL prefix-set plus import/export
  policy defaults, previously ~70 duplicated lines per design that were verified
  to produce identical output
- `build_configlets()` / `merge_extras_configlets()` — raw-dict→`ConfigletIntent`
  mapping, previously written twice (unconstrained's explicit builder and
  3-stage's extras merge)
- `build_policy_statements()` / `build_prefix_entries()` and the
  `normalize_policy_update()` / `normalize_prefix_set_update()` merge hooks,
  previously duplicated between `build_routing_policies` and 3-stage's local
  hooks
- `derive_default_ip_mtu()`, `build_credentials()`, `build_eda_settings()` —
  small blocks that were triplicated verbatim

Consolidating the merge hooks fixed two real collapsed-spine defects that the
duplication had hidden: it called `merge_by_name` without the normalization
hooks, so overriding a design-default routing policy by name crashed in
`FabricIntent` cross-reference validation (`'dict' object has no attribute
'match'`), and an overridden default kept `internal=True` so EDA would skip
emitting it.

What stays per-design: the *content* of design-generated configlets (3-stage's
node-isolation event handler vs collapsed-spine's ESI DF timers), bridge-domain
and IRB enrichment (SIMPLE BD type, dual-stack), and topology generation
(collapsed-spine's explicit ISLs vs 3-stage's full-mesh allocation).
 
### 2.3 The `_merge_extras_*` family is boilerplate over `merge_by_name` — resolved

**Status: done.** The per-resource `_merge_extras_<resource>` functions are gone.
`core/extras.py` provides `apply_extras(current, extras, table)` driven by a
`resource → ExtrasSpec(model_cls, pre_process)` table, and the table itself now
lives once in `_common_builders.service_extras_table()` behind
`apply_service_extras(current, extras, default_ip_mtu=...)`. Both locked designs
(3-stage and collapsed-spine) call that one function, so adding a service
resource to the extras contract is a single table entry rather than a function
per resource per design.

Configlets stay outside the table on purpose: they replace by name rather than
overlaying field-by-field (a raw config patch is only meaningful as a whole),
which is what `merge_extras_configlets()` does.

Collapsed-spine extras were wired up as part of this: `topology.extras.configlets`
was accepted by its schema but silently dropped by the builder, and
`services.extras` was a free-form `additionalProperties: true` bag that nothing
read. Its services schema now declares the six extras resource lists against
`extended*` partial-override defs (only `name` required, cross-field conditionals
such as the EVPNVXLAN `vni`/`evi` requirement dropped so a partial override need
not restate the resource). `extendedRouter` and `extendedVlan` are identical to
3-stage's, so they were promoted to `common_services_defs.json` and both designs
alias them; the bridge-domain, IRB, routed-interface and static-route variants
stay local because collapsed-spine's base shapes differ (SIMPLE BD type,
dual-stack IRB and routed interfaces, non-IPv4-patterned static-route prefixes).
 
### 2.4 Two validated NVDs and all reference designs bypass the engine
 
`DESIGN_BUILDERS` registers three designs, but `validated-designs/` also contains
**ai-dc** and **dci**, and `reference-designs/` contains eBGP/iBGP/OSPF underlay
variants and a 4-way collapsed spine. ai-dc and dci are deployed today via
hand-written `eda-manifests/*.yaml` and `deploy-*.sh` scripts — i.e. raw CRs that
re-encode by hand the very objects (`Init`, `TopoNode`, pools, `Interface`,
`Fabric`, services) the generator already produces from intent. That is the
largest single block of duplicated, unvalidated, hand-maintained config in the
repo, and it can't benefit from the engine's diff/prune/sync-gating.
 
The reference designs are mostly *protocol-parameter* variations of the existing
3-stage builder, which hardcodes eBGP underlay + eBGP overlay. The
`srlinux-dci-config` capability already exists on the side.
 
Recommendation (incremental):
- Parameterize underlay/overlay protocol in the 3-stage builder (eBGP/iBGP/OSPF
  as config, not a fork) so the three reference designs collapse into input
  variants of one builder.
- Add a `dci` builder that reuses the DCGW/stitching config generation rather
  than shipping standalone manifests.
- Bring ai-dc under the engine via an `ai-dc` design module (rail-optimized link
  generation is the main new logic; nodes/pools/services/interfaces are already
  modeled). Even partial coverage removes most of the hand-maintained manifests.
---
 
## Priority
 
| # | Item | Effort | Payoff |
|---|------|--------|--------|
| 1.1 | EDA-version-keyed registry profiles + `--eda-version` | M–L | Enables multi-EDA support (core goal) |
| 1.2 | Floor + train-based SRL version gate | S–M | "24.10 onwards" semantics; fixes 26.x gap |
| 2.1 | Wire design schemas to shared `$defs` | M | Kills 3× schema duplication |
| 2.2 | Lift shared builder helpers into `_common_builders` | M | Stops builder drift; eases new designs |
| 1.4 | Fix codegen field-drop (or assert in CI) | M | Removes silent version-upgrade bug |
| 2.3 | Data-driven extras merge | S | Less boilerplate per resource |
| 1.3 | Version gate + resolution logging on Ansible path | S | Closes fail-open hole |
| 1.5 | Share SRL builder phase boilerplate | S | Less per-version duplication |
| 2.4 | Bring ai-dc / dci / reference designs under the engine | L | Removes biggest hand-maintained block |
 
S = hours, M = a day or two, L = multi-day.
 
## What's already good (keep)
 
The `FabricIntent` abstraction driving both EDA and Ansible from one input; the
single-source `CRType` registry; the SR-Linux exact→minor→major→default dispatch
with documented breaking-change overrides; the fragment-merge input system; and
the layered unit tests that load real design inputs. The recommendations extend
these patterns; none require reworking them.

