"""
NVD Automation Engine — CLI entry point.

Usage:
  python -m automation.deploy \\
    --design validated-designs/3-stage-evpn-vxlan \\
    --mode eda \\
    [--generate-only]
    [--generate-clab]
    [--phase {topology,fabric,services}]
    [--destroy]
    [--dry-run]
    [--prune]
    [--yes]
    [--eda-url URL]
    [--eda-user USER]
    [--eda-password PASS]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs
from automation.generators.eda_generator import generate as eda_generate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nokia Validated Design automation engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--design",
        required=True,
        help="Path to the design directory (e.g. validated-designs/3-stage-evpn-vxlan)",
    )
    parser.add_argument(
        "--mode",
        choices=["eda", "ansible"],
        default="eda",
        help="Deployment mode (default: eda)",
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
        help="Run only a specific deployment phase (repeatable, e.g. --phase topology --phase fabric)",
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
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    design_dir = Path(args.design)
    if not design_dir.exists():
        logging.error("Design directory not found: %s", design_dir)
        return 1

    # ---------------------------------------------------------------
    # Phase 1: Load & validate inputs
    # ---------------------------------------------------------------
    logging.info("Loading inputs from %s", design_dir)
    try:
        topology, services = load_inputs(design_dir)
    except Exception as e:
        logging.error("Failed to load inputs: %s", e)
        return 1

    logging.info(
        "Design: %s | Fabric: %s | Env: %s",
        topology.get("design"),
        topology.get("fabric_name"),
        topology.get("environment"),
    )

    # ---------------------------------------------------------------
    # Phase 2: Build FabricIntent
    # ---------------------------------------------------------------
    logging.info("Building fabric intent...")
    try:
        intent = build_intent(topology, services)
    except Exception as e:
        logging.error("Failed to build intent: %s", e)
        return 1

    logging.info(
        "Built intent: %d nodes, %d links, %d bridge_domains, %d routers",
        len(intent.nodes),
        len(intent.links),
        len(intent.bridge_domains),
        len(intent.routers),
    )

    # ---------------------------------------------------------------
    # Phase 3: Generate target configs
    # ---------------------------------------------------------------
    build_dir = design_dir / "build"

    # ---------------------------------------------------------------
    # Optional: Generate containerlab topology
    # ---------------------------------------------------------------
    if args.generate_clab:
        from automation.generators.clab_generator import generate as clab_generate

        logging.info("Generating containerlab topology...")
        clab_path = clab_generate(intent, output_dir=build_dir)
        print(f"\n✅ Generated containerlab topology: {clab_path}")
        print(f"   Client configs: {build_dir}/client-configs/")
        if not args.generate_only and args.mode != "eda":
            return 0

    if args.mode == "eda":
        # -----------------------------------------------------------
        # Destroy mode — no intent/generation needed
        # -----------------------------------------------------------
        if args.destroy:
            from automation.executors.eda import EdaClient

            try:
                client = EdaClient(
                    url=args.eda_url,
                    username=args.eda_user,
                    password=args.eda_password,
                )
            except ValueError as e:
                logging.error(str(e))
                return 1

            logging.info("Destroying managed resources at %s...", client.url)
            result = client.destroy(
                phases=args.phase,
                dry_run=args.dry_run,
                auto_confirm=args.yes,
            )

            if result.success:
                label = "Dry-run" if args.dry_run else "Destroy"
                print(f"\n✅ {label} successful: {result.message}")
                if result.transaction_id:
                    print(f"   Transaction ID: {result.transaction_id}")
                _print_transaction_details(result.details)
                return 0
            else:
                print(f"\n❌ Destroy failed: {result.message}")
                _print_transaction_details(result.details)
                return 1

        # -----------------------------------------------------------
        # Normal deploy flow — generate + apply
        # -----------------------------------------------------------
        logging.info("Generating EDA CRs...")
        resources = eda_generate(intent, output_dir=build_dir)
        logging.info("Generated %d EDA CRs → %s", len(resources), build_dir)

        if args.generate_only:
            print(f"\n✅ Generated {len(resources)} EDA CRs to {build_dir}/eda_transaction.json")
            _print_summary(resources)
            return 0

        # -----------------------------------------------------------
        # Deploy to EDA
        # -----------------------------------------------------------
        from automation.executors.eda import EdaClient

        try:
            client = EdaClient(
                url=args.eda_url,
                username=args.eda_user,
                password=args.eda_password,
            )
        except ValueError as e:
            logging.error(str(e))
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

        if result.success:
            label = "Dry-run" if args.dry_run else "Deployment"
            print(f"\n✅ {label} successful: {result.message}")
            if result.transaction_id:
                print(f"   Transaction ID: {result.transaction_id}")
            _print_transaction_details(result.details)
            return 0
        else:
            print(f"\n❌ Deployment failed: {result.message}")
            _print_transaction_details(result.details)
            return 1

    elif args.mode == "ansible":
        from automation.generators.ansible_generator import generate as ansible_generate

        ansible_dir = design_dir / f"{design_dir.name}-ansible"
        logging.info("Generating Ansible project...")
        output_path = ansible_generate(intent, output_dir=ansible_dir)
        print(f"\n✅ Generated Ansible project to {output_path}")
        print(f"\n   Quick start:")
        print(f"   cd {output_path}")
        print(f"   ansible-galaxy collection install -r requirements.yml")
        print(f"   ansible-playbook -i inventory.yml playbook.yml")
        return 0

    return 0


def _print_summary(resources: list[dict]) -> None:
    """Print a summary of generated CRs by kind."""
    kinds: dict[str, int] = {}
    for cr in resources:
        kind = cr.get("kind", "Unknown")
        kinds[kind] = kinds.get(kind, 0) + 1

    print("\nResource summary:")
    for kind in sorted(kinds.keys()):
        print(f"  {kind}: {kinds[kind]}")

def _print_transaction_details(details: dict) -> None:
    """Print verbose human-readable transaction details."""
    if not details:
        return

    # Execution summary
    exec_summary = details.get("executionSummary", "")
    if exec_summary:
        print(f"\n   Execution: {exec_summary}")

    # Nodes with config changes
    nodes = details.get("nodesWithConfigChanges") or []
    if nodes:
        print(f"\n   Nodes affected ({len(nodes)}):")
        for node in nodes:
            print(f"     • {node}")

    # Changed CRs
    changed = details.get("changedCrs") or []
    if changed:
        print(f"\n   Changed resources:")
        for group in changed:
            kind = group.get("gvk", {}).get("kind", "?")
            names = group.get("names", [])
            ns = group.get("namespace", "")
            for name in names:
                print(f"     • {kind}/{name} ({ns})")

    # Intent errors
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

    # General errors
    general_errors = details.get("generalErrors") or []
    if general_errors:
        print(f"\n   General errors ({len(general_errors)}):")
        for err in general_errors[:10]:
            print(f"     ✗ {err}")
        if len(general_errors) > 10:
            print(f"     ... and {len(general_errors) - 10} more")


if __name__ == "__main__":
    sys.exit(main())
