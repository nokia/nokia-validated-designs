"""
NVD Automation Engine — CLI entry point.

The fabric intent is loaded from a design directory's YAML inputs
(``--design <dir>``).

Only the designs registered in
:data:`automation.core.fabric_builder.SUPPORTED_DESIGNS` can be deployed by
this engine (``--list-designs`` prints them). The repository also ships
designs under ``validated-designs/`` that are deployed by their own tooling —
pointing ``--design`` at one of those fails with an explicit message.

Usage:
  python -m automation.deploy \\
    --design validated-designs/3-stage-evpn-vxlan \\
    --mode eda

Common options:
    [--list-designs]
    [--generate-only] [--generate-clab] [--diff]
    [--phase {topology,fabric,services}]
    [--destroy] [--dry-run] [--prune] [--yes]
    [--eda-url URL] [--eda-user USER] [--eda-password PASS]
    [--export-yaml OUTDIR]

On completion a machine-readable summary line
``[NVD-DEPLOY-SUMMARY] {...json...}`` is printed on stdout so CI pipelines can
ingest the result without scraping.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from automation.core.fabric_builder import (
    build_intent,
    check_design_dir,
    find_unmanaged_design_dirs,
    list_supported_designs,
    supported_design_names,
)
from automation.core.models import Credentials, FabricIntent
from automation.core.schema_validator import load_inputs
from automation.eda_models.registry import check_srl_version, check_srl_floor
from automation.eda_models.profiles import (
    DEFAULT_EDA_VERSION,
    get_registry,
    list_eda_versions,
    normalize_eda_version,
)
from automation.executors.eda import EdaClient
from automation.generators.eda_generator import generate as eda_generate

# Marker for machine-readable consumers (CI pipelines).
DEPLOY_SUMMARY_MARKER = "[NVD-DEPLOY-SUMMARY]"

# Value of ``environment`` in topology.yaml that targets real hardware.
PHYSICAL_ENVIRONMENT = "physical"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nokia Validated Design automation engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--design",
        help=(
            "Path to the design directory (e.g. "
            "validated-designs/3-stage-evpn-vxlan). Only the designs listed by "
            "--list-designs are supported."
        ),
    )
    parser.add_argument(
        "--list-designs",
        action="store_true",
        help="List the validated designs this engine can deploy, and exit",
    )

    parser.add_argument(
        "--mode",
        choices=["eda", "ansible"],
        default="eda",
        help="Deployment mode (default: eda)",
    )
    parser.add_argument(
        "--eda-version",
        default=None,
        metavar="VERSION",
        help=(
            "Target EDA release (selects CR apiVersions and the SR Linux "
            "support window). Matched by major.minor, so '26.4', '26.4.2' and "
            "the raw build string all select the same profile. "
            f"Available profiles: {', '.join(list_eda_versions())}. "
            "When omitted in --mode eda, the version is auto-detected from the "
            f"live cluster (falling back to {DEFAULT_EDA_VERSION})."
        ),
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate output files without deploying",
    )
    parser.add_argument(
        "--generate-clab",
        action="store_true",
        help="Generate containerlab topology file with Linux clients",
    )
    parser.add_argument(
        "--phase",
        choices=["topology", "fabric", "services"],
        action="append",
        help="Run only a specific deployment phase (repeatable)",
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Remove all managed resources in reverse dependency order",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="EDA dry-run (validate only) or Ansible check mode",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete EDA resources not in desired state (declarative intent)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip prune confirmation prompt",
    )
    parser.add_argument("--eda-url", help="EDA API URL (overrides EDA_URL env)")
    parser.add_argument("--eda-user", help="EDA username (overrides EDA_USER env)")
    parser.add_argument("--eda-password", help="EDA password (overrides EDA_PASSWORD env)")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Preview changes against live EDA state without applying (EDA mode only)",
    )
    parser.add_argument(
        "--export-yaml",
        metavar="OUTDIR",
        help="Write the built FabricIntent as YAML files to OUTDIR and exit",
    )
    parser.add_argument(
        "--export-manifests",
        metavar="OUTDIR",
        help=(
            "Write each EDA CR as its own numbered YAML file to OUTDIR (for "
            "inspection/review) and exit. Files are ordered by filename to "
            "mirror generation order. NOTE: this is for inspection, not "
            "deployment — applying with 'kubectl apply -f' loses EDA's atomic "
            "transactions, node-sync gating, and safe TopoNode handling."
        ),
    )
    parser.add_argument(
        "--sync-wave",
        action="store_true",
        help=(
            "With --export-manifests, add argocd.argoproj.io/sync-wave "
            "annotations so Argo CD preserves apply order"
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.list_designs:
        _print_designs()
        return 0

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    start = time.monotonic()
    summary: dict[str, Any] = {
        "mode": args.mode,
        "phases": args.phase or [],
        "dry_run": args.dry_run,
        "prune": args.prune,
        "destroy": args.destroy,
        "diff": args.diff,
        "generate_only": args.generate_only,
        "success": False,
        "transaction_id": None,
        "creates": 0,
        "updates": 0,
        "deletes": 0,
        "nodes_affected": [],
        "duration_s": 0,
        "errors": [],
    }

    try:
        intent = _load_intent(args, summary)
        if intent is None:
            return _finish(summary, start, rc=1)
    except Exception as e:
        logging.error("Failed to build intent: %s", e)
        summary["errors"].append(f"intent-build: {e}")
        return _finish(summary, start, rc=1)

    logging.info(
        "Built intent: %d nodes, %d links, %d bridge_domains, %d routers",
        len(intent.nodes),
        len(intent.links),
        len(intent.bridge_domains),
        len(intent.routers),
    )
    summary["design"] = intent.design
    summary["fabric_name"] = intent.fabric_name
    summary["environment"] = intent.environment

    env_errors = _preflight_environment(intent, args)
    if env_errors:
        for err in env_errors:
            logging.error("%s", err)
        summary["errors"].extend(env_errors)
        return _finish(summary, start, rc=1)

    # ---------------------------------------------------------------
    # --export-yaml short-circuit (no deployment, no generation)
    # ---------------------------------------------------------------
    if args.export_yaml:
        _export_yaml(intent, Path(args.export_yaml))
        summary["success"] = True
        summary["export_yaml_dir"] = str(args.export_yaml)
        return _finish(summary, start, rc=0)

    # ---------------------------------------------------------------
    # --export-manifests short-circuit (numbered per-CR YAML, inspection)
    # ---------------------------------------------------------------
    if args.export_manifests:
        from automation.generators.eda_generator import export_manifests

        outdir = Path(args.export_manifests)
        args.eda_version = _resolve_eda_version(args)
        logging.info("Generating EDA CRs for manifest export...")
        resources = eda_generate(intent, registry=get_registry(args.eda_version))
        written = export_manifests(resources, outdir, sync_wave=args.sync_wave)
        print(
            f"\n✅ Exported {len(written)} EDA CR manifests to {outdir}"
        )
        print(
            "   These are for inspection/review. Deploy via EDA "
            "('--mode eda') for atomic transactions and node-sync gating."
        )
        summary["success"] = True
        summary["export_manifests_dir"] = str(outdir)
        summary["resources_generated"] = len(written)
        return _finish(summary, start, rc=0)

    # ---------------------------------------------------------------
    # Generate / deploy phase
    # ---------------------------------------------------------------
    design_dir = Path(args.design) if args.design else Path("validated-designs") / intent.fabric_name
    build_dir = design_dir / "build"

    if args.generate_clab:
        from automation.generators.clab_generator import generate as clab_generate

        logging.info("Generating containerlab topology...")
        clab_path = clab_generate(intent, output_dir=build_dir)
        print(f"\n✅ Generated containerlab topology: {clab_path}")
        print(f"   Client configs: {build_dir}/client-configs/")
        if not args.generate_only:
            summary["success"] = True
            return _finish(summary, start, rc=0)

    if args.mode == "eda":
        args.eda_version = _resolve_eda_version(args)
        registry = get_registry(args.eda_version)
        srl_versions = {node.version for node in intent.nodes if node.version}
        for ver in sorted(srl_versions):
            err = check_srl_version(ver, registry)
            if err:
                logging.error(err)
                summary["errors"].append(err)
                return _finish(summary, start, rc=1)
        if srl_versions:
            logging.info(
                "SRL version check passed (EDA %s): %s",
                registry.eda_version,
                ", ".join(sorted(srl_versions)),
            )

        if args.destroy:
            rc = _run_destroy(args, intent, summary)
            return _finish(summary, start, rc=rc)

        logging.info("Generating EDA CRs (EDA %s)...", registry.eda_version)
        resources = eda_generate(intent, output_dir=build_dir, registry=registry)
        logging.info("Generated %d EDA CRs → %s", len(resources), build_dir)

        if args.generate_only:
            print(
                f"\n✅ Generated {len(resources)} EDA CRs to "
                f"{build_dir}/eda_transaction.json"
            )
            _print_summary(resources)
            summary["success"] = True
            summary["resources_generated"] = len(resources)
            return _finish(summary, start, rc=0)

        if args.diff:
            rc = _run_diff(args, intent, resources, summary)
            return _finish(summary, start, rc=rc)

        rc = _run_apply(args, intent, resources, summary)
        return _finish(summary, start, rc=rc)

    if args.mode == "ansible":
        from automation.generators.ansible_generator import generate as ansible_generate

        srl_versions = {node.version for node in intent.nodes if node.version}
        for ver in sorted(srl_versions):
            err = check_srl_floor(ver)
            if err:
                logging.error(err)
                summary["errors"].append(err)
                return _finish(summary, start, rc=1)
        if srl_versions:
            logging.info(
                "SRL version floor check passed: %s",
                ", ".join(sorted(srl_versions)),
            )

        ansible_dir = design_dir / f"{design_dir.name}-ansible"
        logging.info("Generating Ansible project...")
        output_path = ansible_generate(intent, output_dir=ansible_dir)
        print(f"\n✅ Generated Ansible project to {output_path}")
        print(f"\n   Quick start:")
        print(f"   cd {output_path}")
        print(f"   ansible-galaxy collection install -r requirements.yml")
        print(f"   ansible-playbook -i inventory.yml playbook.yml")
        summary["success"] = True
        summary["ansible_project_path"] = str(output_path)
        return _finish(summary, start, rc=0)

    return _finish(summary, start, rc=0)


# ---------------------------------------------------------------------------
# Supported designs
# ---------------------------------------------------------------------------


def _print_designs() -> None:
    """Print the designs this engine can deploy, plus the ones it cannot."""
    import textwrap

    print("Designs supported by the NVD deployer:\n")
    for spec in list_supported_designs():
        print(f"  {spec.name}  ({spec.strategy})")
        print(f"    --design {spec.design_dir}")
        for line in textwrap.wrap(spec.summary, width=72):
            print(f"    {line}")
        print()

    print(
        "The 'design' field in topology.yaml must be one of: "
        f"{', '.join(supported_design_names())}.\n"
    )

    unmanaged = find_unmanaged_design_dirs()
    if unmanaged:
        print(
            "Other directories under validated-designs/ are NOT deployable by\n"
            "this engine — they have no inputs/ + schemas/ and no registered\n"
            "builder, and ship their own EDA manifests or containerlab labs:\n"
        )
        for name in unmanaged:
            print(f"  - validated-designs/{name}")
        print("\nFollow each of those designs' own README to deploy them.")


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def _load_intent(args: argparse.Namespace, summary: dict[str, Any]) -> FabricIntent | None:
    if not args.design:
        logging.error("--design <dir> is required (see --list-designs)")
        summary["errors"].append("missing --design")
        return None
    design_dir = Path(args.design)
    err = check_design_dir(design_dir)
    if err:
        logging.error("%s", err)
        summary["errors"].append(err)
        return None
    logging.info("Loading inputs from %s", design_dir)
    topology, services = load_inputs(design_dir)
    logging.info(
        "Design: %s | Fabric: %s | Env: %s",
        topology.get("design"),
        topology.get("fabric_name"),
        topology.get("environment"),
    )
    return build_intent(topology, services)


# ---------------------------------------------------------------------------
# Environment preflight
# ---------------------------------------------------------------------------


def _preflight_environment(
    intent: FabricIntent, args: argparse.Namespace
) -> list[str]:
    """Check the built intent against the environment its inputs declare.

    Returns fatal errors. Softer mismatches are logged as warnings here so
    they surface once, up front, rather than as a surprise on the device.
    ``environment: containerlab`` needs no checks — the lab defaults are the
    intended ones there.
    """
    if intent.environment != PHYSICAL_ENVIRONMENT:
        return []

    errors: list[str] = []

    missing_mgmt = [node.name for node in intent.nodes if not node.mgmt_ipv4]
    if missing_mgmt:
        errors.append(
            f"environment=physical requires a management address on every node, but "
            f"{len(missing_mgmt)} have none: {', '.join(missing_mgmt)}. Set "
            "'mgmt_base_ipv4' on the node group, or 'mgmt_ipv4' per node under "
            "'nodes:' in topology.yaml."
        )

    defaults = Credentials()
    if (intent.credentials.username, intent.credentials.password) == (
        defaults.username,
        defaults.password,
    ):
        logging.warning(
            "environment=physical is using the built-in SR Linux factory "
            "credentials (user '%s'). Set 'credentials' in topology.yaml before "
            "onboarding production nodes.",
            defaults.username,
        )

    if args.mode == "eda":
        logging.warning(
            "environment=physical: the generated NodeProfile points at the "
            "'srlimages/srlinux-<version>-bin' image paths and 'srlinux-ghcr-<version>' "
            "schema/LLM-DB names. Confirm the hardware images and schema profiles "
            "exist under those names in the EDA artifact server."
        )

    if args.mode == "ansible":
        logging.warning(
            "environment=physical: the generated Ansible project writes "
            "'ansible_password' in cleartext to group_vars/all.yml. Move it to a "
            "vault or lookup before sharing or committing the project."
        )

    if args.generate_clab:
        logging.warning(
            "environment=physical with --generate-clab: the containerlab topology is "
            "a twin, not the deployment target. Port speeds, breakouts and optics are "
            "not represented, so it validates config and control plane only."
        )

    return errors


# ---------------------------------------------------------------------------
# --export-yaml
# ---------------------------------------------------------------------------


def _export_yaml(intent: FabricIntent, outdir: Path) -> None:
    """Dump the built FabricIntent as a pair of YAML files suitable for diff."""
    import yaml  # PyYAML is already a hard dep

    outdir.mkdir(parents=True, exist_ok=True)

    # Split FabricIntent back into a topology-shaped and a services-shaped dict
    # that mirror the existing YAML layout (not byte-identical to the simple
    # inputs because the intent is already expanded, but stable and diffable).
    data = intent.model_dump(mode="json")

    topo_keys = {
        "design", "fabric_name", "environment", "spine_asn", "leaf_asn_start",
        "system0_prefix", "mgmt_subnet", "nodes", "links", "breakouts",
        "edge_interfaces", "lags", "default_mtus", "banners", "prefix_sets",
        "fabric_export_policies", "fabric_import_policies", "credentials", "eda",
    }
    svc_keys = {
        "bridge_domains", "routers", "irb_interfaces", "vlans",
        "routed_interfaces", "static_routes", "configlets", "routing_policies",
    }

    topo = {k: v for k, v in data.items() if k in topo_keys}
    svc = {k: v for k, v in data.items() if k in svc_keys}

    with (outdir / "topology.yaml").open("w") as f:
        yaml.safe_dump(topo, f, sort_keys=False)
    with (outdir / "services.yaml").open("w") as f:
        yaml.safe_dump(svc, f, sort_keys=False)

    logging.info("Exported FabricIntent YAML to %s", outdir)
    print(f"\n✅ FabricIntent exported to {outdir}/topology.yaml + services.yaml")


# ---------------------------------------------------------------------------
# EDA version resolution
# ---------------------------------------------------------------------------


def _resolve_eda_version(args: argparse.Namespace) -> str:
    """Resolve the EDA version string used to select the registry profile.

    Precedence: an explicit ``--eda-version`` always wins. Otherwise, if EDA
    connection details are available, the running release is auto-detected from
    the live cluster's ``/core/about/version`` endpoint. Falls back to
    :data:`DEFAULT_EDA_VERSION` when neither is possible. The returned value is
    matched by major.minor in :func:`get_registry`.
    """
    if args.eda_version:
        return args.eda_version

    url = args.eda_url or os.environ.get("EDA_URL", "")
    if url:
        try:
            probe = EdaClient(
                url=args.eda_url,
                username=args.eda_user,
                password=args.eda_password,
            )
            detected = probe.get_eda_version()
            if detected:
                logging.info(
                    "Auto-detected EDA version %s → profile %s",
                    detected,
                    normalize_eda_version(detected),
                )
                return detected
        except Exception as e:  # noqa: BLE001 - detection is best-effort
            logging.warning("EDA version auto-detection failed: %s", e)

    logging.warning(
        "Could not auto-detect EDA version; using default profile %s "
        "(pass --eda-version to override)",
        DEFAULT_EDA_VERSION,
    )
    return DEFAULT_EDA_VERSION


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def _run_destroy(args: argparse.Namespace, intent: FabricIntent, summary: dict) -> int:
    try:
        client = EdaClient(
            url=args.eda_url,
            username=args.eda_user,
            password=args.eda_password,
            registry=get_registry(args.eda_version),
        )
    except ValueError as e:
        logging.error(str(e))
        summary["errors"].append(str(e))
        return 1

    ns = intent.eda.namespace
    logging.info("Destroying managed resources at %s (namespace=%s)...", client.url, ns)
    result = client.destroy(
        phases=args.phase,
        dry_run=args.dry_run,
        auto_confirm=args.yes,
        namespace=ns,
    )
    _absorb_result_into_summary(result, summary)

    if result.success:
        label = "Dry-run" if args.dry_run else "Destroy"
        print(f"\n✅ {label} successful: {result.message}")
        if result.transaction_id:
            print(f"   Transaction ID: {result.transaction_id}")
        _print_transaction_details(result.details)
        summary["success"] = True
        return 0
    print(f"\n❌ Destroy failed: {result.message}")
    _print_transaction_details(result.details)
    return 1


def _run_diff(args: argparse.Namespace, intent: FabricIntent, resources, summary: dict) -> int:
    try:
        client = EdaClient(
            url=args.eda_url,
            username=args.eda_user,
            password=args.eda_password,
            registry=get_registry(args.eda_version),
        )
    except ValueError as e:
        logging.error(str(e))
        summary["errors"].append(str(e))
        return 1

    ns = intent.eda.namespace
    logging.info("Fetching current managed resources from EDA...")
    current = client.get_managed_resources(namespace=ns)
    plan = client.compute_diff(resources, current)
    _print_diff(plan)
    summary["success"] = True
    summary["creates"] = len(plan.creates)
    summary["updates"] = len(plan.updates)
    summary["deletes"] = len(plan.deletes)
    return 0


def _run_apply(args: argparse.Namespace, intent: FabricIntent, resources, summary: dict) -> int:
    try:
        client = EdaClient(
            url=args.eda_url,
            username=args.eda_user,
            password=args.eda_password,
            registry=get_registry(args.eda_version),
        )
    except ValueError as e:
        logging.error(str(e))
        summary["errors"].append(str(e))
        return 1

    phases_label = f" (phases: {', '.join(args.phase)})" if args.phase else ""
    logging.info("Deploying to EDA at %s%s...", client.url, phases_label)
    result = client.apply(
        resources=resources,
        phases=args.phase,
        prune=args.prune,
        dry_run=args.dry_run,
        auto_confirm=args.yes,
    )
    _absorb_result_into_summary(result, summary)

    if result.success:
        label = "Dry-run" if args.dry_run else "Deployment"
        print(f"\n✅ {label} successful: {result.message}")
        if result.transaction_id:
            print(f"   Transaction ID: {result.transaction_id}")
        _print_transaction_details(result.details)
        summary["success"] = True
        return 0
    print(f"\n❌ Deployment failed: {result.message}")
    _print_transaction_details(result.details)
    return 1


def _absorb_result_into_summary(result, summary: dict) -> None:
    if result is None:
        return
    summary["transaction_id"] = getattr(result, "transaction_id", None)
    details = getattr(result, "details", {}) or {}
    summary["nodes_affected"] = list(details.get("nodesWithConfigChanges") or [])
    # Creates / updates / deletes from changedCrs aren't explicit; approximate
    # from the plan if we computed it elsewhere (see _run_diff) or leave 0.
    msg = getattr(result, "message", "") or ""
    if not result.success:
        summary["errors"].append(msg)
    # Intent-level errors are handy for the journal entry.
    intents = details.get("intentsRun") or []
    for i in intents:
        for e in i.get("errors", []) or []:
            structured = e.get("structuredError", {})
            intent_name = i.get("intentName", {}).get("name", "?")
            kind = i.get("intentName", {}).get("gvk", {}).get("kind", "?")
            summary["errors"].append(
                f"{kind}/{intent_name}: {structured.get('message', str(e))}"
            )


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------


def _finish(summary: dict, start: float, rc: int) -> int:
    summary["duration_s"] = round(time.monotonic() - start, 2)
    summary["exit_code"] = rc
    # Single machine-readable line on stdout.
    print(f"{DEPLOY_SUMMARY_MARKER} {json.dumps(summary, separators=(',', ':'))}")
    return rc


# ---------------------------------------------------------------------------
# Pretty printers (unchanged from previous version)
# ---------------------------------------------------------------------------


def _print_diff(plan) -> None:
    print(
        f"\nTransaction plan: "
        f"{len(plan.creates)} create, "
        f"{len(plan.updates)} update, "
        f"{len(plan.deletes)} delete"
    )
    if plan.creates:
        print("\n  CREATE:")
        for cr in plan.creates:
            kind = cr.get("kind", "?")
            name = cr.get("metadata", {}).get("name", "?")
            print(f"    + {kind}/{name}")
    if plan.updates:
        print("\n  UPDATE:")
        for cr in plan.updates:
            kind = cr.get("kind", "?")
            name = cr.get("metadata", {}).get("name", "?")
            print(f"    ~ {kind}/{name}")
    if plan.deletes:
        print("\n  DELETE:")
        for entry in plan.deletes:
            kind = entry.get("kind", "?")
            name = entry.get("name", "?")
            print(f"    - {kind}/{name}")
    if plan.total_ops == 0:
        print("\n  No changes detected.")


def _print_summary(resources: list[dict]) -> None:
    kinds: dict[str, int] = {}
    for cr in resources:
        kind = cr.get("kind", "Unknown")
        kinds[kind] = kinds.get(kind, 0) + 1

    print("\nResource summary:")
    for kind in sorted(kinds.keys()):
        print(f"  {kind}: {kinds[kind]}")


def _print_transaction_details(details: dict) -> None:
    if not details:
        return

    exec_summary = details.get("executionSummary", "")
    if exec_summary:
        print(f"\n   Execution: {exec_summary}")

    nodes = details.get("nodesWithConfigChanges") or []
    if nodes:
        print(f"\n   Nodes affected ({len(nodes)}):")
        for node in nodes:
            print(f"     • {node}")

    changed = details.get("changedCrs") or []
    if changed:
        print(f"\n   Changed resources:")
        for group in changed:
            kind = group.get("gvk", {}).get("kind", "?")
            names = group.get("names", [])
            ns = group.get("namespace", "")
            for name in names:
                print(f"     • {kind}/{name} ({ns})")

    intents = details.get("intentsRun") or []
    errored = [i for i in intents if i.get("errors")]
    if errored:
        print(f"\n   Intent errors ({len(errored)}):")
        for intent in errored:
            intent_name = intent.get("intentName", {})
            name = intent_name.get("name", "?")
            kind = intent_name.get("gvk", {}).get("kind", "?")
            errors = intent.get("errors", [])
            for err in errors:
                structured = err.get("structuredError", {})
                msg = structured.get("message", str(err))
                print(f"     ✗ {kind}/{name}: {msg}")

    general_errors = details.get("generalErrors") or []
    if general_errors:
        print(f"\n   General errors ({len(general_errors)}):")
        for err in general_errors[:10]:
            print(f"     ✗ {err}")
        if len(general_errors) > 10:
            print(f"     ... and {len(general_errors) - 10} more")


if __name__ == "__main__":
    sys.exit(main())
