# Design Rationale

This document explains what NVD automation is for, when it earns its place in
a deployment pipeline, and — just as importantly — when it does not. It is
meant to be read alongside the README by anyone evaluating whether to adopt
the tool, fork it, or bypass it and drive EDA CRs directly.

## The honest critique

EDA already exposes a declarative API with Custom Resources. A team can
perfectly well author CRs directly, keep them in Git, and reconcile them with
ArgoCD, Flux, or a home-grown controller. From that vantage point, adding
NVD automation on top means:

- another intent model to learn;
- another translation layer to debug;
- another piece of code somebody has to own;
- another surface that can drift from the underlying CR schema as EDA evolves.

A capable automation team will legitimately ask *"why don't we just template
CRs ourselves?"* — and for some teams, that is the right answer. This document
does not try to argue otherwise. It tries to say clearly where NVD automation
is worth the cost and where it is not.

## Where NVD automation earns its keep

### Intent compression

A three-stage EVPN-VXLAN fabric with four spines and thirty-two leaves is
roughly fifteen lines of intent in NVD automation — topology, ASN ranges,
address pools, overlay parameters — versus hundreds of CRs with
cross-references that must stay internally consistent. The compression ratio
is the value. Not because writing CRs is hard, but because keeping them
consistent *under change* is hard. Change the ASN scheme by hand and dozens
of files move; change one field in NVD automation and the plan re-renders.

### Deterministic address and identifier planning

Loopbacks, P2P /31s, VTEP IPs, RDs, RTs, ASNs — all the identifiers that must
be unique, follow a scheme, and never collide. Doing this by hand is where
mistakes happen and where audits fail. A planner that is deterministic from a
seed, so the same intent always produces the same plan, keeps reviews focused
on *intent diffs* rather than on chasing whether some auto-assigned loopback
has silently moved.

### Design lockfiles

The lockfile pattern — intent resolves to a plan, the plan is committed to
Git — means concrete address assignments stay stable across re-renders.
Without this, every regeneration risks reshuffling identifiers, with a large
blast radius. Getting this right by hand with templating is difficult and it
is the kind of thing that surfaces months into production.

### Opinionated guardrails

NVD automation encodes the design choices that come with a Nokia Validated
Design: MTU hierarchy, anycast-gateway behaviour, confirmed-commit phasing,
symmetric vs. asymmetric IRB, and so on. A team writing CRs from scratch
either knows all of this or learns it the hard way. NVD automation ships the
validated design *as executable code* rather than as a PDF that nobody
re-reads once the fabric is up.

### Phased deployment semantics

Confirmed-commit, ordered apply, rollback-on-failure — this is workflow logic
that does not naturally live in a CR. It can be rebuilt in Argo with hooks
and custom controllers, but that is exactly what NVD automation already
provides.

### A translation target for other systems of record

When the customer's canonical truth lives elsewhere — NetBox, Nautobot, a
homegrown CMDB — *something* has to translate that truth into EDA CRs. The
NVD automation intent model is a cleaner translation target than the full CR
surface, because it exposes fewer knobs and encodes the invariants
explicitly. The architectural split *(SoT owns inventory, NVD automation
owns design translation, EDA owns runtime)* keeps each layer's responsibility
tight.

## Where NVD automation is overhead

NVD automation is not the right tool when:

- the team has a mature platform-engineering practice, runs everything as
  CRs, and wants EDA to act purely as a reconciliation engine;
- the team has its own strong opinions about address planning or design
  patterns that NVD automation's opinions would fight;
- the deployment is small enough that the intent-compression benefit does
  not repay the extra abstraction;
- the required design flexibility falls outside what the validated design
  covers, and forcing it through NVD automation would mean routing around
  it.

In these cases, driving EDA CRs directly is the honest recommendation.

## Positioning

NVD automation is a **validated-design accelerator**, not a fabric-automation
requirement. Its job is to get a team from "we have EDA" to "we have a
running, conformant fabric in production" without needing to make a couple of
hundred small design decisions correctly along the way. For teams without
deep network-automation maturity — which is most teams — that is a large
amount of value. For teams with deep maturity, it is a reference
implementation to read, fork, or ignore.

The intended framing is therefore not *"this is the way to use EDA"* but
rather:

- here is the fast path;
- here is the underlying API if you want to go direct;
- here is how to mix the two.

Adoption is opt-in per layer, which is what removes the complexity-tax
objection: teams pay for NVD automation only where it is buying them
something.

## Exit path

A serious adoption story has to include a serious exit story. Otherwise the
tool is a liability regardless of how good the Day-1 experience is. This
section is deliberately explicit about how a team stops using NVD automation
if it stops fitting.

### What "moving away" actually means

The exit path depends on which of three things the team wants to do:

