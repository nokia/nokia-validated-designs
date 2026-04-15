"""
Polling helpers for waiting on network convergence.

Provides ``poll_until`` which repeatedly evaluates a predicate until it
returns True or a timeout is exceeded.  Returns the elapsed time, making
it easy to assert convergence timing in tests.
"""

from __future__ import annotations

import time
from typing import Callable


class ConvergenceTimeout(Exception):
    """Raised when poll_until exceeds its timeout."""

    def __init__(self, description: str, timeout: float, elapsed: float):
        self.description = description
        self.timeout = timeout
        self.elapsed = elapsed
        super().__init__(
            f"Timed out waiting for '{description}' "
            f"after {elapsed:.1f}s (timeout={timeout:.1f}s)"
        )


def poll_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
    description: str = "condition",
    backoff: float = 1.0,
    max_interval: float = 10.0,
) -> float:
    """
    Poll *predicate* until it returns True.

    Args:
        predicate: callable returning True when the condition is met.
            May raise exceptions, which are caught and treated as False.
        timeout: maximum seconds to wait.
        interval: initial polling interval in seconds.
        description: human-readable label for timeout error messages.
        backoff: multiplier applied to interval after each poll (1.0 = constant).
        max_interval: upper bound on the polling interval when using backoff.

    Returns:
        Elapsed seconds until the predicate became True.

    Raises:
        ConvergenceTimeout: if the timeout is exceeded.
    """
    start = time.monotonic()
    current_interval = interval

    while True:
        try:
            if predicate():
                return time.monotonic() - start
        except Exception:
            pass

        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise ConvergenceTimeout(description, timeout, elapsed)

        remaining = timeout - elapsed
        sleep_time = min(current_interval, remaining)
        time.sleep(sleep_time)

        current_interval = min(current_interval * backoff, max_interval)
