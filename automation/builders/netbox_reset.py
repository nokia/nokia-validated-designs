"""
Tear down NVD-managed content in NetBox to restore a clean slate between
test runs.

The reset is tag-scoped: every object seed/setup creates carries the
``nvd-managed`` tag and every custom field follows the ``nvd_`` prefix.
This script finds and deletes exactly those objects — it never touches
users, tokens, custom unrelated content, or NetBox's own configuration.

Two scopes:

- ``--scope data`` (default): remove seeded topology / service content —
  cables, interfaces, devices, IP addresses, prefixes, VLANs, VLAN
  groups, VRFs, sites, and any config contexts tagged ``nvd-managed``.
- ``--scope all``: also remove the NVD custom fields, choice sets, tags,
  device roles, platforms, manufacturer, and device types created by
  ``netbox_setup``.

Cascading order is explicit so foreign-key constraints never block the
delete: cables → interfaces → devices → IP addresses → prefixes →
VLANs → VLAN groups → VRFs → sites.

Safety:
- ``--dry-run`` prints what would be deleted without changing anything.
- Without ``--yes`` the script prompts before touching data.
- A site-slug filter ``--site SLUG`` restricts ``data`` scope to objects
  in that site.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from automation.builders.netbox_client import make_client
from automation.builders.netbox_schema import (
    ALL_NVD_TAGS,
    CUSTOM_FIELDS,
    NVD_DEVICE_ROLES,
    NVD_DEVICE_TYPES,
    NVD_MANAGED_TAG,
    NVD_MANUFACTURER,
    NVD_PLATFORM,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove NVD-managed content from NetBox"
    )
    parser.add_argument("--netbox-url")
    parser.add_argument("--netbox-token")
    parser.add_argument("--netbox-verify", action="store_true", default=False)
    parser.add_argument(
        "--scope", choices=["data", "all"], default="data",
        help="What to remove (default: data)",
    )
    parser.add_argument(
        "--site",
        help="Restrict data deletion to a specific site slug",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted without making any changes",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    nb = make_client(
        url=args.netbox_url,
        token=args.netbox_token,
        verify_tls=args.netbox_verify,
    )

    plan = _plan(nb, scope=args.scope, site_slug=args.site)
    _print_plan(plan)
    total = sum(len(v) for v in plan.values())
    if total == 0:
        logger.info("Nothing to delete.")
        return 0

    if args.dry_run:
        logger.info("--dry-run: no changes made")
        return 0

    if not args.yes:
        prompt = (
            f"\nAbout to DELETE {total} objects from {nb._nvd_base_url}. "
            "Type 'YES' to continue: "
        )
        answer = input(prompt)
        if answer.strip() != "YES":
            logger.info("Aborted by user")
            return 1

    _execute(plan)
    logger.info("Reset complete.")
    return 0


# ---------------------------------------------------------------------------
# Planning (what would be deleted, in cascading order)
# ---------------------------------------------------------------------------


def _plan(nb: Any, *, scope: str, site_slug: str | None) -> dict[str, list[Any]]:
    sites = list(nb.dcim.sites.filter(tag=NVD_MANAGED_TAG, limit=0))
    if site_slug:
        sites = [s for s in sites if s.slug == site_slug]
    site_ids = [s.id for s in sites]

    plan: dict[str, list[Any]] = {
        "cables": [],
        "ip_addresses": [],
        "interfaces": [],
        "devices": [],
        "prefixes": [],
        "vlans": [],
        "vlan_groups": [],
        "vrfs": [],
        "sites": list(sites),
        "config_contexts": [],
    }

    # Cables tagged nvd-managed but cascading under sites (filter by device site)
    if site_ids:
        plan["cables"] = list(nb.dcim.cables.filter(
            tag=NVD_MANAGED_TAG, site_id=site_ids, limit=0,
        ))
        plan["devices"] = list(nb.dcim.devices.filter(
            tag=NVD_MANAGED_TAG, site_id=site_ids, limit=0,
        ))
        device_ids = [d.id for d in plan["devices"]]
        if device_ids:
            plan["interfaces"] = list(nb.dcim.interfaces.filter(
                tag=NVD_MANAGED_TAG, device_id=device_ids, limit=0,
            ))
            plan["ip_addresses"] = list(nb.ipam.ip_addresses.filter(
                tag=NVD_MANAGED_TAG, device_id=device_ids, limit=0,
            ))
        plan["prefixes"] = list(nb.ipam.prefixes.filter(
            tag=NVD_MANAGED_TAG, site_id=site_ids, limit=0,
        ))
        plan["vlans"] = list(nb.ipam.vlans.filter(
            tag=NVD_MANAGED_TAG, site_id=site_ids, limit=0,
        ))
        plan["vlan_groups"] = list(nb.ipam.vlan_groups.filter(
            tag=NVD_MANAGED_TAG, limit=0,
        ))
        # VRFs aren't site-scoped natively: take any VRF tagged nvd-managed.
        plan["vrfs"] = list(nb.ipam.vrfs.filter(
            tag=NVD_MANAGED_TAG, limit=0,
        ))

    # Config contexts tagged nvd-managed (no site filter — they're global).
    try:
        plan["config_contexts"] = list(
            nb.extras.config_contexts.filter(tag=NVD_MANAGED_TAG, limit=0)
        )
    except Exception:
        pass

    if scope == "all":
        plan["custom_fields"] = _nvd_custom_fields(nb)
        plan["choice_sets"] = _nvd_choice_sets(nb)
        plan["device_roles"] = _unused_nvd_roles(nb)
        plan["device_types"] = _unused_nvd_device_types(nb)
        plan["platforms"] = _unused_nvd_platforms(nb)
        plan["manufacturers"] = _unused_nvd_manufacturers(nb)
        plan["tags"] = _nvd_tags(nb)

    return plan


def _nvd_custom_fields(nb: Any) -> list[Any]:
    expected = {spec["name"] for spec in CUSTOM_FIELDS}
    out: list[Any] = []
    for cf in nb.extras.custom_fields.all():
        if cf.name in expected or cf.name.startswith("nvd_"):
            out.append(cf)
    return out


def _nvd_choice_sets(nb: Any) -> list[Any]:
    out: list[Any] = []
    for attr in ("custom_field_choice_sets", "custom_field_choices"):
        endpoint = getattr(nb.extras, attr, None)
        if endpoint is None:
            continue
        for cs in endpoint.all():
            name = getattr(cs, "name", "")
            if name.startswith("nvd-"):
                out.append(cs)
        break
    return out


def _unused_nvd_roles(nb: Any) -> list[Any]:
    result: list[Any] = []
    for slug, _, _ in NVD_DEVICE_ROLES:
        role = nb.dcim.device_roles.get(slug=slug)
        if role is None:
            continue
        count = nb.dcim.devices.filter(role_id=role.id, limit=1)
        if list(count):
            continue
        result.append(role)
    return result


def _unused_nvd_device_types(nb: Any) -> list[Any]:
    result: list[Any] = []
    for slug, _, _ in NVD_DEVICE_TYPES:
        dt = nb.dcim.device_types.get(slug=slug)
        if dt is None:
            continue
        if list(nb.dcim.devices.filter(device_type_id=dt.id, limit=1)):
            continue
        result.append(dt)
    return result


def _unused_nvd_platforms(nb: Any) -> list[Any]:
    result: list[Any] = []
    for plat in nb.dcim.platforms.filter(slug=NVD_PLATFORM[0], limit=0):
        if list(nb.dcim.devices.filter(platform_id=plat.id, limit=1)):
            continue
        result.append(plat)
    return result


def _unused_nvd_manufacturers(nb: Any) -> list[Any]:
    result: list[Any] = []
    manu = nb.dcim.manufacturers.get(slug=NVD_MANUFACTURER[0])
    if manu is None:
        return result
    if list(nb.dcim.device_types.filter(manufacturer_id=manu.id, limit=1)):
        return result
    result.append(manu)
    return result


def _nvd_tags(nb: Any) -> list[Any]:
    expected = {slug for slug, _, _ in ALL_NVD_TAGS}
    out: list[Any] = []
    for tag in nb.extras.tags.all():
        if tag.slug in expected or tag.slug.startswith("nvd-"):
            out.append(tag)
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


DELETE_ORDER = [
    "cables",
    "ip_addresses",
    "interfaces",
    "devices",
    "prefixes",
    "vlans",
    "vlan_groups",
    "vrfs",
    "config_contexts",
    "sites",
    "device_types",
    "device_roles",
    "platforms",
    "manufacturers",
    "custom_fields",
    "choice_sets",
    "tags",
]


def _print_plan(plan: dict[str, list[Any]]) -> None:
    logger.info("Deletion plan:")
    for key in DELETE_ORDER:
        items = plan.get(key, [])
        if not items:
            continue
        logger.info("  %s: %d", key, len(items))
        for item in items[:25]:
            name = getattr(item, "name", None) or getattr(item, "slug", None) or getattr(item, "id", "?")
            logger.debug("    • %s", name)
        if len(items) > 25:
            logger.debug("    ... and %d more", len(items) - 25)


def _execute(plan: dict[str, list[Any]]) -> None:
    for key in DELETE_ORDER:
        items = plan.get(key, [])
        if not items:
            continue
        logger.info("Deleting %d %s ...", len(items), key)
        for item in items:
            try:
                item.delete()
            except Exception as e:
                name = getattr(item, "name", None) or getattr(item, "slug", None) or getattr(item, "id", "?")
                logger.warning("  failed to delete %s/%s: %s", key, name, e)


if __name__ == "__main__":
    sys.exit(main())
