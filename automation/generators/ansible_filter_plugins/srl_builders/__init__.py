"""Version-dispatched SR Linux payload builders.

Selects the best-matching builder module for a given SR Linux software version.
Falls back through exact -> minor -> major -> default.
"""

import importlib
import logging

logger = logging.getLogger(__name__)

_BUILDER_CACHE: dict = {}


def get_builder(version: str):
    """Return the best-matching payload builder module for an SR Linux version.

    Resolution order:
      1. Exact match   – e.g. ``srl_builders.v24_10_2``
      2. Minor match   – e.g. ``srl_builders.v24_10``
      3. Major match   – e.g. ``srl_builders.v24``
      4. Default        – ``srl_builders.default``

    The resolved module is logged (at INFO the first time a version is seen,
    DEBUG on cache hits) so it's clear which builder a device's version mapped
    to — including the fall-through to ``default`` (the 24.10 base).
    """
    if version in _BUILDER_CACHE:
        mod = _BUILDER_CACHE[version]
        logger.debug("SRL builder for %s resolved to %s (cached)", version, mod.__name__)
        return mod

    normalized = version.lstrip("v")
    parts = normalized.replace("-", ".").split(".")
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
            logger.info("SRL builder for %s resolved to %s", version, mod.__name__)
            return mod
        except ModuleNotFoundError:
            continue

    mod = importlib.import_module(".default", package=pkg)
    _BUILDER_CACHE[version] = mod
    logger.info(
        "SRL builder for %s fell through to %s (24.10 base)", version, mod.__name__
    )
    return mod
