"""Unit tests for automation.eda_models.profiles (version-keyed registries)."""

import pytest

from automation.eda_models.profiles import (
    DEFAULT_EDA_VERSION,
    Registry,
    SrlSupport,
    SrlTrain,
    get_default_registry,
    get_registry,
    list_eda_versions,
    normalize_eda_version,
)


class TestProfileSelection:
    def test_default_version_listed(self):
        assert DEFAULT_EDA_VERSION in list_eda_versions()

    def test_get_registry_default(self):
        reg = get_registry()
        assert reg.eda_version == DEFAULT_EDA_VERSION
        assert reg is get_default_registry()

    def test_get_registry_unknown_raises(self):
        with pytest.raises(ValueError) as exc:
            get_registry("0.0.0")
        assert "No EDA registry profile" in str(exc.value)

    def test_multiple_profiles_available(self):
        assert len(list_eda_versions()) >= 2

    def test_profiles_keyed_by_major_minor(self):
        assert set(list_eda_versions()) == {"25.12", "26.4"}


class TestVersionMatching:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("26.4", "26.4"),
            ("26.4.2", "26.4"),
            ("26.4.99", "26.4"),
            ("v26.4.2-2605212019-g73187ba6", "26.4"),
            ("25.12", "25.12"),
            ("25.12.4", "25.12"),
            ("25.12.7", "25.12"),
            ("v25.12.0-abc", "25.12"),
        ],
    )
    def test_normalize_eda_version(self, given, expected):
        assert normalize_eda_version(given) == expected

    def test_normalize_unparseable_raises(self):
        with pytest.raises(ValueError):
            normalize_eda_version("not-a-version")

    @pytest.mark.parametrize(
        "given",
        ["26.4", "26.4.2", "26.4.99", "v26.4.2-2605212019-g73187ba6"],
    )
    def test_patch_releases_map_to_same_profile(self, given):
        assert get_registry(given) is get_registry("26.4")

    def test_2512_patches_map_to_2512(self):
        assert get_registry("25.12.7") is get_registry("25.12")
        assert get_registry("25.12.4") is get_registry("25.12.999")

    def test_unknown_minor_raises_with_matched_key(self):
        with pytest.raises(ValueError) as exc:
            get_registry("27.1.0")
        assert "27.1" in str(exc.value)


class TestApiVersionVariesByProfile:
    def test_kind_and_plural_stable_across_profiles(self):
        regs = [get_registry(v) for v in list_eda_versions()]
        for attr in ("FABRIC", "INTERFACE", "BRIDGE_DOMAIN", "POLICY"):
            kinds = {getattr(r, attr).kind for r in regs}
            plurals = {getattr(r, attr).plural for r in regs}
            assert len(kinds) == 1
            assert len(plurals) == 1

    def test_25_12_fabric_is_v1alpha1(self):
        reg = get_registry("25.12.4")
        assert reg.FABRIC.api_version == "fabrics.eda.nokia.com/v1alpha1"
        assert reg.INTERFACE.api_version == "interfaces.eda.nokia.com/v1alpha1"

    def test_26_4_is_v2(self):
        """Verified against a fresh 26.4.2 cluster's /apps discovery + REST API:
        services/protocols graduated to v2, every other group v1alpha1 -> v1."""
        reg = get_registry("26.4.2")
        assert reg.BRIDGE_DOMAIN.api_version == "services.eda.nokia.com/v2"
        assert reg.ROUTER.api_version == "services.eda.nokia.com/v2"
        assert reg.STATIC_ROUTE.api_version == "protocols.eda.nokia.com/v2"
        assert reg.FABRIC.api_version == "fabrics.eda.nokia.com/v1"
        assert reg.INTERFACE.api_version == "interfaces.eda.nokia.com/v1"
        assert reg.CONFIGLET.api_version == "config.eda.nokia.com/v1"
        assert reg.BANNER.api_version == "siteinfo.eda.nokia.com/v1"

    def test_26_4_uses_v2_generator(self):
        assert get_registry("26.4.2").generator_variant == "v2"
        assert get_registry("25.12.4").generator_variant == "v1"

    def test_26_4_and_25_12_differ_in_api_versions(self):
        r26 = get_registry("26.4.2")
        r25 = get_registry("25.12.4")
        assert r26.group_versions != r25.group_versions
        assert r26.group_versions["services"] == "v2"
        assert r25.group_versions["services"] == "v1"

    def test_25_12_services_is_v1(self):
        reg = get_registry("25.12.4")
        assert reg.BRIDGE_DOMAIN.api_version == "services.eda.nokia.com/v1"

    def test_by_kind_consistency(self):
        reg = get_registry()
        for crt in reg.ALL_TYPES:
            assert reg.BY_KIND[crt.kind] is crt


class TestSrlSupportFloorTrain:
    def test_floor_and_known_trains(self):
        support = SrlSupport(
            floor="24.10",
            trains=(SrlTrain("24.10"), SrlTrain("25.3"), SrlTrain("26.3")),
        )
        assert support.check("24.10.1", "X") is None
        assert support.check("26.3.5", "X") is None

    def test_below_floor_rejected(self):
        support = SrlSupport(floor="24.10", trains=(SrlTrain("24.10"),))
        err = support.check("23.10.1", "X")
        assert err is not None and "floor" in err

    def test_unknown_train_rejected(self):
        support = SrlSupport(floor="24.10", trains=(SrlTrain("24.10"),))
        err = support.check("25.5.1", "X")
        assert err is not None and "train" in err

    def test_patch_range_enforced(self):
        support = SrlSupport(
            floor="24.10",
            trains=(SrlTrain("24.10", min_patch=2, max_patch=4),),
        )
        assert support.check("24.10.1", "X") is not None
        assert support.check("24.10.3", "X") is None
        assert support.check("24.10.9", "X") is not None

    def test_bogus_version_rejected(self):
        support = SrlSupport(floor="24.10", trains=(SrlTrain("24.10"),))
        assert support.check("not-a-version", "X") is not None

    def test_registry_check_srl_version(self):
        reg = get_registry()
        assert isinstance(reg, Registry)
        assert reg.check_srl_version("24.10.2") is None
        assert reg.check_srl_version("23.1.1") is not None
