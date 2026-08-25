"""Unit tests for the NVD deployer CLI surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from automation.deploy import get_version, main

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestVersion:
    def test_installed_metadata_matches_pyproject(self):
        """``pyproject.toml`` is the source of truth; install metadata must match."""
        assert get_version() == _pyproject_version()

    def test_version_flag(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["nvd", "--version"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out.strip()
        assert out == f"nvd {_pyproject_version()}"
