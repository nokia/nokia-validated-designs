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

    def get_managed_resources(self, namespace: str = "eda") -> list[dict]:
        """
        Query EDA for all resources carrying the managed-by label.

        This is used by the prune logic to find resources that
        were previously created by the automation but are no longer
        in the desired state.
        """
        self._ensure_auth()

        # Query each resource type that we manage
        resource_types = [
            ("bootstrap.eda.nokia.com/v1alpha1", "Init", "inits"),
            ("core.eda.nokia.com/v1", "NodeProfile", "nodeprofiles"),
            ("core.eda.nokia.com/v1", "NodeUser", "nodeusers"),
            ("core.eda.nokia.com/v1", "IndexAllocationPool", "indexallocationpools"),
            ("core.eda.nokia.com/v1", "IPAllocationPool", "ipallocationpools"),
            ("core.eda.nokia.com/v1", "TopoNode", "toponodes"),
            ("core.eda.nokia.com/v1", "TopoLink", "topolinks"),
            ("fabrics.eda.nokia.com/v1alpha1", "Fabric", "fabrics"),
            ("interfaces.eda.nokia.com/v1alpha1", "Interface", "interfaces"),
            ("services.eda.nokia.com/v1", "BridgeDomain", "bridgedomains"),
            ("services.eda.nokia.com/v1", "Router", "routers"),
            ("services.eda.nokia.com/v1", "IRBInterface", "irbinterfaces"),
            ("services.eda.nokia.com/v1", "VLAN", "vlans"),
            ("services.eda.nokia.com/v1", "RoutedInterface", "routedinterfaces"),
            ("protocols.eda.nokia.com/v1", "StaticRoute", "staticroutes"),
            ("config.eda.nokia.com/v1alpha1", "Configlet", "configlets"),
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
    # High-level apply
    # ------------------------------------------------------------------

    def apply(
        self,
        resources: list[dict],
        prune: bool = False,
        dry_run: bool = False,
        auto_confirm: bool = False,
    ) -> TransactionResult:
        """
        Full deployment workflow: authenticate → wrap CRs → [diff if prune] → submit → poll.

        Args:
            resources: List of desired-state CRs (k8s-style dicts)
            prune: If True, delete resources not in desired state
            dry_run: If True, validate only
            auto_confirm: If True, skip prune confirmation

        Returns:
            TransactionResult
        """
        self.authenticate()

        if prune:
            # Get current managed resources
            current = self.get_managed_resources()
            plan = self.compute_diff(resources, current)
            logger.info(plan.summary())

            # Combine creates + updates (all use replace for idempotency)
            all_desired = plan.creates + plan.updates

            # Split into REST-API-only and transaction-safe
            rest_crs, tx_resources = self._split_resources(all_desired)

            # Apply REST-API resources first (TopoNode, TopoLink)
            if rest_crs:
                logger.info(
                    "Applying %d resources via EDA REST API", len(rest_crs)
                )
                rest_errors = self._rest_apply(rest_crs, dry_run=dry_run)
                if rest_errors:
                    logger.warning(
                        "%d REST API errors occurred", len(rest_errors)
                    )

                # Wait for TopoNodes to be registered
                if not dry_run:
                    topo_nodes = [
                        cr for cr in rest_crs
                        if cr.get("kind") == "TopoNode"
                    ]
                    if topo_nodes:
                        self._wait_for_nodes(topo_nodes)

            # Build transaction CRs using replace (idempotent)
            tx_crs: list[dict] = []
            for cr in tx_resources:
                tx_crs.append(self._wrap_cr_replace(cr))

            # Deletes
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

            for d in plan.deletes:
                tx_crs.append(
                    self._wrap_cr_delete(
                        d["apiVersion"], d["kind"], d["name"], d.get("namespace", "eda")
                    )
                )

        else:
            # Simple create/replace for all resources
            # Split into REST-API-only and transaction-safe
            rest_crs, tx_resources = self._split_resources(resources)

            # Apply REST-API resources first (TopoNode, TopoLink)
            # Uses PUT (update) with POST fallback (create) — idempotent
            if rest_crs:
                logger.info(
                    "Applying %d resources via EDA REST API", len(rest_crs)
                )
                rest_errors = self._rest_apply(rest_crs, dry_run=dry_run)
                if rest_errors:
                    logger.warning(
                        "%d REST API errors occurred", len(rest_errors)
                    )

                # Wait for TopoNodes to be registered in EDA before
                # submitting the transaction (Configlets reference nodes)
                if not dry_run:
                    topo_nodes = [
                        cr for cr in rest_crs
                        if cr.get("kind") == "TopoNode"
                    ]
                    if topo_nodes:
                        self._wait_for_nodes(topo_nodes)

            # Use 'replace' for transaction CRs — EDA's replace is idempotent
            # (creates if missing, updates if existing)
            logger.info("Wrapping %d resources for transaction", len(tx_resources))
            tx_crs = [self._wrap_cr_replace(cr) for cr in tx_resources]

        if not tx_crs:
            return TransactionResult(
                success=True,
                message="No changes to apply",
            )

        description = "NVD automation"
        if dry_run:
            description += " (dry-run)"

        tx_id = self.submit_transaction(
            tx_crs, dry_run=dry_run, description=description
        )

        return self.poll_transaction(tx_id)

    # ------------------------------------------------------------------
    # Resource splitting
    # ------------------------------------------------------------------

    # Mapping of Kind → REST API plural name for non-transaction resources
    REST_API_KINDS: dict[str, str] = {
        "TopoNode": "toponodes",
        "TopoLink": "topolinks",
    }

    @classmethod
    def _split_resources(cls, resources: list[dict]) -> tuple[list[dict], list[dict]]:
        """Split resources into REST-API and transaction groups."""
        rest_crs: list[dict] = []
        tx_crs: list[dict] = []
        for cr in resources:
            if cr.get("kind", "") in cls.REST_API_KINDS:
                rest_crs.append(cr)
            else:
                tx_crs.append(cr)
        return rest_crs, tx_crs

    def _rest_apply(
        self, resources: list[dict], dry_run: bool = False
    ) -> list[str]:
        """
        Apply resources via the EDA REST API (PUT).

        Uses PUT /apps/{apiVersion}/namespaces/{ns}/{plural}/{name}
        which creates or replaces the resource idempotently.

        Args:
            resources: List of k8s-style CR dicts
            dry_run: If True, pass dryRun=true query param

        Returns:
            List of error messages (empty on success)
        """
        self._ensure_auth()
        errors: list[str] = []

        for cr in resources:
            kind = cr.get("kind", "Unknown")
            name = cr.get("metadata", {}).get("name", "unknown")
            ns = cr.get("metadata", {}).get("namespace", "eda")
            api_version = cr.get("apiVersion", "")

            plural = self.REST_API_KINDS.get(kind)
            if not plural:
                errors.append(f"Unknown REST API kind: {kind}")
                continue

            base_url = f"{self.url}/apps/{api_version}/namespaces/{ns}/{plural}"
            item_url = f"{base_url}/{name}"

            params = {}
            if dry_run:
                params["dryRun"] = "true"

            try:
                # Try PUT first (update existing)
                resp = self._session.put(item_url, json=cr, params=params)

                if resp.status_code == 404:
                    # Resource doesn't exist — create via POST
                    resp = self._session.post(base_url, json=cr, params=params)

                if resp.status_code in (200, 201):
                    action = "created" if resp.status_code == 201 else "configured"
                    dry_label = " (dry run)" if dry_run else ""
                    logger.info(
                        "REST %s/%s: %s%s",
                        kind, name, action, dry_label,
                    )
                else:
                    err_body = resp.text[:200]
                    logger.error(
                        "REST %s/%s failed (%d): %s",
                        kind, name, resp.status_code, err_body,
                    )
                    errors.append(f"{kind}/{name}: {resp.status_code} {err_body}")
            except Exception as e:
                logger.error("REST %s/%s error: %s", kind, name, e)
                errors.append(f"{kind}/{name}: {e}")

        return errors

    def _wait_for_nodes(
        self, topo_nodes: list[dict], timeout: int = 120, interval: int = 5
    ) -> None:
        """
        Wait for TopoNodes to be registered in EDA.

        After creating TopoNodes via REST API, the EDA system needs time
        to process them. Subsequent transaction CRs (Configlets, etc.)
        will fail with 'Node not found' if submitted too early.

        Args:
            topo_nodes: List of TopoNode CR dicts that were just created
            timeout: Maximum wait time in seconds
            interval: Polling interval in seconds
        """
        node_names = [
            cr.get("metadata", {}).get("name", "")
            for cr in topo_nodes
        ]
        ns = topo_nodes[0].get("metadata", {}).get("namespace", "eda")
        api_version = topo_nodes[0].get("apiVersion", "core.eda.nokia.com/v1")

        logger.info(
            "Waiting for %d TopoNodes to be registered...", len(node_names)
        )

        start = time.time()
        while time.time() - start < timeout:
            all_ready = True
            for name in node_names:
                url = (
                    f"{self.url}/apps/{api_version}"
                    f"/namespaces/{ns}/toponodes/{name}"
                )
                try:
                    resp = self._session.get(url)
                    if resp.status_code != 200:
                        all_ready = False
                        break
                except Exception:
                    all_ready = False
                    break

            if all_ready:
                logger.info("All %d TopoNodes are registered", len(node_names))
                return

            elapsed = int(time.time() - start)
            logger.debug(
                "Waiting for TopoNodes... (%ds/%ds)", elapsed, timeout
            )
            time.sleep(interval)

        logger.warning(
            "Timed out waiting for TopoNodes after %ds, proceeding anyway",
            timeout,
        )
