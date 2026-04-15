"""
Fault injection and recovery for containerlab topologies.

Provides a FaultManager that can disable/enable links and pause/unpause
containers to simulate ISL, edge, leaf, and spine failures. All injected
faults are tracked and automatically reverted on cleanup, even when a test
fails.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class FaultType(Enum):
    LINK_DOWN = "link_down"
    NODE_PAUSE = "node_pause"


@dataclass
class _FaultRecord:
    fault_type: FaultType
    container: str
    interface: str = ""
    injected_at: float = 0.0


def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class FaultManager:
    """
    Inject and recover network faults with guaranteed cleanup.

    Usage as context manager::

        with FaultManager() as fm:
            fm.disable_link("clab-dc1-leaf1", "ethernet-1-32")
            # ... run assertions ...
        # link is automatically re-enabled on exit

    Usage as pytest fixture (see conftest.py)::

        def test_isl_failure(fault_manager):
            fault_manager.disable_link("clab-dc1-leaf1", "ethernet-1-32")
            # ... assertions ...
            # cleanup happens in fixture teardown
    """

    def __init__(self) -> None:
        self._faults: list[_FaultRecord] = []

    def __enter__(self) -> FaultManager:
        return self

    def __exit__(self, *args: Any) -> None:
        self.recover_all()

    # ------------------------------------------------------------------
    # Link faults
    # ------------------------------------------------------------------

    def disable_link(self, container: str, interface: str) -> float:
        """
        Disable a link inside an SR Linux container.

        Uses ``ip link set <intf> down`` inside the container's network
        namespace. Works for ISL and edge links.

        Returns the timestamp (monotonic) of injection.
        """
        srl_intf = _to_linux_intf(interface)
        result = _run(["docker", "exec", container, "ip", "link", "set", srl_intf, "down"])
        ts = time.monotonic()

        if result.returncode != 0:
            logger.warning(
                "Failed to disable %s on %s: %s", interface, container, result.stderr,
            )

        self._faults.append(_FaultRecord(
            fault_type=FaultType.LINK_DOWN,
            container=container,
            interface=interface,
            injected_at=ts,
        ))
        logger.info("FAULT: disabled link %s on %s", interface, container)
        return ts

    def enable_link(self, container: str, interface: str) -> None:
        """Re-enable a previously disabled link."""
        srl_intf = _to_linux_intf(interface)
        result = _run(["docker", "exec", container, "ip", "link", "set", srl_intf, "up"])

        if result.returncode != 0:
            logger.warning(
                "Failed to enable %s on %s: %s", interface, container, result.stderr,
            )

        self._faults = [
            f for f in self._faults
            if not (f.fault_type == FaultType.LINK_DOWN
                    and f.container == container
                    and f.interface == interface)
        ]
        logger.info("RECOVERED: enabled link %s on %s", interface, container)

    # ------------------------------------------------------------------
    # Node faults
    # ------------------------------------------------------------------

    def pause_node(self, container: str) -> float:
        """
        Pause a container to simulate a node crash.

        ``docker pause`` freezes all processes; BFD and BGP timers will
        expire naturally on peers.

        Returns the timestamp (monotonic) of injection.
        """
        result = _run(["docker", "pause", container])
        ts = time.monotonic()

        if result.returncode != 0:
            logger.warning("Failed to pause %s: %s", container, result.stderr)

        self._faults.append(_FaultRecord(
            fault_type=FaultType.NODE_PAUSE,
            container=container,
            injected_at=ts,
        ))
        logger.info("FAULT: paused node %s", container)
        return ts

    def unpause_node(self, container: str) -> None:
        """Unpause a previously paused container."""
        result = _run(["docker", "unpause", container])

        if result.returncode != 0:
            logger.warning("Failed to unpause %s: %s", container, result.stderr)

        self._faults = [
            f for f in self._faults
            if not (f.fault_type == FaultType.NODE_PAUSE and f.container == container)
        ]
        logger.info("RECOVERED: unpaused node %s", container)

    # ------------------------------------------------------------------
    # Compound faults
    # ------------------------------------------------------------------

    def isolate_node(self, container: str, interfaces: list[str]) -> float:
        """
        Disable all listed interfaces on a node, simulating full isolation.

        Returns the timestamp of the first link-down.
        """
        ts = 0.0
        for intf in interfaces:
            t = self.disable_link(container, intf)
            if ts == 0.0:
                ts = t
        return ts

    def restore_node_links(self, container: str, interfaces: list[str]) -> None:
        """Re-enable all listed interfaces on a node."""
        for intf in interfaces:
            self.enable_link(container, intf)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def recover_all(self) -> None:
        """
        Revert all outstanding faults in reverse order.

        Called automatically on context-manager exit or fixture teardown.
        """
        for fault in reversed(list(self._faults)):
            try:
                if fault.fault_type == FaultType.LINK_DOWN:
                    self.enable_link(fault.container, fault.interface)
                elif fault.fault_type == FaultType.NODE_PAUSE:
                    self.unpause_node(fault.container)
            except Exception:
                logger.exception(
                    "Failed to recover fault %s on %s",
                    fault.fault_type.value, fault.container,
                )

    @property
    def active_faults(self) -> list[dict]:
        """Return a list of currently active faults for debugging."""
        return [
            {
                "type": f.fault_type.value,
                "container": f.container,
                "interface": f.interface,
                "injected_at": f.injected_at,
            }
            for f in self._faults
        ]


def _to_linux_intf(srl_interface: str) -> str:
    """
    Convert SR Linux interface name to the Linux netdev name used inside
    the container.

    SR Linux containers use names like ``e1-32`` for ``ethernet-1-32``
    and ``e1-3-1`` for breakout channel ``ethernet-1-3/1``.
    """
    return srl_interface.replace("ethernet-", "e").replace("/", "-")
