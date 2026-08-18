"""
EDA API executor.

Handles authentication (Keycloak), transaction submission,
result polling, and declarative pruning of managed resources.

Transaction API format (from OpenAPI spec at /openapi/v3/core):
  POST /core/transaction/v2
  Body: {
      "description": "...",
      "dryRun": true|false,
      "crs": [
          {"type": {"create": {"value": {apiVersion, kind, metadata, spec}}}},
          {"type": {"replace": {"value": {apiVersion, kind, metadata, spec}}}},
          {"type": {"delete": {"gvk": {group, version, kind}, "name": "...", "namespace": "..."}}},
      ]
  }
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3

from automation.generators.eda_generator import (
    DERIVED_SOURCE_LABEL,
    DERIVED_SOURCE_VALUE,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
)
from automation.eda_models.registry import CRType
from automation.eda_models.profiles import Registry, get_default_registry

# Suppress InsecureRequestWarning for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deployment phases
# ---------------------------------------------------------------------------

PHASE_TOPOLOGY = "topology"
PHASE_FABRIC = "fabric"
PHASE_SERVICES = "services"
ALL_PHASES = [PHASE_TOPOLOGY, PHASE_FABRIC, PHASE_SERVICES]

# CR kind → phase mapping (for deploy)
# Note: Interface CRs are split by role label in _split_by_phase(),
# not by this mapping. ISL Interface CRs (role=interSwitch) go to
# topology phase; all others go to services.
PHASE_KINDS: dict[str, set[str]] = {
    PHASE_TOPOLOGY: {
        "Init", "NodeUser", "NodeProfile",
        "IndexAllocationPool", "IPAllocationPool",
        "TopoNode", "TopoLink", "DefaultMTU", "Banner",
    },
    PHASE_FABRIC: {"Fabric"},
    # Policy/PrefixSet ship with services: they are user-declared policies
    # consumed by service CRs (e.g. Router import/export). Fabric's
    # underlay/overlay policies are handled natively by EDA's Fabric
    # reconciler and are not emitted as standalone CRs.
    PHASE_SERVICES: {
        "BridgeDomain", "Router", "IRBInterface",
        "VLAN", "RoutedInterface", "StaticRoute", "Configlet",
        "Policy", "PrefixSet",
    },
}

# Reverse map for O(1) lookup in _split_by_phase
_KIND_TO_PHASE: dict[str, str] = {
    kind: phase
    for phase, kinds in PHASE_KINDS.items()
    for kind in kinds
}

# Shared/singleton resource kinds that are NOT destroyed by default.
# These bootstrap resources (Init, NodeUser, NodeProfile) are typically
# shared across multiple designs and should survive a destroy operation.
SHARED_KINDS: set[str] = {"Init", "NodeUser", "NodeProfile"}

# CR kind → phase mapping (for destroy)
# Interface is grouped with topology because EDA ISLs (auto-created from
# TopoLinks) reference Interface CRs — you can't delete Interfaces while
# TopoLinks still exist.
DESTROY_PHASE_KINDS: dict[str, set[str]] = {
    PHASE_SERVICES: {
        "BridgeDomain", "Router", "IRBInterface",
        "VLAN", "RoutedInterface", "StaticRoute", "Configlet",
        "Policy", "PrefixSet",
    },
    PHASE_FABRIC: {"Fabric"},
    PHASE_TOPOLOGY: {
        "Interface",  # must be deleted with/after TopoLinks
        "TopoLink", "TopoNode", "DefaultMTU", "Banner",
        "IPAllocationPool", "IndexAllocationPool",
    },
}

# Resource kinds in correct destroy order (services first, topo last).
# Interface appears AFTER TopoLink because ISLs reference Interfaces.
# Resolved to CR types per-instance via the client's registry profile.
DESTROY_ORDER_KINDS: list[str] = [
    # --- services ---
    "Configlet", "StaticRoute", "RoutedInterface",
    "VLAN", "IRBInterface", "Router", "BridgeDomain",
    # Policy/PrefixSet come after the service CRs that reference them
    # (e.g. Router.importPolicy) and before Fabric.
    "Policy", "PrefixSet",
    # --- fabric ---
    "Fabric",
    # --- topology (Interface after TopoLink!) ---
    "TopoLink", "Interface", "DefaultMTU", "Banner",
    "TopoNode", "IPAllocationPool", "IndexAllocationPool",
    "NodeProfile", "NodeUser", "Init",
]


# Spec fields that cannot be compared against live state, per CR kind.
#
# EDA stores credentials hashed and never returns the plaintext, so a password
# always looks different. The NodeProfile schema/LLM fields are overwritten from
# EDA's reference ``srlinux-ghcr-<version>`` profile by
# :meth:`EdaClient._enrich_node_profiles` at apply time, so the generated values
# are never the authoritative ones and comparing them reports a permanent diff.
UNCOMPARABLE_SPEC_FIELDS: dict[str, frozenset[str]] = {
    "NodeUser": frozenset({"password"}),
    "NodeProfile": frozenset(
        {"onboardingPassword", "yang", "versionMatch", "versionPath", "llmDb"}
    ),
}


def _values_match(desired: Any, current: Any) -> bool:
    """Compare a desired value against live state, ignoring EDA's additions.

    Comparison is *declared-intent subset* semantics: only what the generator
    puts in the CR has to match. Keys EDA populated on its own (defaults,
    computed fields) are ignored, because the generator never claimed them.

    Lists of scalars are compared order-insensitively — EDA is free to reorder
    a label or selector list, and that is not a change of intent. Lists of
    objects are compared positionally, since their order can be meaningful.
    """
    if isinstance(desired, dict):
        if not isinstance(current, dict):
            return False
        return all(
            key in current and _values_match(val, current[key])
            for key, val in desired.items()
        )

    if isinstance(desired, list):
        if not isinstance(current, list) or len(desired) != len(current):
            return False
        if all(not isinstance(v, (dict, list)) for v in desired):
            return sorted(desired, key=repr) == sorted(current, key=repr)
        return all(_values_match(d, c) for d, c in zip(desired, current))

    return desired == current


def cr_matches_live_state(desired: dict, current: dict) -> bool:
    """Return True when *desired* is already fully reflected in *current*.

    Compares the generator-owned parts of the CR only: the labels it sets and
    the spec fields it declares, minus the per-kind fields listed in
    :data:`UNCOMPARABLE_SPEC_FIELDS`. Server-managed metadata (``resourceVersion``,
    ``generation``, ``creationTimestamp``, …) and ``status`` are never consulted.
    """
    desired_labels = (desired.get("metadata") or {}).get("labels") or {}
    current_labels = (current.get("metadata") or {}).get("labels") or {}
    if not _values_match(desired_labels, current_labels):
        return False

    kind = desired.get("kind", "")
    skip = UNCOMPARABLE_SPEC_FIELDS.get(kind, frozenset())
    desired_spec = {
        k: v for k, v in (desired.get("spec") or {}).items() if k not in skip
    }
    return _values_match(desired_spec, current.get("spec") or {})


@dataclass
class TransactionPlan:
    """Computed diff of desired vs. current state."""

    creates: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    deletes: list[dict] = field(default_factory=list)
    # Desired CRs already reflected in EDA. Still submitted on apply (replace
    # is idempotent), but excluded from total_ops so a converged fabric plans
    # zero operations.
    unchanged: list[dict] = field(default_factory=list)

    @property
    def total_ops(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.deletes)

    def summary(self) -> str:
        return (
            f"Transaction plan: "
            f"{len(self.creates)} create, "
            f"{len(self.updates)} update, "
            f"{len(self.deletes)} delete, "
            f"{len(self.unchanged)} unchanged"
        )

    def counts(self) -> dict[str, int]:
        """Plan sizes, for the machine-readable deploy summary."""
        return {
            "creates": len(self.creates),
            "updates": len(self.updates),
            "deletes": len(self.deletes),
            "unchanged": len(self.unchanged),
        }


@dataclass
class TransactionResult:
    """Result of an EDA transaction execution."""

    success: bool
    transaction_id: str = ""
    message: str = ""
    details: dict = field(default_factory=dict)


class EdaClient:
    """
    Client for the Nokia EDA API.

    Handles Keycloak authentication, transaction submission via
    /core/transaction/v2, result polling, and declarative pruning.

    Authentication requires only username/password. The eda-api-server
    client secret is auto-discovered from the Keycloak admin API.
    """

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool = False,
        registry: Registry | None = None,
    ):
        self.url = (url or os.environ.get("EDA_URL", "")).rstrip("/")
        self.username = username or os.environ.get("EDA_USER", "admin")
        self.password = password or os.environ.get("EDA_PASSWORD", "admin")
        self.verify_ssl = verify_ssl
        self.registry = registry or get_default_registry()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client_secret: str | None = None
        self._keycloak_base: str | None = None
        self._ref_profile_cache: dict[tuple[str, str], dict | None] = {}
        # Plan from the most recent apply(), for callers that report counts.
        self.last_plan: TransactionPlan | None = None
        self._session = requests.Session()
        self._session.verify = self.verify_ssl

        if not self.url:
            raise ValueError(
                "EDA URL must be provided via --eda-url or EDA_URL env var"
            )

    def _destroy_order(self) -> list[CRType]:
        """CR types in destroy order, resolved against the active registry."""
        return [
            self.registry.BY_KIND[kind]
            for kind in DESTROY_ORDER_KINDS
            if kind in self.registry.BY_KIND
        ]

    @staticmethod
    def _extract_namespace(resources: list[dict], default: str = "eda") -> str:
        """Extract the namespace from the first resource in the list."""
        for cr in resources:
            ns = cr.get("metadata", {}).get("namespace", "")
            if ns:
                return ns
        return default

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    # EDA's Keycloak/identity reverse-proxy base path moved between releases.
    # 26.4.x serves it under ``/core/proxy/v1/identity``; 25.12 and earlier use
    # ``/core/httpproxy/v1/keycloak``. Probe in newest-first order and cache.
    _KEYCLOAK_BASE_CANDIDATES = (
        "/core/proxy/v1/identity",
        "/core/httpproxy/v1/keycloak",
    )

    def _resolve_keycloak_base(self) -> str:
        """Return the working Keycloak base URL for this EDA release.

        Probes the candidate proxy paths against the ``eda`` realm's OIDC
        discovery document and caches the first that responds 200.
        """
        if self._keycloak_base is not None:
            return self._keycloak_base

        for path in self._KEYCLOAK_BASE_CANDIDATES:
            base = f"{self.url}{path}"
            probe = f"{base}/realms/eda/.well-known/openid-configuration"
            try:
                resp = self._session.get(probe, timeout=10)
            except requests.RequestException:
                continue
            if resp.status_code == 200:
                logger.info("Resolved Keycloak base path: %s", path)
                self._keycloak_base = base
                return base

        # Fall back to the legacy path; authenticate() will surface a clear
        # HTTP error if it is also unavailable.
        fallback = f"{self.url}{self._KEYCLOAK_BASE_CANDIDATES[-1]}"
        logger.warning(
            "Could not probe a Keycloak base path; falling back to %s",
            fallback,
        )
        self._keycloak_base = fallback
        return fallback

    def authenticate(self) -> str:
        """
        Authenticate with EDA's Keycloak identity provider.

        Uses a two-step flow:
        1. Get admin token from Keycloak master realm (public admin-cli client)
        2. Use that to discover the eda-api-server client secret
        3. Get a token scoped to the EDA API using the discovered secret

        Only username and password are required.
        """
        keycloak_base = self._resolve_keycloak_base()

        # Step 1: Discover eda-api-server client secret (cached per-session)
        if self._client_secret is None:
            logger.info("Discovering EDA API client secret via Keycloak")
            self._client_secret = self._discover_client_secret(keycloak_base)
        client_secret = self._client_secret

        # Step 2: Get EDA API token
        token_url = f"{keycloak_base}/realms/eda/protocol/openid-connect/token"
        payload = {
            "client_id": "eda-api-server",
            "client_secret": client_secret,
            "grant_type": "password",
            "scope": "openid",
            "username": self.username,
            "password": self.password,
        }

        logger.info("Authenticating with EDA at %s", self.url)
        resp = self._session.post(token_url, data=payload)
        resp.raise_for_status()

        token_data = resp.json()
        self._token = token_data["access_token"]
        self._session.headers["Authorization"] = f"Bearer {self._token}"

        # Store expiry with 30s safety margin
        expires_in = token_data.get("expires_in", 300)
        self._token_expires_at = time.time() + expires_in - 30

        logger.info("Authentication successful (token expires in %ds)", expires_in)
        return self._token

    def _discover_client_secret(self, keycloak_base: str) -> str:
        """
        Discover the eda-api-server client secret from Keycloak.

        Uses the master realm admin-cli (public client, no secret needed)
        to authenticate, then queries the Keycloak admin API for the
        eda-api-server client credentials.
        """
        # Get master realm admin token
        master_token_url = (
            f"{keycloak_base}/realms/master/protocol/openid-connect/token"
        )
        resp = self._session.post(
            master_token_url,
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
            },
        )
        resp.raise_for_status()
        master_token = resp.json()["access_token"]

        # Query Keycloak admin API for eda-api-server client
        headers = {"Authorization": f"Bearer {master_token}"}
        clients_url = f"{keycloak_base}/admin/realms/eda/clients"
        resp = self._session.get(
            clients_url,
            params={"clientId": "eda-api-server"},
            headers=headers,
        )
        resp.raise_for_status()

        clients = resp.json()
        if not clients:
            raise ValueError(
                "Could not find 'eda-api-server' client in Keycloak. "
                "Ensure EDA is properly installed."
            )

        client_uuid = clients[0]["id"]

        # Get the client secret
        secret_url = (
            f"{keycloak_base}/admin/realms/eda/clients/{client_uuid}/client-secret"
        )
        resp = self._session.get(secret_url, headers=headers)
        resp.raise_for_status()

        secret = resp.json().get("value", "")
        if not secret:
            raise ValueError(
                "eda-api-server client secret is empty in Keycloak."
            )

        logger.info("Discovered EDA API client secret from Keycloak")
        return secret

    def _ensure_auth(self) -> None:
        """Ensure we have a valid, non-expired token."""
        if not self._token or time.time() >= self._token_expires_at:
            if self._token:
                logger.info("Token expired or near expiry, re-authenticating")
            self.authenticate()

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """Make an authenticated request with a single 401 retry.

        If the response is 401 Unauthorized, clears the token, re-authenticates,
        and retries exactly once. This handles mid-session token expiry that
        slips past the proactive expiry check in ``_ensure_auth``.
        """
        self._ensure_auth()
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.warning("Got 401, re-authenticating and retrying")
            self._token = None
            self._ensure_auth()
            resp = self._session.request(method, url, **kwargs)
        return resp

    def get_eda_version(self) -> str | None:
        """Return the running EDA release (e.g. ``"26.4.2"``), or ``None``.

        Queries the authenticated ``/core/about/version`` endpoint and parses
        the ``eda.version`` build string (``"v26.4.2-2605212019-g73187ba6"``).
        Returns ``None`` if the endpoint is unavailable or unparseable so the
        caller can fall back to a default profile.
        """
        url = f"{self.url}/core/about/version"
        try:
            resp = self._request("GET", url, timeout=10)
            if resp.status_code != 200:
                logger.warning("Could not query EDA version: HTTP %d", resp.status_code)
                return None
            raw = (resp.json().get("eda") or {}).get("version", "")
        except (requests.RequestException, ValueError) as e:
            logger.warning("Could not query EDA version: %s", e)
            return None
        m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", raw)
        if not m:
            logger.warning("Could not parse EDA version from %r", raw)
            return None
        return m.group(1)

    # ------------------------------------------------------------------
    # Transaction payload formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_cr_create(cr: dict) -> dict:
        """Wrap a k8s-style CR dict into EDA transaction create format."""
        return {
            "type": {
                "create": {
                    "value": cr,
                }
            }
        }

    @staticmethod
    def _wrap_cr_replace(cr: dict) -> dict:
        """Wrap a k8s-style CR dict into EDA transaction replace format."""
        return {
            "type": {
                "replace": {
                    "value": cr,
                }
            }
        }

    @staticmethod
    def _wrap_cr_delete(api_version: str, kind: str, name: str, namespace: str = "eda") -> dict:
        """Create an EDA transaction delete entry."""
        # Parse apiVersion into group + version
        parts = api_version.rsplit("/", 1)
        if len(parts) == 2:
            group, version = parts
        else:
            group, version = "", parts[0]

        return {
            "type": {
                "delete": {
                    "gvk": {
                        "group": group,
                        "version": version,
                        "kind": kind,
                    },
                    "name": name,
                    "namespace": namespace,
                }
            }
        }

    # ------------------------------------------------------------------
    # Resource queries (for prune support)
    # ------------------------------------------------------------------

    def get_managed_resources(
        self, namespace: str = "eda", phases: list[str] | None = None,
    ) -> list[dict]:
        """
        Query EDA for all resources carrying the managed-by label.

        Args:
            namespace: EDA namespace to query
            phases: Optional list of phases to filter by. If None, return all.

        Returns:
            List of managed resource dicts with _kind and _apiVersion injected.
        """
        self._ensure_auth()

        # Determine which kinds to query based on phase filter
        if phases:
            target_kinds: set[str] = set()
            for p in phases:
                target_kinds |= DESTROY_PHASE_KINDS.get(p, set())
        else:
            target_kinds = None  # query all

        resource_types = [
            crt for crt in self._destroy_order()
            if target_kinds is None or crt.kind in target_kinds
        ]

        params = {"labelSelector": f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"}

        def _fetch(crt: CRType) -> list[dict]:
            url = (
                f"{self.url}/apps/{crt.api_version}"
                f"/namespaces/{namespace}/{crt.plural}"
            )
            try:
                resp = self._request("GET", url, params=params)
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    for item in items:
                        item["_kind"] = crt.kind
                        item["_apiVersion"] = crt.api_version
                    return items
                logger.warning(
                    "Could not query %s: %s", crt.plural, resp.status_code
                )
            except Exception as e:
                logger.warning("Error querying %s: %s", crt.plural, e)
            return []

        managed: list[dict] = []
        # Parallel fetch — each kind lives on an independent list endpoint,
        # so 8 concurrent requests cuts wall-clock time by roughly that factor.
        with ThreadPoolExecutor(max_workers=8) as pool:
            for items in pool.map(_fetch, resource_types):
                managed.extend(items)

        # Filter out resources marked as derived by another EDA resource.
        # Example: the Policy/PrefixSet that EDA's Fabric reconciler creates
        # to materialise the eBGP ISL routing-policies inherit our parent
        # Fabric's ``managed-by`` label, so they appear in this query — but
        # they are owned by Fabric and must not be pruned/destroyed directly.
        kept: list[dict] = []
        derived: list[str] = []
        for item in managed:
            labels = item.get("metadata", {}).get("labels") or {}
            if labels.get(DERIVED_SOURCE_LABEL) == DERIVED_SOURCE_VALUE:
                derived.append(
                    f"{item.get('_kind', '?')}/"
                    f"{item.get('metadata', {}).get('name', '?')}"
                )
            else:
                kept.append(item)

        if derived:
            logger.info(
                "Skipping %d derived resources (owned by another EDA resource): %s",
                len(derived),
                ", ".join(sorted(derived)),
            )

        logger.info("Found %d managed resources in EDA", len(kept))
        return kept

    # ------------------------------------------------------------------
    # TopoNode existence check (avoid re-onboarding)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # NodeProfile enrichment
    # ------------------------------------------------------------------

    def _get_reference_node_profile(
        self, version: str, namespace: str = "eda"
    ) -> dict | None:
        """Fetch the EDA-managed ``srlinux-ghcr-{version}`` NodeProfile spec.

        Cached per (version, namespace) — the reference profile is immutable
        for the lifetime of the client, so repeated lookups (e.g. across
        multiple NodeProfile CRs sharing a version) reuse a single API call.
        """
        cache_key = (version, namespace)
        if cache_key in self._ref_profile_cache:
            return self._ref_profile_cache[cache_key]

        self._ensure_auth()
        name = f"srlinux-ghcr-{version}"
        url = (
            f"{self.url}/apps/{self.registry.NODE_PROFILE.api_version}"
            f"/namespaces/{namespace}/{self.registry.NODE_PROFILE.plural}/{name}"
        )
        spec: dict | None = None
        try:
            resp = self._request("GET", url)
            if resp.status_code == 200:
                spec = resp.json().get("spec", {})
                logger.info(
                    "Fetched reference NodeProfile %s from EDA", name
                )
            else:
                logger.warning(
                    "Reference NodeProfile %s not found (HTTP %d)",
                    name, resp.status_code,
                )
        except Exception as e:
            logger.warning(
                "Error fetching reference NodeProfile %s: %s", name, e
            )

        self._ref_profile_cache[cache_key] = spec
        return spec

    def _enrich_node_profiles(self, crs: list[dict], namespace: str = "eda") -> list[dict]:
        """Enrich NodeProfile CRs with version-specific fields from EDA.

        For each NodeProfile CR, look up the matching
        ``srlinux-ghcr-{version}`` profile already installed in EDA and
        copy over the ``yang``, ``versionMatch``, ``versionPath``, and
        ``llmDb`` fields.  Clab-specific fields (``images``, ``port``,
        ``annotate``) are preserved from the generated CR.
        """
        result: list[dict] = []
        for cr in crs:
            if cr.get("kind") != "NodeProfile":
                result.append(cr)
                continue

            spec = cr.get("spec", {})
            version = spec.get("version", "")
            if not version:
                result.append(cr)
                continue

            ref = self._get_reference_node_profile(version, namespace=namespace)
            if ref:
                for field in ("yang", "versionMatch", "versionPath", "llmDb"):
                    if field in ref:
                        spec[field] = ref[field]
                logger.info(
                    "Enriched NodeProfile %s with fields from srlinux-ghcr-%s",
                    cr.get("metadata", {}).get("name", ""),
                    version,
                )
            result.append(cr)
        return result

    # ------------------------------------------------------------------
    # TopoNode helpers
    # ------------------------------------------------------------------

    def _get_existing_toponode_names(self, namespace: str = "eda") -> set[str]:
        """
        Query EDA for TopoNodes that already exist.

        TopoNode is special in EDA: re-applying via ``replace`` always
        triggers full re-evaluation and re-onboarding, even when the
        spec is unchanged.  By querying first we can skip existing
        TopoNodes and only ``create`` genuinely new ones.
        """
        self._ensure_auth()
        url = (
            f"{self.url}/apps/{self.registry.TOPO_NODE.api_version}"
            f"/namespaces/{namespace}/{self.registry.TOPO_NODE.plural}"
        )
        try:
            resp = self._request("GET", url)
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                names = {
                    item.get("metadata", {}).get("name", "")
                    for item in items
                }
                names.discard("")
                logger.info(
                    "Found %d existing TopoNodes in EDA: %s",
                    len(names),
                    ", ".join(sorted(names)) if names else "(none)",
                )
                return names
            else:
                logger.warning(
                    "Could not query existing TopoNodes (HTTP %d), "
                    "will use replace for all",
                    resp.status_code,
                )
                return set()
        except Exception as e:
            logger.warning(
                "Error querying existing TopoNodes: %s, "
                "will use replace for all",
                e,
            )
            return set()

    # ------------------------------------------------------------------
    # Diff computation (for prune)
    # ------------------------------------------------------------------

    def compute_diff(
        self, desired: list[dict], current: list[dict]
    ) -> TransactionPlan:
        """
        Compute the diff between desired and current state.

        Resources are matched by ``kind:name``. A desired CR that already
        exists is compared against live state with :func:`cr_matches_live_state`
        and lands in ``updates`` only when its declared labels or spec fields
        actually differ, so a converged fabric plans zero operations.

        Returns a TransactionPlan with creates, updates, deletes, and unchanged.
        """
        desired_keys: dict[str, dict] = {}
        for cr in desired:
            key = f"{cr['kind']}:{cr['metadata']['name']}"
            desired_keys[key] = cr

        current_keys: dict[str, dict] = {}
        for cr in current:
            kind = cr.get("_kind", cr.get("kind", ""))
            name = cr.get("metadata", {}).get("name", "")
            key = f"{kind}:{name}"
            current_keys[key] = cr

        plan = TransactionPlan()

        # Creates: in desired but not in current. Otherwise compare content.
        for key, cr in desired_keys.items():
            if key not in current_keys:
                plan.creates.append(cr)
            elif cr_matches_live_state(cr, current_keys[key]):
                plan.unchanged.append(cr)
            else:
                plan.updates.append(cr)

        # Deletes: in current but not in desired
        for key, cr in current_keys.items():
            if key not in desired_keys:
                kind = cr.get("_kind", cr.get("kind", ""))
                name = cr.get("metadata", {}).get("name", "")
                api_version = cr.get("_apiVersion", cr.get("apiVersion", ""))
                plan.deletes.append(
                    {
                        "apiVersion": api_version,
                        "kind": kind,
                        "name": name,
                        "namespace": cr.get("metadata", {}).get("namespace", ""),
                    }
                )

        return plan

    # ------------------------------------------------------------------
    # Transaction submission
    # ------------------------------------------------------------------

    def submit_transaction(
        self,
        crs: list[dict],
        dry_run: bool = False,
        description: str = "NVD automation deployment",
    ) -> str:
        """
        Submit a transaction to EDA.

        Args:
            crs: List of wrapped CR dicts (already in transaction format)
            dry_run: If True, validate only without applying
            description: Human-readable description

        Returns:
            Transaction ID (as string)
        """
        self._ensure_auth()

        tx_url = f"{self.url}/core/transaction/v2"

        payload: dict[str, Any] = {
            "description": description,
            "dryRun": dry_run,
            "crs": crs,
        }

        logger.info(
            "Submitting transaction with %d CRs (dry_run=%s)",
            len(crs),
            dry_run,
        )

        resp = self._request("POST", tx_url, json=payload)
        resp.raise_for_status()

        result = resp.json()
        tx_id = str(result.get("id", ""))

        logger.info("Transaction submitted: %s", tx_id)
        return tx_id

    # ------------------------------------------------------------------
    # Transaction polling
    # ------------------------------------------------------------------

    def poll_transaction(
        self, tx_id: str, timeout: int = 300, interval: int = 5
    ) -> TransactionResult:
        """
        Poll for transaction completion.

        Uses exponential backoff (1s → 2s → 4s → ``interval``) so that short
        transactions finish quickly while long-running ones settle at the
        configured steady-state interval.

        Args:
            tx_id: Transaction ID from submit_transaction
            timeout: Maximum wait time in seconds
            interval: Steady-state (and maximum) polling interval in seconds

        Returns:
            TransactionResult
        """
        self._ensure_auth()

        # Use the summary endpoint which has clear state/success fields
        summary_url = f"{self.url}/core/transaction/v2/result/summary/{tx_id}"
        execution_url = f"{self.url}/core/transaction/v2/result/execution/{tx_id}"

        backoff = 1.0
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = self._request("GET", summary_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if not data:
                        logger.debug("Transaction %s: empty response", tx_id)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, float(interval))
                        continue
                    state = (data.get("state") or "").lower()

                    if state == "complete":
                        success = data.get("success", False)

                        # Fetch execution details for error info
                        details = data
                        try:
                            exec_resp = self._request("GET", execution_url)
                            if exec_resp.status_code == 200:
                                exec_data = exec_resp.json()
                                if exec_data:
                                    details = exec_data
                        except Exception:
                            pass

                        if success:
                            logger.info("Transaction %s completed successfully", tx_id)
                            return TransactionResult(
                                success=True,
                                transaction_id=tx_id,
                                message="Transaction completed successfully",
                                details=details,
                            )
                        else:
                            # Extract error messages
                            errors = details.get("generalErrors") or []
                            err_summary = "; ".join(errors[:5])
                            if len(errors) > 5:
                                err_summary += f" (+{len(errors) - 5} more)"
                            logger.error(
                                "Transaction %s failed: %s", tx_id, err_summary
                            )
                            return TransactionResult(
                                success=False,
                                transaction_id=tx_id,
                                message=f"Transaction failed: {err_summary}",
                                details=details,
                            )
                    else:
                        logger.debug(
                            "Transaction %s state: %s", tx_id, state
                        )
                elif resp.status_code == 404:
                    logger.debug("Transaction %s not yet available", tx_id)
                else:
                    logger.warning(
                        "Unexpected status %d polling transaction %s",
                        resp.status_code,
                        tx_id,
                    )
            except Exception as e:
                logger.warning("Error polling transaction %s: %s", tx_id, e)

            time.sleep(backoff)
            backoff = min(backoff * 2, float(interval))

        return TransactionResult(
            success=False,
            transaction_id=tx_id,
            message=f"Transaction timed out after {timeout}s",
        )

    # ------------------------------------------------------------------
    # High-level apply (phased deployment)
    # ------------------------------------------------------------------

    def apply(
        self,
        resources: list[dict],
        phases: list[str] | None = None,
        prune: bool = False,
        dry_run: bool = False,
        auto_confirm: bool = False,
    ) -> TransactionResult:
        """
        Phased deployment workflow.

        Splits resources into three phases and executes them in order:
          Phase 1 (topology): Init, NodeProfile, NodeUser, pools, TopoNodes, TopoLinks
          Phase 2 (fabric):   Fabric CR
          Phase 3 (services): Interfaces, BridgeDomains, Routers, IRBs, VLANs, etc.

        If `phases` is provided, only the specified phases are executed.
        Between Phase 1 and Phase 2, waits for TopoNodes to sync.

        Live state is queried up front to plan the transaction; the plan is
        left on :attr:`last_plan`.

        Args:
            resources: List of desired-state CRs (k8s-style dicts)
            phases: Which phases to run (default: all)
            prune: If True, delete resources not in desired state
            dry_run: If True, validate only
            auto_confirm: If True, skip prune confirmation

        Returns:
            TransactionResult (from the last executed phase)
        """
        self.authenticate()
        run_phases = phases or ALL_PHASES
        ns = self._extract_namespace(resources)

        # Plan against live state. Required for --prune, and recorded on
        # ``last_plan`` either way so callers can report real counts.
        current = self.get_managed_resources(namespace=ns, phases=run_phases)
        plan = self.compute_diff(resources, current)
        self.last_plan = plan
        logger.info(plan.summary())

        # Every desired CR is still submitted, including the unchanged ones:
        # replace is idempotent, and trusting the comparison to skip work would
        # turn a false "unchanged" verdict into missing device config.
        all_desired = plan.creates + plan.updates + plan.unchanged

        delete_crs: list[dict] = []
        if prune:
            if plan.deletes and not dry_run:
                print(f"\n⚠️  Prune will DELETE {len(plan.deletes)} resources:")
                for d in plan.deletes:
                    print(f"   - {d['kind']}: {d['name']}")
                if not auto_confirm:
                    confirm = input("\nProceed? [y/N]: ")
                    if confirm.lower() != "y":
                        return TransactionResult(
                            success=False,
                            message="Prune cancelled by user",
                        )
            delete_crs = plan.deletes

        # Enrich NodeProfile CRs with version-specific fields from EDA
        all_desired = self._enrich_node_profiles(all_desired, namespace=ns)

        # Split CRs into phases
        phased = self._split_by_phase(all_desired)

        last_result = TransactionResult(success=True, message="No changes to apply")

        # Phase 1: Topology
        if PHASE_TOPOLOGY in run_phases and phased.get(PHASE_TOPOLOGY):
            topo_crs = phased[PHASE_TOPOLOGY]

            # TopoNodes need special handling: re-applying an existing
            # TopoNode via replace triggers full re-onboarding. Query
            # EDA first and only create genuinely new ones.
            existing_nodes = self._get_existing_toponode_names(namespace=ns)
            new_topo_nodes: list[dict] = []
            skipped_nodes: list[str] = []
            other_crs: list[dict] = []

            for cr in topo_crs:
                if cr.get("kind") == "TopoNode":
                    name = cr.get("metadata", {}).get("name", "")
                    if name in existing_nodes:
                        skipped_nodes.append(name)
                    else:
                        new_topo_nodes.append(cr)
                else:
                    other_crs.append(cr)

            if skipped_nodes:
                logger.info(
                    "Skipping %d existing TopoNodes (already onboarded): %s",
                    len(skipped_nodes),
                    ", ".join(sorted(skipped_nodes)),
                )

            # Build the topology transaction:
            #   - other CRs use replace (idempotent upsert)
            #   - new TopoNodes use create (avoids re-onboarding)
            result = self._submit_phase(
                crs=other_crs,
                create_crs=new_topo_nodes,
                description="NVD topology",
                dry_run=dry_run,
                phase_label="Phase 1 (topology)",
            )
            if not result.success:
                return result
            last_result = result

            # Wait for ALL TopoNodes (new + existing) to be Synced
            # before proceeding to fabric/services phases.
            if not dry_run:
                all_node_names = (
                    [cr.get("metadata", {}).get("name", "") for cr in new_topo_nodes]
                    + skipped_nodes
                )
                if all_node_names:
                    topo_ns = self._extract_namespace(resources)
                    all_node_stubs = [
                        {"metadata": {"name": n, "namespace": topo_ns},
                         "apiVersion": self.registry.TOPO_NODE.api_version}
                        for n in all_node_names
                    ]
                    self._wait_for_nodes_sync(all_node_stubs)

        # Phase 2: Fabric
        if PHASE_FABRIC in run_phases and phased.get(PHASE_FABRIC):
            result = self._submit_phase(
                crs=phased[PHASE_FABRIC],
                description="NVD fabric",
                dry_run=dry_run,
                phase_label="Phase 2 (fabric)",
            )
            if not result.success:
                return result
            last_result = result

        # Phase 3: Services (+ any prune deletes)
        if PHASE_SERVICES in run_phases:
            svc_crs = phased.get(PHASE_SERVICES, [])
            if svc_crs or delete_crs:
                result = self._submit_phase(
                    crs=svc_crs,
                    delete_crs=delete_crs,
                    description="NVD services",
                    dry_run=dry_run,
                    phase_label="Phase 3 (services)",
                )
                if not result.success:
                    return result
                last_result = result

        return last_result

    # ------------------------------------------------------------------
    # Destroy (reverse-order deletion of managed resources)
    # ------------------------------------------------------------------

    def destroy(
        self,
        phases: list[str] | None = None,
        dry_run: bool = False,
        auto_confirm: bool = False,
        namespace: str = "eda",
    ) -> TransactionResult:
        """
        Remove all managed resources in reverse dependency order.

        Queries EDA for resources with the managed-by label, then
        deletes them in a single atomic transaction ordered by
        DESTROY_ORDER (services → fabric → topology, with Interfaces
        placed after TopoLinks).

        If `phases` is provided, only destroy resources in those phases
        (using DESTROY_PHASE_KINDS for grouping: Interface is in topology,
        not services).

        Args:
            phases: Which phases to destroy (default: all)
            dry_run: If True, validate only
            auto_confirm: If True, skip confirmation
            namespace: EDA namespace

        Returns:
            TransactionResult
        """
        self.authenticate()
        destroy_phases = phases or ALL_PHASES

        # Query managed resources, scoped to requested phases
        managed = self.get_managed_resources(
            namespace=namespace, phases=destroy_phases,
        )

        if not managed:
            print("\n✅ No managed resources found to destroy.")
            return TransactionResult(
                success=True, message="No resources to destroy",
            )

        # Group by kind for display
        by_kind: dict[str, list[str]] = {}
        for cr in managed:
            kind = cr.get("_kind", "?")
            name = cr.get("metadata", {}).get("name", "?")
            by_kind.setdefault(kind, []).append(name)

        print(f"\n⚠️  Will DESTROY {len(managed)} managed resources:")
        for kind, names in by_kind.items():
            print(f"   {kind}: {', '.join(sorted(names))}")

        if not auto_confirm and not dry_run:
            confirm = input("\nType 'destroy' to confirm: ")
            if confirm.strip() != "destroy":
                return TransactionResult(
                    success=False, message="Destroy cancelled by user",
                )

        # Build ordered delete list following DESTROY_ORDER
        # (correct dependency order within a single transaction)
        managed_by_kind: dict[str, list[dict]] = {}
        for cr in managed:
            kind = cr.get("_kind", "")
            if kind in SHARED_KINDS:
                continue
            managed_by_kind.setdefault(kind, []).append(cr)

        tx_crs: list[dict] = []
        for crt in self._destroy_order():
            if crt.kind in SHARED_KINDS:
                continue
            for cr in managed_by_kind.get(crt.kind, []):
                name = cr.get("metadata", {}).get("name", "")
                tx_crs.append(
                    self._wrap_cr_delete(crt.api_version, crt.kind, name, namespace)
                )

        if not tx_crs:
            return TransactionResult(
                success=True, message="No resources to destroy",
            )

        desc = "NVD destroy"
        if phases:
            desc += f" ({', '.join(phases)})"
        if dry_run:
            desc += " (dry-run)"

        logger.info(
            "Destroying %d resources in a single transaction%s",
            len(tx_crs),
            " (dry-run)" if dry_run else "",
        )

        tx_id = self.submit_transaction(
            tx_crs, dry_run=dry_run, description=desc,
        )
        result = self.poll_transaction(tx_id)

        if result.success:
            logger.info("Destroy transaction %s succeeded", tx_id)
        else:
            logger.error(
                "Destroy transaction %s failed: %s", tx_id, result.message,
            )

        return result

    # ------------------------------------------------------------------
    # Internal: phase submission
    # ------------------------------------------------------------------

    def _submit_phase(
        self,
        *,
        crs: list[dict],
        create_crs: list[dict] | None = None,
        delete_crs: list[dict] | None = None,
        description: str,
        dry_run: bool,
        phase_label: str,
    ) -> TransactionResult:
        """Submit a transaction for a set of CRs and poll for result.

        Args:
            crs: CRs to upsert via ``replace`` (idempotent for most kinds).
            create_crs: CRs to submit via ``create`` (fails if already
                exists — used for TopoNodes to avoid re-onboarding).
            delete_crs: CRs to delete.
            description: Human-readable transaction description.
            dry_run: Validate only.
            phase_label: Label for log messages.
        """
        tx_crs: list[dict] = [self._wrap_cr_replace(cr) for cr in crs]
        for cr in (create_crs or []):
            tx_crs.append(self._wrap_cr_create(cr))
        for d in (delete_crs or []):
            tx_crs.append(
                self._wrap_cr_delete(
                    d["apiVersion"], d["kind"], d["name"],
                    d.get("namespace", "eda"),
                )
            )

        if not tx_crs:
            return TransactionResult(success=True, message="No changes")

        if dry_run:
            description += " (dry-run)"

        logger.info(
            "%s: submitting %d CRs%s",
            phase_label, len(tx_crs),
            " (dry-run)" if dry_run else "",
        )

        tx_id = self.submit_transaction(
            tx_crs, dry_run=dry_run, description=description,
        )

        result = self.poll_transaction(tx_id)

        if result.success:
            logger.info("%s: transaction %s succeeded", phase_label, tx_id)
        else:
            logger.error(
                "%s: transaction %s failed: %s",
                phase_label, tx_id, result.message,
            )

        return result

    # ------------------------------------------------------------------
    # Resource splitting by phase
    # ------------------------------------------------------------------

    @staticmethod
    def _split_by_phase(resources: list[dict]) -> dict[str, list[dict]]:
        """
        Split resources into phase buckets.

        Interface CRs are special-cased: ISL interfaces
        (eda.nokia.com/role=interSwitch) must deploy in the topology
        phase alongside TopoLink/TopoNode because TopoLinks reference
        them via interfaceResource. All other Interface CRs (edge, LAG)
        go into the services phase.
        """
        result: dict[str, list[dict]] = {}

        for cr in resources:
            kind = cr.get("kind", "")
            if kind == "Interface":
                # ISL interfaces must be in the topology phase so that
                # TopoLink CRs can reference them
                role = cr.get("metadata", {}).get("labels", {}).get(
                    "eda.nokia.com/role", ""
                )
                phase = (
                    PHASE_TOPOLOGY if role == "interSwitch"
                    else PHASE_SERVICES
                )
            else:
                phase = _KIND_TO_PHASE.get(kind, PHASE_SERVICES)
            result.setdefault(phase, []).append(cr)

        return result

    # ------------------------------------------------------------------
    # Node sync waiting
    # ------------------------------------------------------------------

    def _wait_for_nodes_sync(
        self,
        topo_nodes: list[dict],
        timeout: int = 600,
        interval: int = 10,
    ) -> None:
        """
        Wait for all TopoNodes to reach ``Synced`` node-state.

        After the topology transaction, EDA's NPP connects to each node,
        pushes initial config, and transitions through several states::

            TryingToConnect → WaitingForInitialCfg → Committing → Synced

        Downstream phases (Fabric, Services) depend on nodes being fully
        synced — submitting them earlier causes intent errors because the
        NPP hasn't confirmed the baseline config yet.

        This method polls ``GET /apps/.../toponodes/{name}`` and checks
        ``status.node-state`` for each node until all report ``Synced``
        or the timeout expires.
        """
        node_names = [
            cr.get("metadata", {}).get("name", "")
            for cr in topo_nodes
        ]
        ns = topo_nodes[0].get("metadata", {}).get("namespace", "eda")

        logger.info(
            "Waiting for %d TopoNodes to reach Synced state...",
            len(node_names),
        )

        def _check(name: str) -> tuple[str, str | None]:
            """Return (name, node_state) — None on error."""
            url = (
                f"{self.url}/apps/{self.registry.TOPO_NODE.api_version}"
                f"/namespaces/{ns}/{self.registry.TOPO_NODE.plural}/{name}"
            )
            try:
                resp = self._request("GET", url)
                if resp.status_code != 200:
                    return name, f"http-{resp.status_code}"
                return name, resp.json().get("status", {}).get("node-state", "") or "unknown"
            except Exception as e:
                return name, f"error: {e}"

        start = time.time()
        pending: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(node_names)))) as pool:
            while time.time() - start < timeout:
                synced: list[str] = []
                pending = []  # (name, current_state)

                for name, state in pool.map(_check, node_names):
                    if state == "Synced":
                        synced.append(name)
                    else:
                        pending.append((name, state or "unknown"))

                if len(synced) == len(node_names):
                    elapsed = int(time.time() - start)
                    logger.info(
                        "All %d TopoNodes are Synced (%ds)",
                        len(node_names),
                        elapsed,
                    )
                    return

                elapsed = int(time.time() - start)
                state_summary = ", ".join(
                    f"{n}={s}" for n, s in pending[:5]
                )
                if len(pending) > 5:
                    state_summary += f" (+{len(pending) - 5} more)"
                logger.info(
                    "Waiting for TopoNodes: %d/%d Synced (%ds/%ds) — %s",
                    len(synced),
                    len(node_names),
                    elapsed,
                    timeout,
                    state_summary,
                )
                time.sleep(interval)

        pending_names = ", ".join(n for n, _ in pending)
        logger.warning(
            "Timed out waiting for TopoNodes after %ds — "
            "still pending: %s. Proceeding anyway.",
            timeout,
            pending_names,
        )

