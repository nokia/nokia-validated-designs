"""
fcli subprocess wrapper for control-plane state queries.

Wraps the ``fcli`` CLI tool to query SR Linux node state via gNMI,
parsing JSON output into Python dicts for use in test assertions.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class FcliError(Exception):
    """Raised when an fcli command fails."""


def _parse_json(raw: str) -> Any | None:
    """Try to extract a JSON array or object from *raw*.

    fcli sometimes emits non-JSON preamble (warnings, ANSI codes,
    progress text) before the actual JSON payload.  We first try the
    full string, then scan for the first ``[`` or ``{`` and parse from
    there.  Returns ``None`` if no valid JSON can be found.
    """
    text = raw.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for i, ch in enumerate(text):
        if ch in ("[", "{"):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def fcli_query(
    command: str,
    *,
    topo_path: str = "",
    gnmi_port: int | None = None,
    inventory_filter: str = "",
    field_filter: str = "",
    extra_args: list[str] | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """
    Run an fcli command and return parsed JSON output.

    Args:
        command: fcli command (e.g. "bgp-peers", "mac", "vxlan")
        topo_path: path to .clab.yaml topology file
        gnmi_port: override gNMI port (fcli -p); None keeps the fcli default
        inventory_filter: node filter (e.g. "name=leaf1", "role=spine")
        field_filter: row filter (e.g. "State=established")
        extra_args: additional CLI arguments
        timeout: command timeout in seconds

    Returns:
        List of result dicts parsed from fcli JSON output.
    """
    cmd = ["fcli"]
    if topo_path:
        cmd.extend(["-t", topo_path])
    if gnmi_port is not None:
        cmd.extend(["-p", str(gnmi_port)])
    if inventory_filter:
        cmd.extend(["-i", inventory_filter])
    cmd.extend(["-o", "json"])
    cmd.append(command)
    if field_filter:
        cmd.extend(["-f", field_filter])
    if extra_args:
        cmd.extend(extra_args)

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise FcliError(f"fcli timed out after {timeout}s: {' '.join(cmd)}") from e

    if result.returncode != 0:
        raise FcliError(
            f"fcli failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    if not result.stdout.strip():
        return []

    raw = result.stdout
    data = _parse_json(raw)
    if data is None:
        preview = raw[:500] + ("…" if len(raw) > 500 else "")
        raise FcliError(
            f"Failed to parse fcli JSON output for '{' '.join(cmd)}'.\n"
            f"stdout: {preview}\nstderr: {result.stderr.strip()}"
        )

    if isinstance(data, list):
        return data
    return [data]


@dataclass
class FcliClient:
    """
    Stateful fcli client bound to a specific containerlab topology.

    Usage::

        fcli = FcliClient(topo_path="/path/to/dc1.clab.yml")
        peers = fcli.bgp_peers()
        macs = fcli.mac_table(network_instance="macvrf-v10")
    """

    topo_path: str
    gnmi_port: int | None = None

    def _query(self, command: str, **kwargs) -> list[dict[str, Any]]:
        return fcli_query(
            command, topo_path=self.topo_path, gnmi_port=self.gnmi_port, **kwargs
        )

    def bgp_peers(
        self, *, node: str = "", state: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        filt = f"state={state}" if state else ""
        return self._query("bgp-peers", inventory_filter=inv, field_filter=filt)

    def bgp_rib(
        self, *, route_family: str = "evpn", route_type: str = "",
        node: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        args = ["-r", route_family]
        if route_type:
            args.extend(["-t", route_type])
        return self._query("bgp-rib", inventory_filter=inv, extra_args=args)

    def mac_table(
        self, *, node: str = "", network_instance: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        filt = f"Network-Instance={network_instance}" if network_instance else ""
        return self._query("mac", inventory_filter=inv, field_filter=filt)

    def vxlan_tunnels(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("vxlan", inventory_filter=inv)

    def ethernet_segments(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("es", inventory_filter=inv)

    def arp_table(
        self, *, node: str = "", network_instance: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        filt = f"Network-Instance={network_instance}" if network_instance else ""
        return self._query("arp", inventory_filter=inv, field_filter=filt)

    def network_instances(
        self, *, node: str = "", ni_type: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        filt = f"type={ni_type}" if ni_type else ""
        return self._query("ni", inventory_filter=inv, field_filter=filt)

    def subinterfaces(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("subif", inventory_filter=inv)

    def lldp(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("lldp", inventory_filter=inv)

    def lag(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("lag", inventory_filter=inv)

    def ipv4_rib(
        self, *, node: str = "", prefix: str = "",
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        args = ["-a", prefix] if prefix else []
        return self._query("ipv4-rib", inventory_filter=inv, extra_args=args)

    def ifstats(
        self, *, node: str = "", interval: int = 5,
    ) -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query(
            "ifstats", inventory_filter=inv,
            extra_args=["-s", str(interval)],
            timeout=interval + 20,
        )

    def sys_info(self, *, node: str = "") -> list[dict[str, Any]]:
        inv = f"name={node}" if node else ""
        return self._query("sys-info", inventory_filter=inv)
