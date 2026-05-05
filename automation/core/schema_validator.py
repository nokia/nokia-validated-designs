"""
Schema validator for NVD input files.

Validates topology.yaml and services.yaml against their respective
JSON Schema definitions stored in the design's schemas/ directory.

Inputs may also be split across multiple YAML fragments under
``inputs/topology.d/`` and ``inputs/services.d/``. Fragments are loaded
lexicographically by filename, deep-merged with by-name list semantics,
and the merged result is validated against the strict schema.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path

import jsonschema
import yaml

logger = logging.getLogger(__name__)


# Top-level keys whose values must agree across all fragments. A mismatch
# is fatal (you can't half-build two designs into one intent).
TOPOLOGY_IDENTITY_KEYS: tuple[str, ...] = ("design", "fabric_name")
SERVICES_IDENTITY_KEYS: tuple[str, ...] = ()

# Fragment filename ordering convention: "NN-name.yaml". Files without the
# numeric prefix still load (lexicographic order applies) but produce a
# warning so the operator knows the order may be ambiguous.
_NN_PREFIX_RE = re.compile(r"^[0-9]+[-_]")


# ---------------------------------------------------------------------------
# Single-file primitives (kept for backward compat and direct use)
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    """Load and parse a YAML file.

    An empty file (or one containing only comments / a single ``null``)
    parses as an empty mapping. This makes all-commented placeholder
    fragments under ``*.d/`` directories valid by default.
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
    return data


def load_json_schema(path: Path) -> dict:
    """Load a JSON Schema file."""
    with open(path) as f:
        return json.load(f)


def validate_input(data: dict, schema_path: Path) -> None:
    """Validate a data dict against a JSON Schema file.

    Raises jsonschema.ValidationError on failure.
    """
    schema = load_json_schema(schema_path)
    jsonschema.validate(instance=data, schema=schema)
    logger.info("Validation passed against %s", schema_path.name)


def load_and_validate(yaml_path: Path, schema_path: Path) -> dict:
    """Load a YAML input file and validate it against its schema."""
    data = load_yaml(yaml_path)
    validate_input(data, schema_path)
    return data


# ---------------------------------------------------------------------------
# Fragment merging
# ---------------------------------------------------------------------------


def merge_fragments(
    fragments: list[tuple[str, dict]],
    *,
    identity_keys: tuple[str, ...] = (),
) -> dict:
    """Deep-merge a list of (label, data) fragments in order.

    Merge rules:

    * Dicts are merged recursively.
    * Lists of dicts are merged by ``name`` field when present (later entries
      replace earlier ones with the same name; entries without ``name`` are
      appended). Item order is otherwise preserved.
    * Other lists (e.g. plain string lists) are replaced wholesale.
    * Scalars are replaced wholesale.
    * Conflicts on top-level ``identity_keys`` raise ``ValueError``.
    * All other overrides log a warning identifying the source fragment.
    """
    merged: dict = {}
    for label, frag in fragments:
        merged = _merge_into(merged, frag, label, identity_keys, breadcrumb="")
    return merged


def _merge_into(
    base,
    override,
    label: str,
    identity_keys: tuple[str, ...],
    breadcrumb: str,
):
    if isinstance(base, dict) and isinstance(override, dict):
        for k, v in override.items():
            sub = f"{breadcrumb}.{k}" if breadcrumb else k
            if k in base:
                if breadcrumb == "" and k in identity_keys:
                    if base[k] != v:
                        raise ValueError(
                            f"identity key '{k}' conflict: '{base[k]}' (earlier) "
                            f"vs '{v}' (in {label})"
                        )
                    continue
                base[k] = _merge_into(base[k], v, label, identity_keys, sub)
            else:
                base[k] = v
        return base

    if isinstance(base, list) and isinstance(override, list):
        if _list_has_named_items(base) or _list_has_named_items(override):
            return _merge_lists_by_name(base, override, label, breadcrumb)
        if base != override:
            logger.warning(
                "Replacing list at '%s' from %s (was %d items, now %d items)",
                breadcrumb, label, len(base), len(override),
            )
        return list(override)

    if base != override:
        logger.warning(
            "Overriding '%s' from %s (%r -> %r)",
            breadcrumb, label, base, override,
        )
    return override


def _list_has_named_items(items: list) -> bool:
    return any(isinstance(x, dict) and "name" in x for x in items)


def _merge_lists_by_name(
    base: list,
    override: list,
    label: str,
    breadcrumb: str,
) -> list:
    by_name: dict[str, dict] = {}
    order: list[str] = []
    keyless: list = []

    for item in base:
        if isinstance(item, dict) and "name" in item:
            name = item["name"]
            if name not in by_name:
                order.append(name)
            by_name[name] = item
        else:
            keyless.append(item)

    for item in override:
        if isinstance(item, dict) and "name" in item:
            name = item["name"]
            if name in by_name:
                logger.warning(
                    "Overriding '%s[name=%s]' from %s",
                    breadcrumb, name, label,
                )
            else:
                order.append(name)
            by_name[name] = item
        else:
            keyless.append(item)

    return [by_name[n] for n in order] + keyless


# ---------------------------------------------------------------------------
# Fragment-aware loader
# ---------------------------------------------------------------------------


