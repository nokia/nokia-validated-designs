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
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3

from automation.generators.eda_generator import MANAGED_BY_LABEL, MANAGED_BY_VALUE

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
        "TopoNode", "TopoLink",
    },
    PHASE_FABRIC: {"Fabric"},
    PHASE_SERVICES: {
        "BridgeDomain", "Router", "IRBInterface",
        "VLAN", "RoutedInterface", "StaticRoute", "Configlet",
    },
}

# CR kind → phase mapping (for destroy)
# Interface is grouped with topology because EDA ISLs (auto-created from
# TopoLinks) reference Interface CRs — you can't delete Interfaces while
# TopoLinks still exist.
DESTROY_PHASE_KINDS: dict[str, set[str]] = {
    PHASE_SERVICES: {
        "BridgeDomain", "Router", "IRBInterface",
        "VLAN", "RoutedInterface", "StaticRoute", "Configlet",
    },
    PHASE_FABRIC: {"Fabric"},
    PHASE_TOPOLOGY: {
        "Interface",  # must be deleted with/after TopoLinks
        "TopoLink", "TopoNode",
        "IPAllocationPool", "IndexAllocationPool",
        "NodeProfile", "NodeUser", "Init",
    },
}

# Resource types in correct destroy order (services first, topo last).
# Interface appears AFTER TopoLink because ISLs reference Interfaces.
DESTROY_ORDER: list[tuple[str, str, str]] = [
    # (apiVersion, kind, plural)
    # --- services ---
    ("config.eda.nokia.com/v1alpha1", "Configlet", "configlets"),
    ("protocols.eda.nokia.com/v1", "StaticRoute", "staticroutes"),
    ("services.eda.nokia.com/v1", "RoutedInterface", "routedinterfaces"),
    ("services.eda.nokia.com/v1", "VLAN", "vlans"),
    ("services.eda.nokia.com/v1", "IRBInterface", "irbinterfaces"),
    ("services.eda.nokia.com/v1", "Router", "routers"),
    ("services.eda.nokia.com/v1", "BridgeDomain", "bridgedomains"),
    # --- fabric ---
    ("fabrics.eda.nokia.com/v1alpha1", "Fabric", "fabrics"),
    # --- topology (Interface after TopoLink!) ---
    ("core.eda.nokia.com/v1", "TopoLink", "topolinks"),
    ("interfaces.eda.nokia.com/v1alpha1", "Interface", "interfaces"),
    ("core.eda.nokia.com/v1", "TopoNode", "toponodes"),
    ("core.eda.nokia.com/v1", "IPAllocationPool", "ipallocationpools"),
    ("core.eda.nokia.com/v1", "IndexAllocationPool", "indexallocationpools"),
    ("core.eda.nokia.com/v1", "NodeProfile", "nodeprofiles"),
    ("core.eda.nokia.com/v1", "NodeUser", "nodeusers"),
    ("bootstrap.eda.nokia.com/v1alpha1", "Init", "inits"),
]


