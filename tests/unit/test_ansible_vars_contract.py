"""Unit tests for the generated Ansible projects' variable contract.

Two properties matter here and neither is obvious from reading the code:

* The contract must accept everything the generator emits. Otherwise the
  schema directive we write into each vars file flags the generator's own
  output, and operators learn to ignore it.
* The contract must reject plausible mistakes. A ``.get()``-based builder
  ignores an unknown key silently, and with ``purge`` enabled the config that
  key should have produced is then deleted from the device, so "accepted but
  ignored" is the failure mode these artifacts exist to prevent.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

from automation.core.fabric_builder import build_intent
from automation.core.models import FabricIntent
from automation.core.schema_validator import load_inputs
from automation.generators.ansible_generator import generate
from automation.generators.ansible_vars_contract import (
    GROUPS,
    ROLE_VARS,
    SCHEMA_FILENAME,
    VARS,
    ansible_vars_schema,
    inert_var_paths,
    role_argument_specs,
    vars_reference_table,
)

DESIGNS_ROOT = Path(__file__).resolve().parents[2] / "validated-designs"
BUILDERS_DIR = (
    Path(__file__).resolve().parents[2]
    / "automation"
    / "generators"
    / "ansible_filter_plugins"
)


@pytest.fixture()
def three_stage_intent() -> FabricIntent:
    design_dir = DESIGNS_ROOT / "3-stage-evpn-vxlan"
    if not design_dir.exists():
        pytest.skip("3-stage-evpn-vxlan design not found")
    topo, svc = load_inputs(design_dir)
    return build_intent(topo, svc)


@pytest.fixture()
def project(three_stage_intent, tmp_path) -> Path:
    generate(three_stage_intent, output_dir=tmp_path)
    return tmp_path


def _vars_files(project: Path) -> list[Path]:
    return sorted(project.glob("group_vars/*.yml")) + sorted(
        project.glob("host_vars/*.yml")
    )


def _validator(project: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads((project / "schemas" / SCHEMA_FILENAME).read_text())
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# Schema is emitted and wired up
# ---------------------------------------------------------------------------


class TestSchemaEmission:
    def test_schema_file_written(self, project):
        schema_path = project / "schemas" / SCHEMA_FILENAME
        assert schema_path.exists()
        jsonschema.Draft202012Validator.check_schema(
            json.loads(schema_path.read_text())
        )

    def test_every_vars_file_points_at_the_schema(self, project):
        files = _vars_files(project)
        assert files, "no vars files generated"
        for path in files:
            first = path.read_text().splitlines()[0]
            assert first == (
                f"# yaml-language-server: $schema=../schemas/{SCHEMA_FILENAME}"
            ), f"{path.name} missing schema directive"

    def test_directive_path_resolves(self, project):
        """The relative path in the directive must actually exist on disk."""
        for path in _vars_files(project):
            rel = path.read_text().splitlines()[0].split("$schema=")[1]
            assert (path.parent / rel).resolve().exists()

    def test_header_warns_about_regeneration(self, project):
        header = (project / "host_vars" / "leaf1.yml").read_text()
        assert "Regenerating overwrites this file" in header


# ---------------------------------------------------------------------------
# The generator's own output satisfies the contract
# ---------------------------------------------------------------------------


class TestGeneratedOutputRoundTrips:
    def test_all_vars_files_validate(self, project):
        validator = _validator(project)
        failures: list[str] = []
        for path in _vars_files(project):
            data = yaml.safe_load(path.read_text()) or {}
            for err in validator.iter_errors(data):
                where = ".".join(str(p) for p in err.path) or "<root>"
                failures.append(f"{path.name}: {where}: {err.message}")
        assert not failures, "generated output violates its own schema:\n" + "\n".join(
            failures
        )

    def test_argument_specs_accept_real_scope(self, project):
        """Every role's spec accepts the merged scope for every host.

        Mirrors what ansible-core does at role entry: it collects only the
        variables named in the spec out of the task vars, then validates.
        """
        pytest.importorskip("ansible")
        from ansible.module_utils.common.arg_spec import ArgumentSpecValidator

        all_gv = yaml.safe_load((project / "group_vars" / "all.yml").read_text())
        group_gv = {
            group: yaml.safe_load((project / "group_vars" / f"{group}.yml").read_text())
            for group in ("leafs", "spines")
        }
        inventory = yaml.safe_load((project / "inventory.yml").read_text())
        specs = _load_role_specs(project)

        failures: list[str] = []
        for group, data in inventory["all"]["children"].items():
            for host in data["hosts"]:
                hv = yaml.safe_load((project / "host_vars" / f"{host}.yml").read_text())
                scope = {**all_gv, **group_gv[group], **hv}
                for role, options in specs.items():
                    provided = {k: v for k, v in scope.items() if k in options}
                    result = ArgumentSpecValidator(options).validate(provided)
                    if result.error_messages:
                        failures.append(f"{host}/{role}: {result.error_messages}")
        assert not failures, "\n".join(failures)


def _load_role_specs(project: Path) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for meta in sorted(project.glob("roles/*/meta/argument_specs.yml")):
        role = meta.parent.parent.name
        loaded = yaml.safe_load(meta.read_text())
        specs[role] = loaded["argument_specs"]["main"]["options"]
    return specs


# ---------------------------------------------------------------------------
# The contract rejects plausible mistakes
# ---------------------------------------------------------------------------


class TestSchemaRejectsMistakes:
    @pytest.fixture()
    def leaf_vars(self, project) -> dict:
        return yaml.safe_load((project / "host_vars" / "leaf1.yml").read_text())

    def _errors(self, project, data) -> list[str]:
        return [e.message for e in _validator(project).iter_errors(data)]

    def test_unknown_top_level_key(self, project, leaf_vars):
        data = copy.deepcopy(leaf_vars)
        data["irb_interfacs"] = data.pop("irb_interfaces")
        assert self._errors(project, data), "top-level typo accepted"

    def test_unknown_nested_key(self, project, leaf_vars):
        data = copy.deepcopy(leaf_vars)
        data["irb_interfaces"][0]["anycast_g"] = data["irb_interfaces"][0].pop(
            "anycast_gw"
        )
        assert self._errors(project, data), "nested typo accepted"

    def test_wrong_scalar_type(self, project, leaf_vars):
        data = copy.deepcopy(leaf_vars)
        data["irb_interfaces"][0]["arp_timeout"] = "280"
        assert self._errors(project, data), "string in an integer field accepted"

    def test_missing_required_nested_key(self, project, leaf_vars):
        data = copy.deepcopy(leaf_vars)
        del data["node"]["asn"]
        assert self._errors(project, data), "missing node.asn accepted"

    def test_value_outside_enum(self, project, leaf_vars):
        data = copy.deepcopy(leaf_vars)
        data["edge_interfaces"][0]["encap"] = "dot1x"
        assert self._errors(project, data), "invalid encap accepted"

    @pytest.mark.parametrize("key", ["ansible_port", "x_site"])
    def test_passthrough_namespaces_allowed(self, project, leaf_vars, key):
        data = copy.deepcopy(leaf_vars)
        data[key] = "whatever"
        assert not self._errors(project, data), f"{key} should be permitted"


class TestArgumentSpecsRejectMistakes:
    def test_nested_typo_fails_validation(self, project):
        pytest.importorskip("ansible")
        from ansible.module_utils.common.arg_spec import ArgumentSpecValidator

        options = _load_role_specs(project)["services"]
        hv = yaml.safe_load((project / "host_vars" / "leaf1.yml").read_text())
        hv["irb_interfaces"][0]["anycast_g"] = hv["irb_interfaces"][0].pop("anycast_gw")

        provided = {k: v for k, v in hv.items() if k in options}
        result = ArgumentSpecValidator(options).validate(provided)
        assert result.error_messages
        assert "anycast_g" in " ".join(result.error_messages)

    def test_missing_required_fails_validation(self, project):
        pytest.importorskip("ansible")
        from ansible.module_utils.common.arg_spec import ArgumentSpecValidator

        options = _load_role_specs(project)["fabric"]
        result = ArgumentSpecValidator(options).validate({})
        assert result.error_messages, "absent required 'node' accepted"


# ---------------------------------------------------------------------------
# Argument spec emission
# ---------------------------------------------------------------------------


class TestArgumentSpecEmission:
    def test_spec_written_for_every_declared_role(self, project):
        for role in ROLE_VARS:
            path = project / "roles" / role / "meta" / "argument_specs.yml"
            assert path.exists(), f"missing argument_specs.yml for {role}"

    def test_detect_role_has_no_spec(self, project):
        """detect reads no project variables, so a spec would be noise."""
        assert (project / "roles" / "detect" / "tasks" / "main.yml").exists()
        assert not (project / "roles" / "detect" / "meta").exists()

    def test_specs_are_well_formed(self, project):
        for role, options in _load_role_specs(project).items():
            assert options, f"{role} spec has no options"
            for name, opt in options.items():
                assert "type" in opt, f"{role}.{name} has no type"
                assert opt.get("description"), f"{role}.{name} has no description"

    def test_specs_reference_the_schema(self, project):
        for meta in sorted(project.glob("roles/*/meta/argument_specs.yml")):
            text = meta.read_text()
            assert SCHEMA_FILENAME in text, f"{meta} does not point at the schema"

    def test_role_vars_are_all_declared(self):
        """Guards typos in ROLE_VARS, which would otherwise KeyError at generate."""
        declared = {v.name for v in VARS}
        for role, names in ROLE_VARS.items():
            unknown = set(names) - declared
            assert not unknown, f"{role} references undeclared vars: {unknown}"


# ---------------------------------------------------------------------------
# Drift between the builders and the contract
# ---------------------------------------------------------------------------


_HV_LOOKUP_RE = re.compile(
    r"""(?:hv|host_vars)(?:\.get\(|\[)["']([a-z_0-9]+)["']"""
)

# Read from the merged scope by the builders but supplied as a runtime fact by
# the detect role rather than by a vars file, so not part of the contract.
_RUNTIME_FACTS = {"sw_version"}


class TestContractTracksBuilders:
    def test_every_key_the_builders_read_is_declared(self):
        """A builder gaining a new key must be declared here to be documented.

        Without this the contract silently narrows: the new key works, but the
        schema rejects it and the editor tells operators it is a typo.
        """
        declared = {v.name for v in VARS} | _RUNTIME_FACTS
        found: dict[str, set[str]] = {}
        for source in sorted(BUILDERS_DIR.rglob("*.py")):
            for key in _HV_LOOKUP_RE.findall(source.read_text()):
                found.setdefault(key, set()).add(source.name)

        assert found, "no host_vars lookups found; the regex probably broke"
        undeclared = {k: v for k, v in found.items() if k not in declared}
        assert not undeclared, (
            "builders read variables missing from the contract in "
            "ansible_vars_contract.py: "
            + ", ".join(f"{k} ({', '.join(sorted(v))})" for k, v in sorted(undeclared.items()))
        )


# ---------------------------------------------------------------------------
# Documentation helpers
# ---------------------------------------------------------------------------


class TestDocumentationHelpers:
    def test_readme_documents_the_contract(self, project):
        readme = (project / "README.md").read_text()
        assert "## Variable reference" in readme
        assert "## Validating your edits" in readme
        assert SCHEMA_FILENAME in readme
        assert "meta/argument_specs.yml" in readme

    def test_readme_is_not_indented(self, project):
        """textwrap.dedent silently no-ops when an interpolated block is flush left."""
        readme = (project / "README.md").read_text()
        assert readme.startswith("# "), "README lost its dedent"
        assert "\n        ## Quick start" not in readme

    def test_every_top_level_var_appears_in_the_table(self):
        table = vars_reference_table()
        for var in VARS:
            assert f"`{var.name}`" in table, f"{var.name} missing from README table"

    def test_inert_paths_are_dotted_and_known(self):
        paths = inert_var_paths()
        assert paths, "expected some inert keys"
        top_level = {v.name for v in VARS}
        for path in paths:
            assert path.split(".")[0] in top_level

    def test_groups_cover_all_vars(self):
        grouped = [v for members in GROUPS.values() for v in members]
        assert len(grouped) == len(VARS)

    def test_schema_documents_every_key(self):
        schema = ansible_vars_schema()
        for name, node in schema["properties"].items():
            assert node.get("description"), f"{name} has no description"

    def test_inert_keys_are_flagged_in_their_description(self):
        schema = ansible_vars_schema()

        def find(node: dict, parts: list[str]) -> dict:
            for part in parts:
                node = node.get("properties", node.get("items", {}).get("properties", {}))[part]
            return node

        for path in inert_var_paths():
            parts = path.split(".")
            node = find({"properties": schema["properties"]}, parts)
            assert "NOT read" in node["description"], f"{path} not flagged as inert"


class TestRoleSpecScoping:
    def test_spine_scope_needs_no_service_vars(self, project):
        """A spine has no service keys; its roles must not require them."""
        pytest.importorskip("ansible")
        from ansible.module_utils.common.arg_spec import ArgumentSpecValidator

        all_gv = yaml.safe_load((project / "group_vars" / "all.yml").read_text())
        spine_gv = yaml.safe_load((project / "group_vars" / "spines.yml").read_text())
        hv = yaml.safe_load((project / "host_vars" / "spine1.yml").read_text())
        scope = {**all_gv, **spine_gv, **hv}

        for role, options in _load_role_specs(project).items():
            provided = {k: v for k, v in scope.items() if k in options}
            result = ArgumentSpecValidator(options).validate(provided)
            assert not result.error_messages, f"{role}: {result.error_messages}"
