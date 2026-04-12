"""Version-dispatched SR Linux payload builders.

Selects the best-matching builder module for a given SR Linux software version.
Falls back through exact -> minor -> major -> default.
"""

import importlib

_BUILDER_CACHE: dict = {}

KNOWN_VERSIONS: dict = {}


def get_builder(version: str):
    """Return the best-matching payload builder module for an SR Linux version.

    Resolution order:
      1. Exact match   – e.g. ``srl_builders.v24_10_2``
      2. Minor match   – e.g. ``srl_builders.v24_10``
      3. Major match   – e.g. ``srl_builders.v24``
      4. Default        – ``srl_builders.default``
    """
    if version in _BUILDER_CACHE:
        return _BUILDER_CACHE[version]

    parts = version.replace("-", ".").split(".")
    candidates = []
    if len(parts) >= 3:
        candidates.append(f"v{'_'.join(parts[:3])}")
    if len(parts) >= 2:
        candidates.append(f"v{'_'.join(parts[:2])}")
    if len(parts) >= 1:
        candidates.append(f"v{parts[0]}")

    pkg = __name__
    for name in candidates:
        try:
            mod = importlib.import_module(f".{name}", package=pkg)
            _BUILDER_CACHE[version] = mod
            return mod
        except ModuleNotFoundError:
            continue

    mod = importlib.import_module(".default", package=pkg)
    _BUILDER_CACHE[version] = mod
    return mod
