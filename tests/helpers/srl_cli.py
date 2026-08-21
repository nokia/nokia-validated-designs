"""
Structured ``sr_cli`` output via docker exec.

``fcli`` covers the state most tests need, but it exposes only the IPv4 RIB and
no QoS tree.  The rail-optimized backend is IPv6-only and its RoCEv2 behaviour
lives entirely in ``/qos``, so this helper reaches for the SR Linux CLI's
``| as json`` output modifier to get both as parsed data.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class SrlCliError(RuntimeError):
    """Raised when an ``sr_cli`` invocation fails or returns unparsable output."""


def cli_json(container: str, command: str, *, timeout: int = 30) -> Any:
    """Run *command* on *container* and parse its ``| as json`` output."""
    cmd = ["docker", "exec", container, "sr_cli", "-e", f"{command} | as json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as exc:
        raise SrlCliError(f"sr_cli failed on {container}: {exc}") from exc

    if proc.returncode != 0:
        raise SrlCliError(
            f"sr_cli failed on {container} (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    text = proc.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SrlCliError(
            f"Unparsable sr_cli output on {container} for '{command}': "
            f"{text[:300]}"
        ) from exc


@dataclass(frozen=True)
class Route:
    """One row of an SR Linux route table."""

    prefix: str
    route_type: str
    active: bool
    metric: int = 0
    preference: int = 0
    origin_network_instance: str = ""
    next_hops: tuple[str, ...] = ()
    next_hop_interfaces: tuple[str, ...] = ()

    @property
    def ecmp_paths(self) -> int:
        return len(self.next_hops)


def _split_column(value: Any) -> tuple[str, ...]:
    """ECMP next-hops arrive as one newline-joined string per column."""
    if not value:
        return ()
    if isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        parts = str(value).split("\n")
    return tuple(p.strip() for p in parts if p.strip())


def routes(
    container: str, network_instance: str, *, family: str = "ipv6-unicast",
) -> list[Route]:
    """Return the route table of *network_instance* on *container*."""
    data = cli_json(
        container,
        f"show network-instance {network_instance} route-table {family} summary",
    )
    if not isinstance(data, dict):
        return []

    out: list[Route] = []
    for instance in data.get("instance", []):
        for row in instance.get("ip route", []):
            out.append(Route(
                prefix=str(row.get("Prefix", "")),
                route_type=str(row.get("Route Type", "")),
                active=str(row.get("Active", "")).lower() == "true",
                metric=int(row.get("Metric", 0) or 0),
                preference=int(row.get("Pref", 0) or 0),
                origin_network_instance=str(row.get("Origin Network Instance", "")),
                next_hops=_split_column(row.get("Next-hop (Type)")),
                next_hop_interfaces=_split_column(row.get("Next-hop Interface")),
            ))
    return out


@dataclass
class QosInterface:
    """PFC and ECN state bound to one interface in ``/qos interfaces``."""

    interface_id: str
    pfc_enabled: bool = False
    pfc_mapping_profile: str = ""
    queue_management_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class EcnSlope:
    """One WRED slope entry of a queue-management-profile."""

    profile: str
    ecn_enabled: bool = False
    min_threshold_percent: int = 0
    max_threshold_percent: int = 0
    max_drop_probability_percent: int = 0


@dataclass
class QosState:
    """The parts of ``/qos`` that make a RoCEv2 fabric lossless."""

    interfaces: dict[str, QosInterface] = field(default_factory=dict)
    ecn_slopes: dict[str, list[EcnSlope]] = field(default_factory=dict)
    pfc_priorities: dict[str, set[int]] = field(default_factory=dict)
    deadlock_timers: dict[str, tuple[int, int]] = field(default_factory=dict)


def qos_state(container: str) -> QosState:
    """Read and flatten ``/qos`` on *container*."""
    data = cli_json(container, "info /qos")
    state = QosState()
    if not isinstance(data, dict):
        return state

    for entry in (data.get("interfaces") or {}).get("interface", []):
        iface_id = str(entry.get("interface-id", ""))
        pfc = entry.get("pfc") or {}
        qos_iface = QosInterface(
            interface_id=iface_id,
            pfc_enabled=bool(pfc.get("pfc-enable", False)),
            pfc_mapping_profile=str(pfc.get("pfc-mapping-profile", "")),
        )
        queues = ((entry.get("output") or {}).get("queues") or {}).get("queue", [])
        for queue in queues:
            qos_iface.queue_management_profiles[str(queue.get("queue-name", ""))] = str(
                queue.get("queue-management-profile", "")
            )
        state.interfaces[iface_id] = qos_iface

    buffer_mgmt = data.get("buffer-management") or {}
    for profile in buffer_mgmt.get("queue-management-profile", []):
        name = str(profile.get("name", ""))
        slopes = []
        for slope in ((profile.get("wred") or {}).get("wred-slope") or []):
            slopes.append(EcnSlope(
                profile=name,
                ecn_enabled=bool(slope.get("enable-ecn", False)),
                min_threshold_percent=int(slope.get("min-threshold-percent", 0) or 0),
                max_threshold_percent=int(slope.get("max-threshold-percent", 0) or 0),
                max_drop_probability_percent=int(
                    slope.get("max-drop-probability-percent", 0) or 0
                ),
            ))
        state.ecn_slopes[name] = slopes

    for profile in data.get("pfc-mapping-profile", []):
        name = str(profile.get("name", ""))
        state.pfc_priorities[name] = {
            int(p["index"])
            for p in profile.get("pfc-priority", [])
            if p.get("pfc-enable", False) and "index" in p
        }

        deadlock = (
            (profile.get("received-pfc-pause-frames") or {}).get("deadlock") or {}
        )
        if deadlock.get("enable"):
            state.deadlock_timers[name] = (
                int(deadlock.get("detection-timer", 0) or 0),
                int(deadlock.get("recovery-timer", 0) or 0),
            )

    return state
