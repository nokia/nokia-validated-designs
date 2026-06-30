"""Unit tests for EdaClient.get_eda_version() version detection."""

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
