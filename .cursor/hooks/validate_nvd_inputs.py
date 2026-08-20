"""Validate NVD design inputs after an agent edit, and report back to the agent.

Reads a Cursor ``postToolUse`` payload on stdin. If the tool touched a file
under ``validated-designs/<design>/inputs/``, the whole design is re-validated
through ``schema_validator.load_inputs`` — which checks each fragment against
``*_fragment_schema.json`` *and* the merged result against the strict schema,
so cross-fragment problems (a required key no fragment supplies, an identity
key conflict) are caught alongside single-file ones.

On failure it emits ``{"additional_context": ...}`` so the error lands in the
agent's context and can be fixed in the same turn. On success it emits nothing.
Every path exits 0: this hook reports, it never blocks.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keys under which the various edit tools carry their target file.
_PATH_KEYS = ("path", "file_path", "target_file", "target_notebook")


def _emit(context: str | None) -> None:
    if context:
        json.dump({"additional_context": context}, sys.stdout)
    sys.exit(0)


def _candidate_paths(payload: dict) -> list[Path]:
    raw: list[str] = []
    for source in (payload.get("tool_input"), payload):
        if isinstance(source, dict):
            raw += [source[k] for k in _PATH_KEYS if isinstance(source.get(k), str)]

    paths = []
    for value in raw:
        p = Path(value)
        paths.append(p if p.is_absolute() else REPO_ROOT / p)
    return paths


def _design_dir(path: Path) -> Path | None:
    """Return the design directory owning *path*, or None if it isn't an input."""
    try:
        rel = path.resolve().relative_to(REPO_ROOT / "validated-designs")
    except (ValueError, OSError):
        return None
    if len(rel.parts) < 2 or rel.parts[1] != "inputs":
        return None
    design = REPO_ROOT / "validated-designs" / rel.parts[0]
    return design if (design / "schemas").is_dir() else None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _emit(None)
    if not isinstance(payload, dict):
        _emit(None)

    designs: list[Path] = []
    for path in _candidate_paths(payload):
        design = _design_dir(path)
        if design is not None and design not in designs:
            designs.append(design)
    if not designs:
        _emit(None)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        import jsonschema
        from automation.core.schema_validator import load_inputs
    except ImportError:
        _emit(None)

    # schema_validator logs progress and merge overrides; keep stdout clean for
    # the hook's JSON and stay quiet unless something actually fails.
    logging.disable(logging.CRITICAL)

    problems: list[str] = []
    for design in designs:
        try:
            load_inputs(design)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path)
            location = f" (at `{where}`)" if where else ""
            problems.append(f"{design.name}: {e.message}{location}")
        except (ValueError, FileNotFoundError, OSError) as e:
            problems.append(f"{design.name}: {e}")

    if not problems:
        _emit(None)

    detail = "\n".join(f"- {p}" for p in problems)
    _emit(
        "Schema validation failed for the design inputs just edited. "
        "Fix this before continuing:\n"
        f"{detail}\n"
        "Schemas live in the design's schemas/ directory "
        "(*_fragment_schema.json per fragment, *_schema.json for the merged result)."
    )


if __name__ == "__main__":
    main()
