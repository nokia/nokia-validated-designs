"""EDA-version-keyed registry profiles.

The automation engine targets a specific EDA release. Across releases the
*kind* and *plural* of every Custom Resource stay stable, but the API group
*version* graduates (e.g. ``v1alpha1`` -> ``v1``). This module models each
supported EDA release as a :class:`Registry` profile that maps every API
group to its apiVersion for that release, plus the SR Linux version-support
window for that release.

Select a profile with :func:`get_registry` (keyed by EDA version) and thread
the resulting :class:`Registry` through the generator/executor. The default
profile (:data:`DEFAULT_EDA_VERSION`) is re-exported by
``automation.eda_models.registry`` for backward compatibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# CR type descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CRType:
    """Immutable descriptor for an EDA Custom Resource type."""

    api_version: str
    kind: str
    plural: str
    # EDA's REST API rejects the ``/namespaces/{ns}/`` path segment for a few
    # kinds (``InvalidForNonNamespacedResource``), so their list/get endpoints
    # are cluster-scoped. See :data:`_CLUSTER_SCOPED_KINDS`.
    cluster_scoped: bool = False


# Fixed API group domains. Only the trailing version varies per EDA release,
# and that lives in each profile's ``group_versions`` map.
_GROUP_DOMAINS: dict[str, str] = {
    "bootstrap": "bootstrap.eda.nokia.com",
    "core": "core.eda.nokia.com",
    "interfaces": "interfaces.eda.nokia.com",
    "fabrics": "fabrics.eda.nokia.com",
    "services": "services.eda.nokia.com",
    "protocols": "protocols.eda.nokia.com",
    "config": "config.eda.nokia.com",
    "siteinfo": "siteinfo.eda.nokia.com",
    "routingpolicies": "routingpolicies.eda.nokia.com",
    "aifabrics": "aifabrics.eda.nokia.com",
    "qos": "qos.eda.nokia.com",
    "aaa": "aaa.eda.nokia.com",
    "topologies": "topologies.eda.nokia.com",
}

# Stable GVK skeleton: (attr_name, group_key, kind, plural). The apiVersion is
# composed at profile-build time from the group domain + the profile's version
# for that group.
_CR_SKELETON: list[tuple[str, str, str, str]] = [
    ("INIT", "bootstrap", "Init", "inits"),
    ("NODE_USER", "core", "NodeUser", "nodeusers"),
    ("NODE_PROFILE", "core", "NodeProfile", "nodeprofiles"),
    ("TOPO_NODE", "core", "TopoNode", "toponodes"),
    ("TOPO_LINK", "core", "TopoLink", "topolinks"),
    ("INDEX_ALLOCATION_POOL", "core", "IndexAllocationPool", "indexallocationpools"),
    ("IP_ALLOCATION_POOL", "core", "IPAllocationPool", "ipallocationpools"),
    ("INTERFACE", "interfaces", "Interface", "interfaces"),
    ("FABRIC", "fabrics", "Fabric", "fabrics"),
    ("BRIDGE_DOMAIN", "services", "BridgeDomain", "bridgedomains"),
    ("ROUTER", "services", "Router", "routers"),
    ("IRB_INTERFACE", "services", "IRBInterface", "irbinterfaces"),
    ("VLAN", "services", "VLAN", "vlans"),
    ("ROUTED_INTERFACE", "services", "RoutedInterface", "routedinterfaces"),
    ("STATIC_ROUTE", "protocols", "StaticRoute", "staticroutes"),
    ("CONFIGLET", "config", "Configlet", "configlets"),
    ("DEFAULT_MTU", "siteinfo", "DefaultMTU", "defaultmtus"),
    ("BANNER", "siteinfo", "Banner", "banners"),
    ("POLICY", "routingpolicies", "Policy", "policys"),
    ("PREFIX_SET", "routingpolicies", "PrefixSet", "prefixsets"),
    # AI-fabric / multi-namespace kinds. Plurals below were read off a live
    # 25.12 cluster's ``kubectl api-resources`` — note EDA pluralizes by naive
    # ``+s``, hence ``forwardingclasss`` (three s's), matching the existing
    # ``policys``.
    ("NAMESPACE", "core", "Namespace", "namespaces"),
    ("NODE_GROUP", "aaa", "NodeGroup", "nodegroups"),
    ("TOPOLOGY_GROUPING", "topologies", "TopologyGrouping", "topologygroupings"),
    ("QUEUE", "qos", "Queue", "queues"),
    ("FORWARDING_CLASS", "qos", "ForwardingClass", "forwardingclasss"),
    ("AI_BACKEND", "aifabrics", "Backend", "backends"),
]


# Kinds whose EDA REST endpoints are cluster-scoped. Their CRDs are namespaced
# and a write still carries ``metadata.namespace`` (an EDA Namespace lives in
# ``eda-system``), but a *read* through ``/namespaces/{ns}/...`` is rejected
# with ``InvalidForNonNamespacedResource`` — both are top-level objects as far
# as the API is concerned. Reading them per namespace instead of once is why a
# converged fabric would otherwise keep re-reporting them as creates.
_CLUSTER_SCOPED_KINDS: frozenset[str] = frozenset(
    {"Namespace", "TopologyGrouping"}
)


# ---------------------------------------------------------------------------
# SR Linux version support (floor + trains, per EDA release)
# ---------------------------------------------------------------------------


def _parse_version(version: str) -> tuple[int, int, int]:
    """Parse ``"24.10.2"`` (or ``"v24.10.2"``) into ``(24, 10, 2)``.

    A missing patch defaults to 0. Raises ``ValueError`` on a non-numeric
    or empty version so the caller can surface a clear gate error.
    """
    cleaned = version.strip().lstrip("v")
    if not cleaned:
        raise ValueError("empty version")
    parts = cleaned.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError) as exc:
        raise ValueError(f"unparseable version {version!r}") from exc
    return major, minor, patch


@dataclass(frozen=True)
class SrlTrain:
    """A supported SR Linux release train (one ``major.minor``).

    ``min_patch``/``max_patch`` bound the accepted maintenance releases within
    the train. ``max_patch=None`` means the train is open-ended.
    """

    minor: str  # "24.10", "25.3", ...
    min_patch: int = 1
    max_patch: int | None = None

    @property
    def key(self) -> tuple[int, int]:
        major, minor, _ = _parse_version(self.minor)
        return (major, minor)


@dataclass(frozen=True)
class SrlSupport:
    """SR Linux support window for one EDA release.

    A version is supported when it is at or above ``floor`` *and* falls inside
    a known train (matching ``major.minor`` with the patch in range). This is
    the "24.10 onwards" semantics: a floor plus per-train patch ranges, rather
    than a frozen exact-match allow-list.
    """

    floor: str  # "24.10"
    trains: tuple[SrlTrain, ...]

    def check_floor(self, version: str, eda_version: str) -> str | None:
        """Return an error if *version* is below the floor, else ``None``.

        Floor-only variant for the Ansible path: it answers "is this device at
        least 24.10" without the EDA-specific train/patch gating, since the
        version-dispatched builders resolve any 24.10+ release.
        """
        try:
            major, minor, _ = _parse_version(version)
        except ValueError:
            return f"SR Linux version {version!r} is not a valid version string"
        fmaj, fmin, _ = _parse_version(self.floor)
        if (major, minor) < (fmaj, fmin):
            return (
                f"SR Linux version {version!r} is below the supported floor "
                f"{self.floor} (24.10 onwards)"
            )
        return None

    def check(self, version: str, eda_version: str) -> str | None:
        """Return an error message if *version* is unsupported, else ``None``."""
        try:
            parsed = _parse_version(version)
        except ValueError:
            return (
                f"SR Linux version {version!r} is not a valid version string"
            )
        major, minor, patch = parsed

        fmaj, fmin, _ = _parse_version(self.floor)
        if (major, minor) < (fmaj, fmin):
            return (
                f"SR Linux version {version!r} is below the supported floor "
                f"{self.floor} for EDA {eda_version} (24.10 onwards)"
            )

        for train in self.trains:
            if train.key == (major, minor):
                if patch < train.min_patch:
                    return (
                        f"SR Linux version {version!r} is below the minimum "
                        f"patch {train.minor}.{train.min_patch} supported by "
                        f"EDA {eda_version}"
                    )
                if train.max_patch is not None and patch > train.max_patch:
                    return (
                        f"SR Linux version {version!r} is above the maximum "
                        f"patch {train.minor}.{train.max_patch} supported by "
                        f"EDA {eda_version}"
                    )
                return None

        known = ", ".join(t.minor for t in self.trains)
        return (
            f"SR Linux train {major}.{minor} is not supported by EDA "
            f"{eda_version}. Supported trains: {known}"
        )


# ---------------------------------------------------------------------------
# Registry — a fully-resolved EDA profile
# ---------------------------------------------------------------------------


class Registry:
    """A fully-resolved set of CR type descriptors for one EDA release.

    Exposes each CR type as a named attribute (``registry.FABRIC``,
    ``registry.INTERFACE``, ...) plus ``ALL_TYPES`` and ``BY_KIND``, mirroring
    the legacy module-level constants in ``registry.py``.
    """

    # Declared for static analysis / editor completion; populated in __init__.
    INIT: CRType
    NODE_USER: CRType
    NODE_PROFILE: CRType
    TOPO_NODE: CRType
    TOPO_LINK: CRType
    INDEX_ALLOCATION_POOL: CRType
    IP_ALLOCATION_POOL: CRType
    INTERFACE: CRType
    FABRIC: CRType
    BRIDGE_DOMAIN: CRType
    ROUTER: CRType
    IRB_INTERFACE: CRType
    VLAN: CRType
    ROUTED_INTERFACE: CRType
    STATIC_ROUTE: CRType
    CONFIGLET: CRType
    DEFAULT_MTU: CRType
    BANNER: CRType
    POLICY: CRType
    PREFIX_SET: CRType
    NAMESPACE: CRType
    NODE_GROUP: CRType
    TOPOLOGY_GROUPING: CRType
    QUEUE: CRType
    FORWARDING_CLASS: CRType
    AI_BACKEND: CRType

    def __init__(
        self,
        eda_version: str,
        group_versions: dict[str, str],
        srl_support: SrlSupport,
        tested_srl_versions: list[str],
        generator_variant: str = "v1",
        supports_ai_fabrics: bool = True,
    ):
        self.eda_version = eda_version
        self.group_versions = dict(group_versions)
        self.srl_support = srl_support
        self.tested_srl_versions = list(tested_srl_versions)
        # CR-generation backend. ``"v1"`` uses the default ``eda_generator``;
        # ``"v2"`` selects ``eda_generator_v2`` (26.4.x services/protocols v2
        # spec shapes + the ``eda_models.eda_26_4`` model package).
        self.generator_variant = generator_variant
        # Whether the AI-fabric kinds (Backend, Queue, ForwardingClass,
        # NodeGroup, TopologyGrouping, Namespace) and the multi-fabric /
        # multi-namespace designs that use them can be generated for this
        # release. False where the CR shapes were never verified against a
        # live cluster, so the generator refuses rather than emitting
        # something plausible but wrong.
        self.supports_ai_fabrics = supports_ai_fabrics

        types: list[CRType] = []
        for attr, group_key, kind, plural in _CR_SKELETON:
            version = group_versions[group_key]
            api_version = f"{_GROUP_DOMAINS[group_key]}/{version}"
            crt = CRType(
                api_version, kind, plural, kind in _CLUSTER_SCOPED_KINDS
            )
            setattr(self, attr, crt)
            types.append(crt)

        self.ALL_TYPES: list[CRType] = types
        self.BY_KIND: dict[str, CRType] = {t.kind: t for t in types}

    def check_srl_version(self, version: str) -> str | None:
        """Return an error message if *version* is unsupported, else ``None``."""
        return self.srl_support.check(version, self.eda_version)

    def check_srl_floor(self, version: str) -> str | None:
        """Return an error if *version* is below the SR Linux floor, else ``None``."""
        return self.srl_support.check_floor(version, self.eda_version)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Registry(eda_version={self.eda_version!r})"


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------


# Trains shared by current EDA releases. 26.3 is included so the SR Linux
# 26.x builder (srl_builders/v26.py) is not rejected by the gate.
_TRAINS_CURRENT: tuple[SrlTrain, ...] = (
    SrlTrain("24.10"),
    SrlTrain("25.3"),
    SrlTrain("25.7"),
    SrlTrain("25.10"),
    SrlTrain("26.3"),
)

_SRL_SUPPORT_CURRENT = SrlSupport(floor="24.10", trains=_TRAINS_CURRENT)

# EDA 26.8 ships a ``srlinux-ghcr-26.7.1`` artifact/node profile on top of the
# trains above (read off a fresh 26.8.1 cluster's artifact list). Kept as its
# own tuple rather than widening ``_TRAINS_CURRENT`` so the 25.12 and 26.4
# profiles keep rejecting a train their clusters cannot onboard.
_TRAINS_26_8: tuple[SrlTrain, ...] = _TRAINS_CURRENT + (SrlTrain("26.7"),)

_SRL_SUPPORT_26_8 = SrlSupport(floor="24.10", trains=_TRAINS_26_8)

# Known-good, explicitly tested exact versions (used by acceptance tests and
# kept for backward compatibility with the old SUPPORTED_SRL_VERSIONS export).
_TESTED_SRL_VERSIONS: list[str] = [
    "24.10.1",
    "24.10.2",
    "24.10.3",
    "24.10.4",
    "25.3.1",
    "25.3.2",
    "25.3.3",
    "25.7.1",
    "25.7.2",
    "25.10.1",
    "25.10.2",
]

# SR Linux releases EDA 26.8 ships an artifact + node profile for, read off a
# fresh 26.8.1 cluster (``kubectl get artifacts -A | grep srlinux-ghcr``).
_TESTED_SRL_VERSIONS_26_8: list[str] = [
    "24.10.1",
    "24.10.2",
    "24.10.3",
    "24.10.4",
    "24.10.5",
    "24.10.6",
    "24.10.7",
    "25.3.2",
    "25.3.3",
    "25.7.1",
    "25.7.2",
    "25.10.1",
    "25.10.2",
    "25.10.3",
    "25.10.4",
    "25.10.5",
    "26.3.1",
    "26.3.2",
    "26.3.3",
    "26.7.1",
]

# EDA 25.12.x — current default. Mixed graduated/pre-release groups.
_PROFILE_25_12 = Registry(
    eda_version="25.12",
    group_versions={
        "bootstrap": "v1alpha1",
        "core": "v1",
        "interfaces": "v1alpha1",
        "fabrics": "v1alpha1",
        "services": "v1",
        "protocols": "v1",
        "config": "v1alpha1",
        "siteinfo": "v1alpha1",
        "routingpolicies": "v1alpha1",
        # Verified against a live 25.12 cluster's /apps discovery.
        "aifabrics": "v1alpha1",
        "qos": "v1",
        "aaa": "v1alpha1",
        "topologies": "v1alpha1",
    },
    srl_support=_SRL_SUPPORT_CURRENT,
    tested_srl_versions=_TESTED_SRL_VERSIONS,
)

# EDA 26.4.x — verified against a freshly-installed live 26.4.2 cluster's
# ``/apps`` discovery + REST API. A *fresh* 26.4.2 install serves the new native
# models: ``services``/``protocols`` graduated to **v2** and every other group
# we use graduated ``v1alpha1`` -> **v1** (including ``bootstrap``). NOTE: an
# *in-place upgrade* from 25.12 keeps serving the OLD apiVersions (v1/v1alpha1)
# for already-migrated resources, so don't be fooled by an upgraded cluster —
# the fresh-install versions below are what 26.4.2 natively expects. The v2
# spec shapes differ enough from 25.12 that a dedicated CR-generation backend
# (``generator_variant="v2"``) + model package (``eda_models.eda_26_4``) are
# required.
_PROFILE_26_4 = Registry(
    eda_version="26.4",
    group_versions={
        "bootstrap": "v1",
        "core": "v1",
        "interfaces": "v1",
        "fabrics": "v1",
        "services": "v2",
        "protocols": "v2",
        "config": "v1",
        "siteinfo": "v1",
        "routingpolicies": "v1",
        # UNVERIFIED against a live 26.4 cluster — no 26.4 install was available
        # to read /apps discovery from. Carried over from 25.12 so the registry
        # is complete; the AI-fabric design refuses to generate against the v2
        # backend (see eda_generator_v2), so these are not silently used.
        "aifabrics": "v1alpha1",
        "qos": "v1",
        "aaa": "v1alpha1",
        "topologies": "v1alpha1",
    },
    srl_support=_SRL_SUPPORT_CURRENT,
    tested_srl_versions=_TESTED_SRL_VERSIONS,
    generator_variant="v2",
    # The AI-fabric group versions above are guesses carried over from 25.12,
    # and their 26.4 spec shapes were never read off a live cluster.
    supports_ai_fabrics=False,
)

# EDA 26.8.x — verified against a freshly-installed live 26.8.1 cluster
# (``/core/about/version`` -> ``v26.8.1-2608202030-ge91888ac``). The groups the
# fabric path uses are versioned exactly as on 26.4, and a property-by-property
# diff of the 26.4 OpenAPI specs against the 26.8 CRDs found the spec shapes to
# be *additive only* — no field removed or renamed — so 26.8 reuses the v2
# generator backend and the ``eda_models.eda_26_4`` model package unchanged.
#
# The four AI-fabric groups are where 26.8 diverges from 26.4: all of them
# graduated (they were unverified guesses on 26.4), and their spec shapes carry
# real renames (``systemPoolIPV4`` -> ``systemPoolIPv4``, ``nodeSelector`` ->
# ``nodeSelectors``, ``gpuVlan`` -> ``gpuVLAN``, ``pfcDeadlock*Timer`` ->
# ``*TimerMs``, ``Queue.queueType`` ``Pfc`` -> ``PFC``, ...). Those kinds are
# generated from the ``eda_models.eda_26_8`` package.
_PROFILE_26_8 = Registry(
    eda_version="26.8",
    group_versions={
        "bootstrap": "v1",
        "core": "v1",
        "interfaces": "v1",
        "fabrics": "v1",
        "services": "v2",
        "protocols": "v2",
        "config": "v1",
        "siteinfo": "v1",
        "routingpolicies": "v1",
        # Graduated in 26.8 — verified against the live cluster's
        # ``kubectl api-resources`` and ``/openapi/v3`` discovery.
        "aifabrics": "v1",
        "qos": "v2",
        "aaa": "v1",
        "topologies": "v1",
    },
    srl_support=_SRL_SUPPORT_26_8,
    tested_srl_versions=_TESTED_SRL_VERSIONS_26_8,
    generator_variant="v2",
)


# Profiles are keyed by ``major.minor`` (e.g. ``"26.4"``). Patch releases within
# a minor are not expected to introduce CR/API breaking changes, so any
# ``25.12.x`` maps to the ``25.12`` profile and any ``26.4.x`` to ``26.4``.
_PROFILES: dict[str, Registry] = {
    _PROFILE_25_12.eda_version: _PROFILE_25_12,
    _PROFILE_26_4.eda_version: _PROFILE_26_4,
    _PROFILE_26_8.eda_version: _PROFILE_26_8,
}

DEFAULT_EDA_VERSION = _PROFILE_25_12.eda_version


def normalize_eda_version(eda_version: str) -> str:
    """Reduce an EDA version string to its ``major.minor`` profile key.

    Accepts the many forms EDA reports/users pass — ``"26.4"``, ``"26.4.2"``,
    ``"v26.4.2-2605212019-g73187ba6"`` — and returns ``"26.4"``. Raises
    ``ValueError`` if no ``major.minor`` can be parsed.
    """
    m = re.search(r"(\d+)\.(\d+)", eda_version)
    if not m:
        raise ValueError(f"Cannot parse an EDA version from {eda_version!r}")
    return f"{m.group(1)}.{m.group(2)}"


def list_eda_versions() -> list[str]:
    """Return the EDA ``major.minor`` versions that have a registry profile."""
    return sorted(_PROFILES)


def get_registry(eda_version: str | None = None) -> Registry:
    """Return the :class:`Registry` profile for *eda_version*.

    *eda_version* is matched by ``major.minor`` (see
    :func:`normalize_eda_version`), so ``"26.4"``, ``"26.4.2"`` and the raw
    ``"v26.4.2-..."`` build string all select the ``26.4`` profile. With no
    argument (or ``None``) the default profile is returned. An unknown version
    raises ``ValueError`` listing the available profiles.
    """
    if eda_version is None:
        return _PROFILES[DEFAULT_EDA_VERSION]
    available = ", ".join(list_eda_versions())
    try:
        key = normalize_eda_version(eda_version)
    except ValueError:
        raise ValueError(
            f"No EDA registry profile for {eda_version!r}. Available: {available}"
        ) from None
    try:
        return _PROFILES[key]
    except KeyError:
        raise ValueError(
            f"No EDA registry profile for {eda_version!r} (matched {key!r}). "
            f"Available: {available}"
        ) from None


def get_default_registry() -> Registry:
    """Return the default EDA registry profile."""
    return _PROFILES[DEFAULT_EDA_VERSION]
