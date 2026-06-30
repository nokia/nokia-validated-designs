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
drift.
 
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
 
### 2.1 Shared schema defs exist but are wired to nothing
 
`automation/schemas/common_services_defs.json` and `common_topology_defs.json`
are titled "Reusable `$defs` … across all NVD designs" — but **no design schema
and no Python references them** (verified by grep). Meanwhile each design ships
full copies of `topology_schema.json`, `services_schema.json`, and both fragment
schemas. The 3-stage and collapsed-spine services schemas differ by ~700 lines,
most of it identical structure with cosmetic title/description changes plus a few
real divergences (collapsed-spine added dual-stack IRBs and a `SIMPLE` BD type).
 
This is duplication that will drift: a fix to the `bridgeDomain` shape must be
made in three places today.
 
Recommendation: finish the job the shared-defs files started. Have each design
schema `$ref` the common `$defs` and keep only design-specific overrides locally.
The fragment-schema generator (`codegen/generate_fragment_schemas.py`) can then
derive fragments from the composed schema, so there's one source of truth per
resource shape.
 
### 2.2 Design builders duplicate near-identical helpers (drifting copies)
 
`three_stage_evpn_vxlan.py` (1083 lines) and `collapsed_spine.py` (633 lines)
each redefine `_validate_unique_names`, `_validate_mgmt_ips`, `_increment_ip`,
`_apply_node_overrides`, `_default_routing_policies`, `_build_configlets`, and a
family of `_merge_extras_*` functions. Spot-checking the "shared" helpers shows
they are logically identical but textually different (reworded error messages,
collapsed one-liners) — the signature of copy-paste-then-edit. `_increment_ip` is
literally `str(IPv4Address(base) + offset)` in both, written two ways.
 
`_common_builders.py` already exists for exactly this purpose but currently holds
only the trivial 1:1 dict→model mappers. The node-generation scaffolding
(validation, overrides, IP math, extras-merge, default policies) belongs there
too. As it stands, adding a third constrained design means copying
`collapsed_spine.py` and inheriting the same drift.
 
Recommendation: promote the genuinely shared helpers into `_common_builders.py`
(or a small `BaseConstrainedDesign`), leaving only design-specific logic
(collapsed-spine ISL meshing, AI-fabric rail assignment, etc.) in the per-design
modules.
 
### 2.3 The `_merge_extras_*` family is boilerplate over `merge_by_name`
 
`core/extras.py` already provides a generic `merge_by_name`. Each design then
wraps it in ~8 near-identical `_merge_extras_<resource>` functions that differ
only in the model class and an optional pre-process hook. This is a per-resource
× per-design multiplier.
 
Recommendation: replace with one data-driven table
(`resource → (model_cls, pre_process)`) iterated generically, shared across
designs. Adding a resource type then touches one table entry instead of N
functions in M designs (and shrinks the `DEVELOPMENT.md` "add a resource"
checklist).
 
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

