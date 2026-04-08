"""
Schema validator for NVD input files.

Validates topology.yaml and services.yaml against their respective
JSON Schema definitions stored in the design's schemas/ directory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import jsonschema
import yaml

logger = logging.getLogger(__name__)


def load_yaml(path: Path) -> dict:
    """Load and parse a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
    return data


def load_json_schema(path: Path) -> dict:
    """Load a JSON Schema file."""
    with open(path) as f:
        return json.load(f)


def validate_input(data: dict, schema_path: Path) -> None:
    """
    Validate a data dict against a JSON Schema file.

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


def load_inputs(design_dir: Path) -> tuple[dict, dict]:
    """
    Load and validate both topology and services inputs for a design.

    Args:
        design_dir: Path to the design directory (e.g. validated-designs/3-stage-evpn-vxlan)

    Returns:
        Tuple of (topology_data, services_data)
    """
    inputs_dir = design_dir / "inputs"
    schemas_dir = design_dir / "schemas"

    topo_path = inputs_dir / "topology.yaml"
    svc_path = inputs_dir / "services.yaml"
    topo_schema = schemas_dir / "topology_schema.json"
    svc_schema = schemas_dir / "services_schema.json"

    # Check files exist
    for p in [topo_path, svc_path]:
        if not p.exists():
            raise FileNotFoundError(f"Input file not found: {p}")

    # Load YAML
    topo_data = load_yaml(topo_path)
    svc_data = load_yaml(svc_path)

    # Validate if schemas exist (schemas are optional during development)
    if topo_schema.exists():
        validate_input(topo_data, topo_schema)
    else:
        logger.warning("No topology schema found at %s, skipping validation", topo_schema)

    if svc_schema.exists():
        validate_input(svc_data, svc_schema)
    else:
        logger.warning("No services schema found at %s, skipping validation", svc_schema)

    return topo_data, svc_data