@dataclass
class TransactionPlan:
    """Computed diff of desired vs. current state."""

    creates: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    deletes: list[dict] = field(default_factory=list)

    @property
    def total_ops(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.deletes)

    def summary(self) -> str:
        return (
            f"Transaction plan: "
            f"{len(self.creates)} create, "
            f"{len(self.updates)} update, "
            f"{len(self.deletes)} delete"
        )


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
    ):
        self.url = (url or os.environ.get("EDA_URL", "")).rstrip("/")
        self.username = username or os.environ.get("EDA_USER", "admin")
        self.password = password or os.environ.get("EDA_PASSWORD", "admin")
        self.verify_ssl = verify_ssl
        self._token: str | None = None
        self._session = requests.Session()
        self._session.verify = self.verify_ssl

        if not self.url:
            raise ValueError(
                "EDA URL must be provided via --eda-url or EDA_URL env var"
            )

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> str:
        """
        Authenticate with EDA's Keycloak identity provider.

        Uses a two-step flow:
        1. Get admin token from Keycloak master realm (public admin-cli client)
        2. Use that to discover the eda-api-server client secret
        3. Get a token scoped to the EDA API using the discovered secret

        Only username and password are required.
        """
        keycloak_base = f"{self.url}/core/httpproxy/v1/keycloak"

        # Step 1: Discover eda-api-server client secret
        logger.info("Discovering EDA API client secret via Keycloak")
        client_secret = self._discover_client_secret(keycloak_base)

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

        logger.info("Authentication successful")
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
        """Ensure we have a valid token."""
        if not self._token:
            self.authenticate()

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
            (av, kind, plural)
            for av, kind, plural in DESTROY_ORDER
            if target_kinds is None or kind in target_kinds
        ]

        managed: list[dict] = []
        for api_version, kind, plural in resource_types:
            try:
                url = (
                    f"{self.url}/apps/{api_version}"
                    f"/namespaces/{namespace}/{plural}"
                )
                params = {
                    "labelSelector": f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
                }
                resp = self._session.get(url, params=params)
                if resp.status_code == 200:
                    items = resp.json().get("items", [])
                    for item in items:
                        item["_kind"] = kind
                        item["_apiVersion"] = api_version
                    managed.extend(items)
                else:
                    logger.warning(
                        "Could not query %s: %s", plural, resp.status_code
                    )
            except Exception as e:
                logger.warning("Error querying %s: %s", plural, e)

        logger.info("Found %d managed resources in EDA", len(managed))
        return managed

    # ------------------------------------------------------------------
    # Diff computation (for prune)
    # ------------------------------------------------------------------

    def compute_diff(
        self, desired: list[dict], current: list[dict]
    ) -> TransactionPlan:
        """
        Compute the diff between desired and current state.

        Returns a TransactionPlan with creates, updates, and deletes.
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

        # Creates: in desired but not in current
        for key, cr in desired_keys.items():
            if key not in current_keys:
                plan.creates.append(cr)
            else:
                # Update: exists in both — use replace
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

        resp = self._session.post(tx_url, json=payload)
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

        Args:
            tx_id: Transaction ID from submit_transaction
            timeout: Maximum wait time in seconds
            interval: Polling interval in seconds

        Returns:
            TransactionResult
        """
        self._ensure_auth()

        # Use the summary endpoint which has clear state/success fields
        summary_url = f"{self.url}/core/transaction/v2/result/summary/{tx_id}"
        execution_url = f"{self.url}/core/transaction/v2/result/execution/{tx_id}"

        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = self._session.get(summary_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if not data:
                        logger.debug("Transaction %s: empty response", tx_id)
                        time.sleep(interval)
                        continue
                    state = (data.get("state") or "").lower()

                    if state == "complete":
                        success = data.get("success", False)

                        # Fetch execution details for error info
                        details = data
                        try:
                            exec_resp = self._session.get(execution_url)
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

            time.sleep(interval)

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

        # Optional prune diff
        delete_crs: list[dict] = []
        if prune:
            current = self.get_managed_resources(phases=run_phases)
            plan = self.compute_diff(resources, current)
            logger.info(plan.summary())
            all_desired = plan.creates + plan.updates

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
        else:
            all_desired = resources

        # Split CRs into phases
        phased = self._split_by_phase(all_desired)

        last_result = TransactionResult(success=True, message="No changes to apply")

        # Phase 1: Topology
        if PHASE_TOPOLOGY in run_phases and phased.get(PHASE_TOPOLOGY):
            result = self._submit_phase(
                crs=phased[PHASE_TOPOLOGY],
                description="NVD topology",
                dry_run=dry_run,
                phase_label="Phase 1 (topology)",
            )
            if not result.success:
                return result
            last_result = result

            # Wait for TopoNodes to sync before downstream phases
            if not dry_run:
                topo_nodes = [
                    cr for cr in phased[PHASE_TOPOLOGY]
                    if cr.get("kind") == "TopoNode"
                ]
                if topo_nodes:
                    self._wait_for_nodes_sync(topo_nodes)

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
            managed_by_kind.setdefault(kind, []).append(cr)

        tx_crs: list[dict] = []
        for api_version, kind, plural in DESTROY_ORDER:
            for cr in managed_by_kind.get(kind, []):
                name = cr.get("metadata", {}).get("name", "")
                tx_crs.append(
                    self._wrap_cr_delete(api_version, kind, name, namespace)
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
        delete_crs: list[dict] | None = None,
        description: str,
        dry_run: bool,
        phase_label: str,
    ) -> TransactionResult:
        """Submit a transaction for a set of CRs and poll for result."""
        tx_crs: list[dict] = [self._wrap_cr_replace(cr) for cr in crs]
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
        kind_to_phase: dict[str, str] = {}
        for phase, kinds in PHASE_KINDS.items():
            for kind in kinds:
                kind_to_phase[kind] = phase

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
                phase = kind_to_phase.get(kind, PHASE_SERVICES)
            result.setdefault(phase, []).append(cr)

        return result

    # ------------------------------------------------------------------
    # Node sync waiting
    # ------------------------------------------------------------------

    def _wait_for_nodes_sync(
        self,
        topo_nodes: list[dict],
        timeout: int = 120,
        interval: int = 5,
    ) -> None:
        """
        Wait for TopoNodes to be fully registered and synced in EDA.

        After a topology transaction, EDA needs time to process the
        nodes before downstream resources (Configlets, Interfaces, etc.)
        can reference them. This polls the TopoNode REST API and checks
        for a 'ready' condition.
        """
        node_names = [
            cr.get("metadata", {}).get("name", "")
            for cr in topo_nodes
        ]
        ns = topo_nodes[0].get("metadata", {}).get("namespace", "eda")
        api_version = topo_nodes[0].get("apiVersion", "core.eda.nokia.com/v1")

        logger.info(
            "Waiting for %d TopoNodes to be synced...", len(node_names)
        )

        start = time.time()
        while time.time() - start < timeout:
            all_ready = True
            pending = []
            for name in node_names:
                url = (
                    f"{self.url}/apps/{api_version}"
                    f"/namespaces/{ns}/toponodes/{name}"
                )
                try:
                    resp = self._session.get(url)
                    if resp.status_code != 200:
                        all_ready = False
                        pending.append(name)
                        continue
                    data = resp.json()
                    status = data.get("status", {})
                    conditions = status.get("conditions", [])
                    ready = any(
                        c.get("type") == "Ready"
                        and c.get("status") == "True"
                        for c in conditions
                    )
                    if not ready and conditions:
                        all_ready = False
                        pending.append(name)
                except Exception:
                    all_ready = False
                    pending.append(name)

            if all_ready:
                elapsed = int(time.time() - start)
                logger.info(
                    "All %d TopoNodes are synced (%ds)", len(node_names), elapsed
                )
                return

            elapsed = int(time.time() - start)
            logger.info(
                "Waiting for TopoNodes: %d/%d ready (%ds/%ds) — pending: %s",
                len(node_names) - len(pending),
                len(node_names),
                elapsed,
                timeout,
                ", ".join(pending[:5]),
            )
            time.sleep(interval)

        logger.warning(
            "Timed out waiting for TopoNodes after %ds, proceeding",
            timeout,
        )

