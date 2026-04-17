"""Unit tests for automation.core.selectors."""

from automation.core.models import NodeIntent
from automation.core.selectors import labels_match, node_matches_selector


class TestLabelsMatch:
    def test_exact_match(self):
        assert labels_match(
            ["eda.nokia.com/role=leaf"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_no_match(self):
        assert not labels_match(
            ["eda.nokia.com/role=spine"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_or_logic_first_matches(self):
        assert labels_match(
            ["eda.nokia.com/role=leaf", "eda.nokia.com/role=spine"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_or_logic_second_matches(self):
        assert labels_match(
            ["eda.nokia.com/role=spine", "eda.nokia.com/role=leaf"],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_empty_selectors(self):
        assert not labels_match([], {"eda.nokia.com/role": "leaf"})

    def test_empty_labels(self):
        assert not labels_match(["eda.nokia.com/role=leaf"], {})

    def test_whitespace_handling(self):
        assert labels_match(
            ["  eda.nokia.com/role = leaf  "],
            {"eda.nokia.com/role": "leaf"},
        )

    def test_selector_without_equals(self):
        assert not labels_match(["no-equals-sign"], {"key": "val"})

    def test_multiple_labels(self):
        labels = {
            "eda.nokia.com/role": "edge",
            "eda.nokia.com/tagged-v10": "enabled",
        }
        assert labels_match(["eda.nokia.com/tagged-v10=enabled"], labels)
        assert not labels_match(["eda.nokia.com/tagged-v20=enabled"], labels)


class TestNodeMatchesSelector:
    def _node(self, **labels) -> NodeIntent:
        return NodeIntent(
            name="leaf1",
            role="leaf",
            platform="7220 IXR-D3L",
            version="24.10.2",
            system0_ipv4="10.0.0.1/32",
            asn=65001,
            mgmt_ipv4="172.21.21.11",
            labels=labels,
        )

    def test_match(self):
        node = self._node(**{"eda.nokia.com/role": "leaf"})
        assert node_matches_selector(node, ["eda.nokia.com/role=leaf"])

    def test_no_match(self):
        node = self._node(**{"eda.nokia.com/role": "leaf"})
        assert not node_matches_selector(node, ["eda.nokia.com/role=spine"])

    def test_empty_selectors_returns_false(self):
        node = self._node(**{"eda.nokia.com/role": "leaf"})
        assert not node_matches_selector(node, [])
