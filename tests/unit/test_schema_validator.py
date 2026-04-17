"""Unit tests for automation.core.schema_validator."""

import json
import pytest
from pathlib import Path

from automation.core.schema_validator import load_yaml, load_inputs, validate_input


DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"


class TestLoadYaml:
    def test_load_valid_yaml(self, tmp_path):
        p = tmp_path / "test.yaml"
        p.write_text("key: value\n")
        data = load_yaml(p)
        assert data == {"key": "value"}

    def test_load_non_mapping_raises(self, tmp_path):
        p = tmp_path / "test.yaml"
        p.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="must contain a YAML mapping"):
            load_yaml(p)


class TestValidateInput:
    def test_valid_data(self, tmp_path):
        schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(schema))
        validate_input({"name": "test"}, schema_path)

    def test_invalid_data_raises(self, tmp_path):
        schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(schema))
        with pytest.raises(Exception):
            validate_input({}, schema_path)


class TestLoadInputs:
    def test_3stage_inputs_load(self):
        design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
        if not design_dir.exists():
            pytest.skip("3-stage-evpn-vxlan design not found")
        topo, svc = load_inputs(design_dir)
        assert topo["design"] == "3-stage-evpn-vxlan"
        assert "bridge_domains" in svc

    def test_unconstrained_inputs_load(self):
        design_dir = DESIGNS_ROOT / "unconstrained-3-stage"
        if not design_dir.exists():
            pytest.skip("unconstrained-3-stage design not found")
        topo, svc = load_inputs(design_dir)
        assert topo["design"] == "unconstrained-3-stage"
        assert "bridge_domains" in svc

    def test_missing_inputs_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_inputs(tmp_path)
