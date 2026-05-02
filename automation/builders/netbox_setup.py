"""
Idempotent NetBox setup — creates all custom fields, tags, device roles,
manufacturer, platform, and standard device types required by the NVD
NetBox builder/seed/reset workflow.

Runs safely against an empty NetBox and against an already-bootstrapped
NetBox (every object is looked up before create, and existing objects
are only updated with additive fields).

Entry point:
    python -m automation.builders.netbox_setup \
        --netbox-url https://srv9002 --netbox-token $NETBOX_TOKEN

Environment variables are read as fallbacks for both arguments.
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
        description="Bootstrap NetBox with NVD custom fields, tags, roles, platforms"
    )
    parser.add_argument("--netbox-url")
    parser.add_argument("--netbox-token")
    parser.add_argument(
        "--netbox-verify", action="store_true", default=False,
        help="Verify NetBox TLS certificate (default: off)"
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

    logger.info("Ensuring NVD tags ...")
    _ensure_tags(nb)

    logger.info("Ensuring NVD custom fields ...")
    _ensure_custom_fields(nb)

    logger.info("Ensuring manufacturer / platform / device types ...")
    _ensure_manufacturer(nb)
    _ensure_platform(nb)
    _ensure_device_types(nb)

    logger.info("Ensuring device roles ...")
    _ensure_device_roles(nb)

    logger.info("Setup complete — NetBox is ready for seed + deploy.")
    return 0


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def _ensure_tags(nb: Any) -> None:
    for slug, name, description in ALL_NVD_TAGS:
        existing = nb.extras.tags.get(slug=slug)
        if existing is not None:
            logger.debug("  tag '%s' already exists", slug)
            continue
        nb.extras.tags.create(name=name, slug=slug, description=description, color="9e9e9e")
        logger.info("  created tag '%s'", slug)


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


def _ensure_custom_fields(nb: Any) -> None:
    """Create or update NVD custom fields.

    NetBox 4.x expects selection fields to be backed by a CustomFieldChoiceSet
    — rather than create one per field, we embed the choices inline by
    using the ``choice_set`` via a pre-created set per field. To keep this
    simple and dependency-free we first create ChoiceSets for each select
    field, then reference them from the custom field payload.
    """
    choice_sets = _ensure_choice_sets(nb)

    # Collapse duplicate field names that are declared for multiple object
    # types (e.g. nvd_labels on both Device and Interface) into a single
    # spec with the union of object_types.
    collapsed: dict[str, dict[str, Any]] = {}
    for spec in CUSTOM_FIELDS:
        name = spec["name"]  # type: ignore[index]
        if name in collapsed:
            existing_spec = collapsed[name]
            existing_spec["object_types"] = sorted(set(
                list(existing_spec["object_types"]) + list(spec["object_types"])  # type: ignore[list-item]
            ))
        else:
            collapsed[name] = dict(spec)

    for spec in collapsed.values():
        name: str = spec["name"]  # type: ignore[assignment]
        existing = nb.extras.custom_fields.get(name=name)

        create_payload: dict[str, Any] = {
            "name": name,
            "label": spec.get("label", name),
            "type": spec["type"],
            "description": spec.get("description", "") or "",
            "object_types": spec["object_types"],
            "required": spec.get("required", False),
        }
        if spec["type"] == "select":
            cs_id = choice_sets.get(name)
            if cs_id is None:
                raise RuntimeError(
                    f"missing choice set for custom field '{name}'"
                )
            create_payload["choice_set"] = cs_id
        if spec.get("default") is not None:
            create_payload["default"] = spec["default"]

        if existing is None:
            nb.extras.custom_fields.create(**create_payload)
            logger.info("  created custom field '%s'", name)
            continue

        # Update path: NetBox rejects `type` changes, so only PATCH fields
        # that are genuinely mutable and that actually differ.
        existing_type = getattr(existing, "type", None)
        existing_type_val = (
            getattr(existing_type, "value", existing_type)
            if existing_type is not None else None
        )
        if existing_type_val and existing_type_val != spec["type"]:
            logger.warning(
                "  custom field '%s' has type '%s' in NetBox but code "
                "expects '%s' — skipping update (delete/recreate manually "
                "if this is intentional)",
                name, existing_type_val, spec["type"],
            )
            continue

        patch: dict[str, Any] = {}
        if getattr(existing, "label", None) != create_payload["label"]:
            patch["label"] = create_payload["label"]
        if getattr(existing, "description", None) != create_payload["description"]:
            patch["description"] = create_payload["description"]
        if bool(getattr(existing, "required", False)) != bool(create_payload["required"]):
            patch["required"] = create_payload["required"]
        # object_types comparison: existing is list of strings; payload too
        existing_ot = list(getattr(existing, "object_types", []) or [])
        if sorted(existing_ot) != sorted(create_payload["object_types"]):
            patch["object_types"] = create_payload["object_types"]

        if patch:
            existing.update(patch)
            logger.info("  updated custom field '%s' (%s)", name, ", ".join(patch.keys()))
        else:
            logger.debug("  custom field '%s' already matches", name)


def _ensure_choice_sets(nb: Any) -> dict[str, int]:
    """Create a ChoiceSet for each select custom field and return a name→id map."""
    out: dict[str, int] = {}
    for spec in CUSTOM_FIELDS:
        if spec["type"] != "select":
            continue
        name: str = spec["name"]  # type: ignore[assignment]
        choices: list[str] = spec.get("choices") or []  # type: ignore[assignment]
        cs_name = f"nvd-{name.replace('nvd_', '')}-choices"

        # NetBox 4.x ChoiceSet: extras.custom_field_choice_sets
        existing = _find_choice_set(nb, cs_name)
        extra_choices = [[c, c] for c in choices]

        if existing is None:
            cs = nb.extras.custom_field_choice_sets.create(
                name=cs_name,
                description=f"Choices for {name}",
                extra_choices=extra_choices,
            )
            out[name] = cs.id
            logger.info("  created choice set '%s'", cs_name)
        else:
            if getattr(existing, "extra_choices", None) != extra_choices:
                existing.update({"extra_choices": extra_choices})
                logger.info("  updated choice set '%s'", cs_name)
            out[name] = existing.id
    return out


def _find_choice_set(nb: Any, name: str) -> Any:
    """NetBox 4.x: ``extras.custom_field_choice_sets``. Some versions expose it
    as ``custom_field_choices``; fall back gracefully."""
    for attr in ("custom_field_choice_sets", "custom_field_choices"):
        endpoint = getattr(nb.extras, attr, None)
        if endpoint is None:
            continue
        try:
            return endpoint.get(name=name)
        except Exception:
            return next((cs for cs in endpoint.all() if cs.name == name), None)
    raise RuntimeError(
        "NetBox API does not expose custom_field_choice_sets — please "
        "upgrade to NetBox 4.x"
    )


# ---------------------------------------------------------------------------
# Manufacturer / platform / device types / roles
# ---------------------------------------------------------------------------


def _ensure_manufacturer(nb: Any) -> None:
    slug, name = NVD_MANUFACTURER
    existing = nb.dcim.manufacturers.get(slug=slug)
    if existing is None:
        nb.dcim.manufacturers.create(name=name, slug=slug)
        logger.info("  created manufacturer '%s'", slug)
    else:
        logger.debug("  manufacturer '%s' already exists", slug)


def _ensure_platform(nb: Any) -> None:
    slug, name, _ = NVD_PLATFORM
    manu = nb.dcim.manufacturers.get(slug=NVD_MANUFACTURER[0])
    if manu is None:
        raise RuntimeError("manufacturer not created — cannot link platform")
    existing = nb.dcim.platforms.get(slug=slug)
    if existing is None:
        nb.dcim.platforms.create(name=name, slug=slug, manufacturer=manu.id)
        logger.info("  created platform '%s'", slug)
    else:
        logger.debug("  platform '%s' already exists", slug)


def _ensure_device_types(nb: Any) -> None:
    manu = nb.dcim.manufacturers.get(slug=NVD_MANUFACTURER[0])
    if manu is None:
        raise RuntimeError("manufacturer not created — cannot create device types")
    for slug, model, part_number in NVD_DEVICE_TYPES:
        existing = nb.dcim.device_types.get(slug=slug)
        if existing is None:
            nb.dcim.device_types.create(
                manufacturer=manu.id,
                model=model,
                slug=slug,
                part_number=part_number,
                u_height=1,
                tags=[{"slug": NVD_MANAGED_TAG}],
            )
            logger.info("  created device type '%s' (%s)", slug, model)
        else:
            logger.debug("  device type '%s' already exists", slug)


def _ensure_device_roles(nb: Any) -> None:
    for slug, name, color in NVD_DEVICE_ROLES:
        existing = nb.dcim.device_roles.get(slug=slug)
        if existing is None:
            nb.dcim.device_roles.create(name=name, slug=slug, color=color)
            logger.info("  created device role '%s'", slug)
        else:
            logger.debug("  device role '%s' already exists", slug)


if __name__ == "__main__":
    sys.exit(main())
