"""
EDA Pydantic Model Generator.

Reads EDA OpenAPI v3 specs and generates clean Pydantic v2 models
with proper class names, camelCase aliases, and field constraints.

Usage:
    python -m automation.codegen.generate_models

Output:
    automation/eda_models/{group}.py   — one file per API group
    automation/eda_models/__init__.py  — re-exports all models
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

SPEC_DIR = Path(__file__).parent / "openapi_specs"
OUT_DIR = Path(__file__).parent.parent / "eda_models"

# Map of OpenAPI spec file → (module_name, {Kind: fully_qualified_schema_key_prefix})
# Only resources we actually use in the deployer
RESOURCE_MAP: dict[str, tuple[str, list[str]]] = {
    "services_eda_nokia_com_v1.json": (
        "services",
        ["BridgeDomain", "Router", "IRBInterface", "VLAN", "RoutedInterface"],
    ),
    "core_eda_nokia_com_v1.json": (
        "core",
        [
            "TopoNode", "TopoLink", "NodeProfile", "NodeUser",
            "IndexAllocationPool", "IPAllocationPool",
        ],
    ),
    "fabrics_eda_nokia_com_v1alpha1.json": (
        "fabrics",
        ["Fabric"],
    ),
    "interfaces_eda_nokia_com_v1alpha1.json": (
        "interfaces",
        ["Interface"],
    ),
    "protocols_eda_nokia_com_v1.json": (
        "protocols",
        ["StaticRoute"],
    ),
    "config_eda_nokia_com_v1alpha1.json": (
        "config",
        ["Configlet"],
    ),
    "bootstrap_eda_nokia_com_v1alpha1.json": (
        "bootstrap",
        ["Init"],
    ),
    "routingpolicies_eda_nokia_com_v1alpha1.json": (
        "routingpolicies",
        ["Policy", "PrefixSet"],
    ),
}


# EDA 26.4.x (fresh install) resource map. ``services``/``protocols`` are v2 and
# every other group graduated v1alpha1 -> v1, with breaking spec shape changes.
# These models live in their own subpackage so the 25.12 (default) models are
# untouched.
RESOURCE_MAP_26_4: dict[str, tuple[str, list[str]]] = {
    "services_eda_nokia_com_v2.json": (
        "services",
        ["BridgeDomain", "Router", "IRBInterface", "VLAN", "RoutedInterface"],
    ),
    "core_eda_nokia_com_v1.json": (
        "core",
        [
            "TopoNode", "TopoLink", "NodeProfile", "NodeUser",
            "IndexAllocationPool", "IPAllocationPool",
        ],
    ),
    "fabrics_eda_nokia_com_v1.json": (
        "fabrics",
        ["Fabric"],
    ),
    "interfaces_eda_nokia_com_v1.json": (
        "interfaces",
        ["Interface"],
    ),
    "protocols_eda_nokia_com_v2.json": (
        "protocols",
        ["StaticRoute"],
    ),
    "config_eda_nokia_com_v1.json": (
        "config",
        ["Configlet"],
    ),
    "bootstrap_eda_nokia_com_v1.json": (
        "bootstrap",
        ["Init"],
    ),
    "routingpolicies_eda_nokia_com_v1.json": (
        "routingpolicies",
        ["Policy", "PrefixSet"],
    ),
    "siteinfo_eda_nokia_com_v1.json": (
        "siteinfo",
        ["DefaultMTU", "Banner"],
    ),
}


@dataclass(frozen=True)
class Target:
    """One model-generation target: a spec set rendered into a package dir."""

    name: str
    spec_dir: Path
    out_dir: Path
    resource_map: dict[str, tuple[str, list[str]]]


TARGETS: list[Target] = [
    Target("eda_25_12 (default)", SPEC_DIR, OUT_DIR, RESOURCE_MAP),
    Target("eda_26_4", SPEC_DIR / "eda_26_4", OUT_DIR / "eda_26_4", RESOURCE_MAP_26_4),
]


def _to_snake(name: str) -> str:
    """CamelCase → snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _python_type(prop: dict, class_prefix: str, nested: list[tuple[str, dict]]) -> str:
    """Map an OpenAPI property schema to a Python type string."""
    if "enum" in prop:
        vals = prop["enum"]
        return "Literal[" + ", ".join(repr(v) for v in vals) + "]"

    t = prop.get("type", "string")

    if t == "string":
        fmt = prop.get("format", "")
        return "str"
    elif t == "integer":
        return "int"
    elif t == "boolean":
        return "bool"
    elif t == "number":
        return "float"
    elif t == "array":
        items = prop.get("items", {})
        if items.get("type") == "object" and items.get("properties"):
            # Nested object array — create a sub-class
            sub_name = class_prefix + _to_class_name(prop.get("title", "Item"))
            nested.append((sub_name, items))
            return f"list[{sub_name}]"
        elif items.get("type") == "string":
            return "list[str]"
        elif items.get("type") == "integer":
            return "list[int]"
        elif items.get("type") == "boolean":
            return "list[bool]"
        elif "enum" in items:
            inner = "Literal[" + ", ".join(repr(v) for v in items["enum"]) + "]"
            return f"list[{inner}]"
        else:
            return "list[Any]"
    elif t == "object":
        if prop.get("properties"):
            sub_name = class_prefix + _to_class_name(prop.get("title", "Config"))
            nested.append((sub_name, prop))
            return sub_name
        else:
            return "dict[str, Any]"
    else:
        return "Any"


