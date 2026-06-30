"""
Generate fragment schemas next to each design's strict schema.

For every ``validated-designs/<design>/schemas/<topic>_schema.json`` file
this script produces ``<topic>_fragment_schema.json`` in the same folder.
The fragment schema is a copy of the strict schema with top-level
``required`` removed, so individual fragments under ``inputs/<topic>.d/``
validate cleanly in IDEs that honour the
``# yaml-language-server: $schema=...`` directive.

The merged result is still validated by ``schema_validator.py`` against
the strict schema at runtime, so contractual guarantees are unchanged.

Usage:
    python -m automation.codegen.generate_fragment_schemas

Run after editing any strict ``*_schema.json`` to keep the fragment
sibling files in sync. Generated files start with::

    "DO NOT EDIT — regenerate with:
        python -m automation.codegen.generate_fragment_schemas"

so the source-of-truth relationship is obvious.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from automation.core.schema_validator import (
    bundle_common_refs,
    load_json_schema,
    make_fragment_schema,
)

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGNS_DIR = REPO_ROOT / "validated-designs"
TOPICS = ("topology", "services")


def _emit(strict_path: Path) -> Path | None:
    strict = load_json_schema(strict_path)
    # Bundle shared common $defs into the fragment so the on-disk file is
    # self-contained for editors / YAML language servers.
    fragment = bundle_common_refs(make_fragment_schema(strict))
    out_path = strict_path.with_name(
        strict_path.stem.replace("_schema", "_fragment_schema") + strict_path.suffix
    )
    out_path.write_text(json.dumps(fragment, indent=2) + "\n")
    return out_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not DESIGNS_DIR.is_dir():
        logger.error("validated-designs directory not found: %s", DESIGNS_DIR)
        return 1

    written: list[Path] = []
    for design_dir in sorted(p for p in DESIGNS_DIR.iterdir() if p.is_dir()):
        schemas_dir = design_dir / "schemas"
        if not schemas_dir.is_dir():
            continue
        for topic in TOPICS:
            strict = schemas_dir / f"{topic}_schema.json"
            if not strict.exists():
                continue
            out = _emit(strict)
            if out is not None:
                rel = out.relative_to(REPO_ROOT)
                written.append(out)
                logger.info("wrote %s", rel)

    if not written:
        logger.warning("No strict schemas found under %s", DESIGNS_DIR)
        return 1
    logger.info("generated %d fragment schemas", len(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
