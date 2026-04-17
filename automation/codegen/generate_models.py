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


def _generate_class(
    class_name: str,
    schema: dict,
    parent_prefix: str,
    all_classes: list[tuple[str, str]],
) -> None:
    """Generate a Pydantic model class from an OpenAPI object schema."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    nested_queue: list[tuple[str, dict]] = []

    lines = [f"class {class_name}(_EDABase):"]

    if not props:
        lines.append("    pass")
        all_classes.append((class_name, "\n".join(lines)))
        return

    for prop_name, prop in props.items():
        snake = _to_snake(prop_name)
        py_type = _python_type(prop, parent_prefix, nested_queue)
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

    all_classes.append((class_name, "\n".join(lines)))

    # Generate nested classes (depth-first so they appear before parent)
    for sub_name, sub_schema in nested_queue:
        _generate_class(sub_name, sub_schema, parent_prefix, all_classes)


def generate_module(spec_file: str, module_name: str, kinds: list[str]) -> str:
    """Generate a Python module with Pydantic models for the given resource kinds."""
    spec_path = SPEC_DIR / spec_file
    with open(spec_path) as f:
        spec = json.load(f)

    schemas = spec.get("components", {}).get("schemas", {})

    # Find the spec schema for each resource kind
    all_classes: list[tuple[str, str]] = []
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

        _generate_class(class_name, spec_schema, prefix, all_classes)
        generated_kind_classes.append(class_name)
        print(f"  ✓ {kind} → {class_name} ({len(spec_schema.get('properties', {}))} fields)")

    # De-duplicate classes (nested types may repeat)
    seen = set()
    unique_classes = []
    for name, code in all_classes:
        if name not in seen:
            seen.add(name)
            unique_classes.append((name, code))

    # Topological sort: nested classes before parents
    # Simple approach: reverse the list (depth-first generates children first)
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


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_exports: dict[str, list[str]] = {}

    for spec_file, (module_name, kinds) in RESOURCE_MAP.items():
        print(f"\n{module_name} ({spec_file}):")
        code = generate_module(spec_file, module_name, kinds)

        out_path = OUT_DIR / f"{module_name}.py"
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
            names = ", ".join(classes)
            init_lines.append(f"from .{module_name} import (  # noqa: F401")
            for cls in classes:
                init_lines.append(f"    {cls},")
            init_lines.append(")")

    init_lines.append("")
    (OUT_DIR / "__init__.py").write_text("\n".join(init_lines))

    print(f"\n✅ Generated models in {OUT_DIR}")


if __name__ == "__main__":
    main()
