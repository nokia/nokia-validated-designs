"""
Populate NetBox with the ``dc1`` 3-stage-evpn-vxlan sample fabric.

The seed loads ``validated-designs/3-stage-evpn-vxlan/inputs/*.yaml``,
expands it through the design-specific builder (which assigns ASNs, ISL
port allocations, default routing policies, configlets, etc.), then
writes the resulting ``FabricIntent`` into NetBox via
``automation.builders.netbox_writer.write``.

This makes NetBox a lossless mirror of whatever the YAML would have
produced, so the NetBox path and YAML path can feed identical intents
into the downstream generators and executors.

Entry point:
    python -m automation.builders.netbox_seed_dc1_3stage \\
        --netbox-url https://srv9002 \\
        --netbox-token $NETBOX_TOKEN
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from automation.builders.netbox_client import make_client
from automation.builders.netbox_writer import write as write_intent
from automation.core.fabric_builder import build_intent
from automation.core.schema_validator import load_inputs

DEFAULT_DESIGN_DIR = Path("validated-designs/3-stage-evpn-vxlan")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed NetBox with the dc1 3-stage-evpn-vxlan sample fabric"
    )
    parser.add_argument("--netbox-url")
    parser.add_argument("--netbox-token")
    parser.add_argument("--netbox-verify", action="store_true", default=False)
    parser.add_argument(
        "--design-dir",
        default=str(DEFAULT_DESIGN_DIR),
        help=f"Path to the YAML inputs (default: {DEFAULT_DESIGN_DIR})",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    design_dir = Path(args.design_dir)
    if not design_dir.exists():
        logging.error("Design directory not found: %s", design_dir)
        return 1

    logging.info("Loading YAML inputs from %s", design_dir)
    topology, services = load_inputs(design_dir)
    intent = build_intent(topology, services)
    logging.info(
        "Expanded intent: %d nodes, %d links, %d bridge_domains, %d routers, %d lags",
        len(intent.nodes), len(intent.links), len(intent.bridge_domains),
        len(intent.routers), len(intent.lags),
    )

    nb = make_client(
        url=args.netbox_url,
        token=args.netbox_token,
        verify_tls=args.netbox_verify,
    )

    logging.info("Writing intent to NetBox site %s ...", intent.fabric_name)
    counts = write_intent(nb, intent)
    logging.info(
        "Seed complete: site=%s devices=%s interfaces=%s irbs=%s routed_interfaces=%s "
        "cables=%s vrfs=%s vlan_groups=%s vlans=%s prefixes=%s",
        intent.fabric_name, counts.get("devices", 0), counts.get("interfaces", 0),
        counts.get("irbs", 0), counts.get("routed_interfaces", 0),
        counts.get("cables", 0), counts.get("vrfs", 0),
        counts.get("vlan_groups", 0), counts.get("vlans", 0),
        counts.get("prefixes", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
