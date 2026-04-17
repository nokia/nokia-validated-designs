"""Label selector matching utilities.

Shared by generators that need to match EDA-style label selectors
(e.g. ``eda.nokia.com/tagged-v10=enabled``) against label dicts.
"""

from __future__ import annotations

from automation.core.models import NodeIntent


def labels_match(selectors: list[str], labels: dict[str, str]) -> bool:
    """Check if any selector matches the labels (OR logic across selectors).

    Handles both full EDA-prefixed keys (``eda.nokia.com/foo=bar``) and
    short keys (``foo=bar``) by stripping whitespace and comparing directly.
    """
    for sel in selectors:
        sel = sel.strip()
        if "=" not in sel:
            continue
        key, value = sel.split("=", 1)
        key = key.strip()
        value = value.strip()
        if labels.get(key) == value:
            return True
    return False


def node_matches_selector(node: NodeIntent, selectors: list[str]) -> bool:
    """Check if a node matches a list of label selectors (OR logic).

    A node matches if *any* selector matches (same semantics as EDA).
    """
    if not selectors:
        return False
    return labels_match(selectors, node.labels)
