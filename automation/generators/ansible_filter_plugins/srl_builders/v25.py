"""SR Linux payload builder for all 25.x releases.

Thin re-export of ``v25_3`` — the yearly boundary changes (24.x → 25.x)
apply to every 25.x minor release (25.3, 25.7, 25.10, …).  If a specific
minor introduces its own breaking changes, add a ``v25_7.py`` etc. and
the version dispatcher will prefer it over this module.
"""

from .v25_3 import *  # noqa: F401,F403