1. **Keep the fabric, stop using NVD automation for future changes.** The
   team wants to own the CRs directly from now on — typically because the
   platform practice has matured, or the design flexibility they need is
   outside what the validated-design opinions allow. This is the common
   case and the one to optimise for.
2. **Migrate to a different intent layer.** The team has built or adopted a
   different abstraction — a Terraform module set, an internal DSL, a
   NetBox-driven pipeline — and wants that to become the source of truth.
   This is case 1 with an extra translation step.
3. **Leave EDA entirely.** A different problem — a fabric-manager exit, not
   an NVD automation exit. Out of scope for this document.

### The structural reason exit can be clean

NVD automation sits above EDA. It compiles intent into CRs and hands them to
EDA to reconcile. The artefacts that actually run the fabric — the CRs — are
already in EDA-native form. If NVD automation disappears, the CRs it
produced are still valid EDA state. That is structurally very different from
a tool that owns runtime state itself.

The exit story therefore hinges on one question: *can the team take the CRs
NVD automation has generated and continue to operate the fabric from those
CRs directly?* If yes, the exit is real. If the tool holds hidden state that
the CRs don't reflect, the exit is only nominal — the team is still
dependent on NVD automation in practice, even after they stop invoking it.

### The concrete exit path

For case 1:

1. **Render and freeze.** Run NVD automation one final time to produce the
   full CR set for the current fabric state. Commit the CRs to a Git repo —
   this becomes the new source of truth. The lockfile is committed alongside
   so identifier assignments stay pinned.
2. **Detach.** Stop running NVD automation's reconciliation. From this point,
   changes are made by editing CRs directly and applying them via whatever
   GitOps or pipeline setup the team prefers — ArgoCD, Flux, `kubectl`, or a
   Terraform provider against the EDA API.
3. **Verify.** Confirm the fabric still reconciles cleanly against the
   committed CRs. This is the moment of truth: if EDA reports drift that
   isn't in the CRs, the tool was holding hidden state and the exit isn't
   clean.
4. **Adopt whatever comes next.** Templating, Kustomize overlays, a Terraform
   module, a custom operator — the team's choice. The rendered CRs are the
   starting point for that new approach, not a throwaway.

For case 2, add a step between 1 and 4: write a translator from the intent
model (or from the rendered CRs) into the new intent model. The intent YAML
is deliberately small and structured, which makes it a reasonable input to a
migration script — often easier to translate *from* than the CRs themselves.

### Design commitments that make exit possible

The exit path is not automatic. It rests on design commitments NVD
automation has to honour and that customers can hold the project to:

- **All state that affects the fabric appears in rendered CRs.** No implicit
  behaviour applied at reconcile time that isn't in the output. This is the
  single most important commitment.
- **Rendered CRs are human-readable and human-editable.** A network engineer
  should be able to read a rendered CR and understand what it does without
  needing NVD automation to interpret it.
- **The lockfile format is documented and stable.** A team taking over the
  CRs must be able to understand how identifiers were assigned and continue
  the scheme by hand if they choose.
- **No hidden runtime dependencies on the tool.** The CRs must be valid EDA
  CRs standalone — no annotations that only NVD automation knows how to
  interpret, no references to tool-only resources.
- **A documented render-and-detach mode.** An explicit command that emits the
  full CR set in a form ready to be committed and operated independently.
  This makes exit a first-class supported workflow, not something a customer
  has to reverse-engineer.
- **Idempotence.** Re-rendering the same intent produces byte-identical CRs
  (modulo timestamps). Without this, a team cannot tell what the tool would
  have done versus what they have edited by hand.

### The awkward cases

Three honest caveats:

- **Partial exit.** A team may want to keep using NVD automation for
  greenfield PODs while hand-managing a specific tenant's VRFs. The design
  has to support cohabitation — some CRs owned by the tool, some owned by
  the team, with a clear rule for what happens when they touch the same
  object.
- **Design drift after exit.** Once CRs are edited by hand, they will drift
  from the validated design. That is the team's choice — but the validated
  guarantees (Nokia-validated behaviour, interop assumptions) no longer
  apply. This is stated plainly, not as a threat.
- **Re-adoption is not automatic.** If a team wants to come back after a
  period of hand-editing, importing modified CRs back into NVD automation's
  intent model is genuinely hard, because the CR surface is larger than the
  intent surface and the reverse mapping is lossy. Realistic answer:
  re-adoption means re-authoring intent, not automated import.

## A note on agentic and MCP-driven workflows

A structured intent model is a better generation target for an LLM than raw
CRs, because the surface area is smaller and the invariants are explicit.
This makes NVD automation a natural pairing with MCP-driven operator
assistants: the model reasons about intent, NVD automation handles
compilation into CRs, and EDA handles reconciliation. This is a deliberate
direction for the project rather than an accident of its design.