def _to_class_name(title: str) -> str:
    """Convert a title like 'L3 Proxy ARP/ND' → 'L3ProxyArpNd'."""
    # Remove special chars, split on spaces
    parts = re.sub(r"[^a-zA-Z0-9 ]", " ", title).split()
    return "".join(p.capitalize() for p in parts)


def _field_kwargs(name: str, prop: dict) -> str:
    """Build Field() kwargs string."""
    args = []

    # Alias (camelCase)
    snake = _to_snake(name)
    if snake != name:
        args.append(f'alias="{name}"')

    # Description
    desc = prop.get("description", "")
    if desc:
        # Escape backslashes and quotes for Python string literal
        desc = desc.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        args.append(f'description="{desc}"')

    # Title
    title = prop.get("title", "")
    if title:
        args.append(f'title="{title}"')

    # Constraints
    if "minimum" in prop:
        val = prop["minimum"]
        if val >= 0:
            args.append(f"ge={val}")
        else:
            args.append(f"ge={val}")
    if "maximum" in prop:
        args.append(f"le={prop['maximum']}")
    if "minLength" in prop:
        args.append(f"min_length={prop['minLength']}")
    if "maxLength" in prop:
        args.append(f"max_length={prop['maxLength']}")
    if "pattern" in prop:
        pat = prop["pattern"].replace('"', '\\"')
        args.append(f'pattern=r"{pat}"')

    return ", ".join(args)


def _nested_for(prop: dict, class_prefix: str) -> tuple[str, dict] | None:
    """Return ``(class_name, schema)`` for an inline object sub-schema, else None.

    Mirrors the nested-class naming used by :func:`_python_type` so the
    collection pass and the emission pass agree on class names.
    """
    if "enum" in prop:
        return None
    t = prop.get("type", "string")
    if t == "array":
        items = prop.get("items", {})
        if items.get("type") == "object" and items.get("properties"):
            return class_prefix + _to_class_name(prop.get("title", "Item")), items
        return None
    if t == "object" and prop.get("properties"):
        return class_prefix + _to_class_name(prop.get("title", "Config")), prop
    return None


def _collect_class(
    class_name: str,
    schema: dict,
    parent_prefix: str,
    collected: dict[str, dict],
    order: list[str],
) -> None:
    """Collect inline object schemas keyed by generated class name.

    When two inline sub-schemas resolve to the same class name (e.g. the ``bgp``
    block under both ``underlayProtocol`` and ``overlayProtocol`` both become
    ``FabricBgp``), their ``properties``/``required`` are **unioned** rather than
    one silently shadowing the other. This prevents fields that exist on only
    one side (e.g. ``asnPool`` on the underlay) from being dropped — the bug
    previously worked around by a hand-applied MANUAL PATCH.
    """
    entry = collected.get(class_name)
    if entry is None:
        entry = {"properties": {}, "required": set(), "prefix": parent_prefix}
        collected[class_name] = entry
        order.append(class_name)

    entry["required"] |= set(schema.get("required", []))
    for prop_name, prop in schema.get("properties", {}).items():
        # Union: first definition of a field wins for its type/kwargs; fields
        # unique to a later schema are appended.
        if prop_name not in entry["properties"]:
            entry["properties"][prop_name] = prop
        nested = _nested_for(prop, parent_prefix)
        if nested is not None:
            sub_name, sub_schema = nested
            _collect_class(sub_name, sub_schema, parent_prefix, collected, order)


def _emit_class(class_name: str, entry: dict) -> str:
    """Render a Pydantic model class from a collected (merged) schema entry."""
    props: dict = entry["properties"]
    required: set = entry["required"]
    prefix: str = entry["prefix"]

    lines = [f"class {class_name}(_EDABase):"]

    if not props:
        lines.append("    pass")
        return "\n".join(lines)

    throwaway: list[tuple[str, dict]] = []
    for prop_name, prop in props.items():
        snake = _to_snake(prop_name)
        py_type = _python_type(prop, prefix, throwaway)
        field_kwargs = _field_kwargs(prop_name, prop)
        is_required = prop_name in required
        default = prop.get("default")

        if is_required:
            if field_kwargs:
                lines.append(f"    {snake}: {py_type} = Field(..., {field_kwargs})")
            else:
                lines.append(f"    {snake}: {py_type}")
        else:
            # Optional with default
            if default is not None:
                default_repr = repr(default)
                if field_kwargs:
                    lines.append(
                        f"    {snake}: {py_type} | None = Field({default_repr}, {field_kwargs})"
                    )
                else:
                    lines.append(f"    {snake}: {py_type} | None = {default_repr}")
            else:
                if field_kwargs:
                    lines.append(
                        f"    {snake}: {py_type} | None = Field(None, {field_kwargs})"
                    )
                else:
                    lines.append(f"    {snake}: {py_type} | None = None")

    return "\n".join(lines)


