"""Unit tests for EdaClient version detection, CR scoping and NodeProfile enrichment."""

import copy

import pytest

from automation.executors.eda import EdaClient


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


@pytest.fixture()
def client() -> EdaClient:
    # url satisfies the constructor; no network calls are made because
    # _request is stubbed per-test.
    return EdaClient(url="https://eda.example:9443", username="admin", password="admin")


def _stub(client: EdaClient, resp: _FakeResp) -> None:
    client._request = lambda method, url, **kw: resp  # type: ignore[assignment]


class TestGetEdaVersion:
    def test_parses_build_string(self, client):
        _stub(client, _FakeResp(200, {
            "eda": {"version": "v26.4.2-2605212019-g73187ba6"},
            "eda-api": {"version": "v26.4.2-2605212019-g73187ba6"},
        }))
        assert client.get_eda_version() == "26.4.2"

    def test_parses_plain_semver(self, client):
        _stub(client, _FakeResp(200, {"eda": {"version": "25.12.4"}}))
        assert client.get_eda_version() == "25.12.4"

    def test_major_minor_only(self, client):
        _stub(client, _FakeResp(200, {"eda": {"version": "v26.4"}}))
        assert client.get_eda_version() == "26.4"

    def test_non_200_returns_none(self, client):
        _stub(client, _FakeResp(404))
        assert client.get_eda_version() is None

    def test_unparseable_returns_none(self, client):
        _stub(client, _FakeResp(200, {"eda": {"version": "unknown"}}))
        assert client.get_eda_version() is None

    def test_missing_eda_key_returns_none(self, client):
        _stub(client, _FakeResp(200, {"eda-api": {"version": "v26.4.2"}}))
        assert client.get_eda_version() is None


class TestClusterScopedKinds:
    """EDA rejects ``/namespaces/{ns}/`` for Namespace and TopologyGrouping, so
    they have to be read from a cluster-scoped endpoint — exactly once, not once
    per namespace.
    """

    @staticmethod
    def _record_urls(client: EdaClient) -> list[str]:
        urls: list[str] = []

        def fake(method, url, **kw):
            urls.append(url)
            return _FakeResp(200, {"items": []})

        client._request = fake  # type: ignore[assignment]
        client._ensure_auth = lambda: None  # type: ignore[assignment]
        return urls

    def test_registry_marks_only_the_non_namespaced_kinds(self):
        from automation.eda_models.profiles import get_registry

        reg = get_registry("25.12")
        scoped = {t.kind for t in reg.ALL_TYPES if t.cluster_scoped}
        assert scoped == {"Namespace", "TopologyGrouping"}

    def test_cluster_scoped_reads_omit_the_namespace_path_segment(self, client):
        urls = self._record_urls(client)
        client.get_managed_resources(namespace=["dc1-backend", "eda-system"])
        ns_urls = [u for u in urls if u.endswith("/namespaces")]
        assert ns_urls == ["https://eda.example:9443/apps/core.eda.nokia.com/v1/namespaces"]

    def test_cluster_scoped_kinds_are_read_once_across_namespaces(self, client):
        urls = self._record_urls(client)
        client.get_managed_resources(
            namespace=["dc1-backend", "dc1-frontend", "eda-system"]
        )
        assert sum(u.endswith("/topologygroupings") for u in urls) == 1
        # A namespaced kind is still read once per namespace.
        assert sum(u.endswith("/toponodes") for u in urls) == 3

    def test_namespaced_kinds_keep_the_namespace_path_segment(self, client):
        urls = self._record_urls(client)
        client.get_managed_resources(namespace="dc1-frontend")
        assert any(u.endswith("/namespaces/dc1-frontend/toponodes") for u in urls)

    def test_a_converged_cluster_scoped_resource_is_not_reported_as_a_create(
        self, client
    ):
        cr = {
            "apiVersion": "core.eda.nokia.com/v1",
            "kind": "Namespace",
            "metadata": {
                "name": "dc1-backend",
                "namespace": "eda-system",
                "labels": {"eda.nokia.com/managed-by": "nvd-automation"},
            },
            "spec": {},
        }
        live = {**copy.deepcopy(cr), "_kind": "Namespace",
                "_apiVersion": "core.eda.nokia.com/v1"}
        plan = client.compute_diff([cr], [live])
        assert plan.total_ops == 0


class TestEnrichNodeProfiles:
    """EDA installs its reference NodeProfiles in a single namespace, so a
    profile emitted into a design-owned namespace still has to resolve its
    version-specific fields (``yang`` above all) from there.
    """

    REF = {
        "yang": "https://asvr/schemaprofiles/srlinux-ghcr-25.10.1/x.zip",
        "versionMatch": r"v25\.10\.1.*",
        "versionPath": ".system.information.version",
        "llmDb": "https://asvr/llm-dbs/llm-db-srlinux-ghcr-25.10.1/x.tar.gz",
    }

    @staticmethod
    def _profile(namespace: str) -> dict:
        return {
            "apiVersion": "core.eda.nokia.com/v1",
            "kind": "NodeProfile",
            "metadata": {"name": "clab-srlinux-25.10.1", "namespace": namespace},
            "spec": {"version": "25.10.1", "yang": "guessed", "port": 57410},
        }

    def _serve_only_in(self, client: EdaClient, namespace: str) -> list[str]:
        """Serve the reference profile from *namespace* alone, recording lookups."""
        tried: list[str] = []

        def fake(method, url, **kw):
            ns = url.split("/namespaces/")[1].split("/")[0]
            tried.append(ns)
            return _FakeResp(200, {"spec": self.REF}) if ns == namespace else _FakeResp(404)

        client._request = fake  # type: ignore[assignment]
        client._ensure_auth = lambda: None  # type: ignore[assignment]
        return tried

    def test_falls_back_to_the_reference_namespace(self, client):
        tried = self._serve_only_in(client, "eda")
        out = client._enrich_node_profiles([self._profile("dc1-frontend")])
        assert out[0]["spec"]["yang"] == self.REF["yang"]
        assert tried == ["dc1-frontend", "eda"]

    def test_clab_specific_fields_survive_enrichment(self, client):
        self._serve_only_in(client, "eda")
        out = client._enrich_node_profiles([self._profile("dc1-backend")])
        assert out[0]["spec"]["port"] == 57410

    def test_no_fallback_lookup_when_the_cr_namespace_answers(self, client):
        tried = self._serve_only_in(client, "eda")
        out = client._enrich_node_profiles([self._profile("eda")])
        assert out[0]["spec"]["yang"] == self.REF["yang"]
        assert tried == ["eda"]

    def test_unresolvable_profile_keeps_the_generated_guess(self, client):
        self._serve_only_in(client, "nowhere")
        out = client._enrich_node_profiles([self._profile("dc1-frontend")])
        assert out[0]["spec"]["yang"] == "guessed"