def make_fragment_schema(strict: dict) -> dict:
    """Return a copy of ``strict`` with top-level ``required`` removed.

    Sub-object ``required`` constraints are kept — a fragment that supplies
    ``underlay:`` must still include all required underlay keys, but a
    fragment that omits ``underlay:`` entirely is fine.

    The fragment schema also gets a ``$id`` and ``title`` suffixed with
    "(fragment)" so on-disk copies declare their relationship clearly when
    referenced from YAML language server directives.
    """
    frag = copy.deepcopy(strict)
    frag.pop("required", None)
    if "$id" in frag:
        frag["$id"] = f"{frag['$id']}.fragment"
    if "title" in frag:
        frag["title"] = f"{frag['title']} (fragment)"
    if "description" in frag:
        frag["description"] = (
            f"{frag['description']} "
            "Fragment variant: top-level required keys removed so individual "
            "fragments under inputs/*.d/ validate cleanly. The merged result "
            "is still validated against the strict schema."
        )
    return frag


def _discover_fragments(d_dir: Path) -> list[Path]:
    files = sorted(
        list(d_dir.glob("*.yaml")) + list(d_dir.glob("*.yml")),
        key=lambda p: p.name,
    )
    return files


def _load_topic(
    *,
    single_file: Path,
    d_dir: Path,
    schema_file: Path,
    kind: str,
    identity_keys: tuple[str, ...],
) -> dict:
    has_single = single_file.is_file()
    has_d = d_dir.is_dir()

    if has_single and has_d:
        raise ValueError(
            f"Both {single_file.name} and {d_dir.name}/ exist for {kind}; "
            f"use one or the other (delete or rename to disambiguate)"
        )
    if not has_single and not has_d:
        raise FileNotFoundError(
            f"No {kind} input found: expected {single_file} or {d_dir}/"
        )

    if has_single:
        if schema_file.exists():
            return load_and_validate(single_file, schema_file)
        logger.warning(
            "No %s schema at %s, skipping validation", kind, schema_file
        )
        return load_yaml(single_file)

    fragments = _discover_fragments(d_dir)
    if not fragments:
        raise FileNotFoundError(
            f"No YAML fragments found in {d_dir}/ (expected *.yaml or *.yml)"
        )

    schema = load_json_schema(schema_file) if schema_file.exists() else None
    fragment_schema_file = schema_file.with_name(
        schema_file.stem.replace("_schema", "_fragment_schema") + schema_file.suffix
    )
    if fragment_schema_file.exists():
        fragment_schema = load_json_schema(fragment_schema_file)
    elif schema:
        fragment_schema = make_fragment_schema(schema)
    else:
        fragment_schema = None

    loaded: list[tuple[str, dict]] = []
    for path in fragments:
        if not _NN_PREFIX_RE.match(path.name):
            logger.warning(
                "%s/%s has no numeric prefix; lexicographic order may be ambiguous",
                d_dir.name, path.name,
            )
        data = load_yaml(path)
        if fragment_schema is not None:
            try:
                jsonschema.validate(instance=data, schema=fragment_schema)
            except jsonschema.ValidationError as e:
                raise jsonschema.ValidationError(
                    f"Fragment {d_dir.name}/{path.name}: {e.message}",
                    path=e.path,
                    schema_path=e.schema_path,
                ) from e
        loaded.append((f"{d_dir.name}/{path.name}", data))

    logger.info(
        "Loaded %d %s fragments: %s",
        len(loaded), kind,
        ", ".join(label for label, _ in loaded),
    )

    merged = merge_fragments(loaded, identity_keys=identity_keys)

    if schema is not None:
        try:
            jsonschema.validate(instance=merged, schema=schema)
        except jsonschema.ValidationError as e:
            raise jsonschema.ValidationError(
                f"Merged {kind} fragments fail strict validation: {e.message}",
                path=e.path,
                schema_path=e.schema_path,
            ) from e
        logger.info(
            "Merged %s validated against %s", kind, schema_file.name
        )

    return merged


def load_inputs(design_dir: Path) -> tuple[dict, dict]:
    """Load and validate topology + services inputs for a design.

    Two layouts are supported (per topic, independently):

    * **Single file** — ``inputs/topology.yaml`` and/or ``inputs/services.yaml``.
    * **Fragment directory** — ``inputs/topology.d/*.yaml`` and/or
      ``inputs/services.d/*.yaml``, loaded in lexicographic filename order
      and deep-merged with by-name list semantics.

    A single topic cannot use both layouts at once; mixing across topics is
    fine (e.g. monolithic topology + fragmented services).

    Args:
        design_dir: Path to the design directory
            (e.g. validated-designs/3-stage-evpn-vxlan).

    Returns:
        Tuple of (topology_data, services_data).
    """
    inputs_dir = design_dir / "inputs"
    schemas_dir = design_dir / "schemas"

    topo = _load_topic(
        single_file=inputs_dir / "topology.yaml",
        d_dir=inputs_dir / "topology.d",
        schema_file=schemas_dir / "topology_schema.json",
        kind="topology",
        identity_keys=TOPOLOGY_IDENTITY_KEYS,
    )
    svc = _load_topic(
        single_file=inputs_dir / "services.yaml",
        d_dir=inputs_dir / "services.d",
        schema_file=schemas_dir / "services_schema.json",
        kind="services",
        identity_keys=SERVICES_IDENTITY_KEYS,
    )
    return topo, svc