def generate_module(
    spec_file: str, module_name: str, kinds: list[str], spec_dir: Path = SPEC_DIR
) -> str:
    """Generate a Python module with Pydantic models for the given resource kinds."""
    spec_path = spec_dir / spec_file
    with open(spec_path) as f:
        spec = json.load(f)

    schemas = spec.get("components", {}).get("schemas", {})

    # Collect inline schemas keyed by class name (unioning collisions), then emit.
    collected: dict[str, dict] = {}
    order: list[str] = []
    generated_kind_classes: list[str] = []

    for kind in kinds:
        # Find the matching schema key
        schema_key = None
        for key in schemas:
            short = key.split(".")[-1]
            if short == kind:
                schema = schemas[key]
                spec_schema = schema.get("properties", {}).get("spec", {})
                if spec_schema.get("properties"):
                    schema_key = key
                    break

        if not schema_key:
            print(f"  ⚠ {kind}: spec schema not found, skipping")
            continue

        spec_schema = schemas[schema_key]["properties"]["spec"]
        class_name = f"{kind}Spec"
        prefix = kind

        _collect_class(class_name, spec_schema, prefix, collected, order)
        generated_kind_classes.append(class_name)
        print(f"  ✓ {kind} → {class_name} ({len(spec_schema.get('properties', {}))} fields)")

    # Emit one class per collected name. Reverse so nested children appear
    # before the parents that reference them (collection records parents first).
    unique_classes = [(name, _emit_class(name, collected[name])) for name in order]
    unique_classes.reverse()

    # Determine imports
    needs_literal = any("Literal[" in code for _, code in unique_classes)
    needs_any = any(": Any" in code or "Any]" in code for _, code in unique_classes)

    # Build module
    import_lines = [
        '"""',
        f"Auto-generated Pydantic v2 models for EDA {module_name} API.",
        "",
        "DO NOT EDIT — regenerate with: python -m automation.codegen.generate_models",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]

    typing_imports = []
    if needs_any:
        typing_imports.append("Any")
    if needs_literal:
        typing_imports.append("Literal")
    if typing_imports:
        import_lines.append(f"from typing import {', '.join(typing_imports)}")
        import_lines.append("")

    import_lines.extend([
        "from pydantic import BaseModel, ConfigDict, Field",
        "",
        "",
        "class _EDABase(BaseModel):",
        '    """Base class for all EDA models — allows population by field name or alias."""',
        "    model_config = ConfigDict(populate_by_name=True)",
        "",
        "",
    ])

    body = "\n\n\n".join(code for _, code in unique_classes)
    export_list = ", ".join(f'"{name}"' for name, _ in unique_classes)

    module_code = "\n".join(import_lines) + body + "\n"

    return module_code


def _generate_target(target: Target) -> None:
    """Generate all model modules + __init__ for a single target package."""
    target.out_dir.mkdir(parents=True, exist_ok=True)

    all_exports: dict[str, list[str]] = {}

    for spec_file, (module_name, kinds) in target.resource_map.items():
        print(f"\n[{target.name}] {module_name} ({spec_file}):")
        code = generate_module(spec_file, module_name, kinds, spec_dir=target.spec_dir)

        out_path = target.out_dir / f"{module_name}.py"
        out_path.write_text(code)
        print(f"  → {out_path}")

        # Track exports
        all_exports[module_name] = [
            line.split("(")[0].replace("class ", "").strip()
            for line in code.split("\n")
            if line.startswith("class ")
        ]

    # Generate __init__.py — export all classes (specs + nested helpers)
    init_lines = [
        '"""Auto-generated EDA Pydantic models — DO NOT EDIT."""',
        "",
    ]
    for module_name, classes in all_exports.items():
        if classes:
            init_lines.append(f"from .{module_name} import (  # noqa: F401")
            for cls in classes:
                init_lines.append(f"    {cls},")
            init_lines.append(")")

    init_lines.append("")
    (target.out_dir / "__init__.py").write_text("\n".join(init_lines))

    print(f"\n✅ Generated {target.name} models in {target.out_dir}")


def main():
    for target in TARGETS:
        _generate_target(target)


if __name__ == "__main__":
    main()
