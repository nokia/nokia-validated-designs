"""SR Linux payload builder for the 24.10.x release train.

Explicit alias of ``default`` — which *is* the 24.10 base builder. This module
exists so the supported floor is named (``v24_10``) rather than implied by
"default", and so ``get_builder("24.10.x")`` reports a 24.10-specific module in
its resolution log instead of the generic fall-through to ``default``.
"""

from .default import *  # noqa: F401,F403
