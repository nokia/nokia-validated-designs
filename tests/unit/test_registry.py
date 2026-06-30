"""Unit tests for automation.eda_models.registry."""

from automation.eda_models.registry import (
    ALL_TYPES,
    BY_KIND,
    CRType,
    EDA_VERSION,
    SUPPORTED_SRL_VERSIONS,
    check_srl_version,
    INIT,
    FABRIC,
    TOPO_NODE,
    BRIDGE_DOMAIN,
)


class TestCRTypeRegistry:
    def test_all_types_populated(self):
        assert len(ALL_TYPES) >= 18

    def test_by_kind_consistency(self):
        for crt in ALL_TYPES:
            assert crt.kind in BY_KIND
            assert BY_KIND[crt.kind] is crt

    def test_no_duplicate_kinds(self):
        kinds = [crt.kind for crt in ALL_TYPES]
        assert len(kinds) == len(set(kinds))

    def test_api_version_format(self):
        for crt in ALL_TYPES:
            assert "/" in crt.api_version, f"{crt.kind} apiVersion missing /"
            parts = crt.api_version.split("/")
            assert len(parts) == 2

    def test_plural_lowercase(self):
        for crt in ALL_TYPES:
            assert crt.plural == crt.plural.lower()

    def test_known_types(self):
        assert INIT.kind == "Init"
        assert FABRIC.kind == "Fabric"
        assert TOPO_NODE.kind == "TopoNode"
        assert BRIDGE_DOMAIN.kind == "BridgeDomain"

    def test_crtype_immutable(self):
        import pytest
        with pytest.raises(AttributeError):
            INIT.api_version = "changed"


class TestSRLVersionCheck:
    def test_supported_version_returns_none(self):
        for ver in SUPPORTED_SRL_VERSIONS:
            assert check_srl_version(ver) is None

    def test_below_floor_returns_message(self):
        # 23.10 is below the 24.10 floor.
        err = check_srl_version("23.10.1")
        assert err is not None
        assert "23.10.1" in err
        assert EDA_VERSION in err

    def test_unknown_train_returns_message(self):
        # 25.5 is at/above the floor but not a known train.
        err = check_srl_version("25.5.1")
        assert err is not None
        assert EDA_VERSION in err

    def test_known_train_open_patch_supported(self):
        # 26.3 is a known train (so the v26 builder isn't rejected).
        assert check_srl_version("26.3.1") is None

    def test_bogus_version_returns_message(self):
        assert check_srl_version("0.0.0") is not None

    def test_eda_version_is_string(self):
        assert isinstance(EDA_VERSION, str)
        # Profiles are keyed by major.minor (e.g. "25.12"); patch releases map
        # to the same profile, so the profile version has at least 2 parts.
        assert len(EDA_VERSION.split(".")) >= 2

    def test_supported_versions_non_empty(self):
        assert len(SUPPORTED_SRL_VERSIONS) > 0
