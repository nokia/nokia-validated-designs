"""
iperf3 helpers for generating traffic flows between client containers.

Provides server lifecycle management and client flow generation via
docker exec.  Designed for load-balancing verification where only the
presence and distribution of traffic matters, not absolute throughput.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

IPERF_PORT_BASE = 9200


@dataclass
class IperfResult:
    """Summary of an iperf3 client run."""

    sent_bytes: int = 0
    sent_bps: float = 0.0
    retransmits: int = 0
    duration: float = 0.0
    success: bool = False
    error: str = ""


def _docker_exec(
    container: str, cmd: list[str], timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )


def _docker_exec_detached(container: str, cmd: list[str]) -> None:
    subprocess.run(
        ["docker", "exec", "-d", container] + cmd,
        capture_output=True, text=True, timeout=10,
    )


def start_server(container: str, bind_ip: str = "", port: int = IPERF_PORT_BASE) -> None:
    """Start an iperf3 server in the background inside a container."""
    kill_server(container, port)
    time.sleep(0.3)
    cmd = ["iperf3", "-s", "-p", str(port), "-D"]
    if bind_ip:
        cmd.extend(["-B", bind_ip])
    _docker_exec(container, cmd, timeout=5)
    time.sleep(0.5)
    logger.debug("Started iperf3 server on %s:%d", container, port)


def kill_server(container: str, port: int = IPERF_PORT_BASE) -> None:
    """Kill iperf3 server(s) in a container."""
    try:
        _docker_exec(container, ["pkill", "-f", f"iperf3.*-p {port}"], timeout=5)
    except Exception:
        pass


def run_client(
    container: str,
    server_ip: str,
    *,
    bind_ip: str = "",
    port: int = IPERF_PORT_BASE,
    duration: int = 5,
    parallel: int = 1,
    udp: bool = False,
    bandwidth: str = "10M",
    mss: int = 0,
) -> IperfResult:
    """
    Run an iperf3 client and return results.

    Args:
        container: Docker container to run client in.
        server_ip: IP address of the iperf3 server.
        bind_ip: Source IP to bind the client to.
        port: Server port.
        duration: Test duration in seconds.
        parallel: Number of parallel streams.
        udp: Use UDP instead of TCP.
        bandwidth: Target bandwidth (only for UDP).
        mss: TCP MSS to set (0 = default).
    """
    cmd = [
        "iperf3", "-c", server_ip, "-p", str(port),
        "-t", str(duration), "-P", str(parallel), "--json",
    ]
    if bind_ip:
        cmd.extend(["-B", bind_ip])
    if udp:
        cmd.extend(["-u", "-b", bandwidth])
    if mss > 0:
        cmd.extend(["-M", str(mss)])

    try:
        result = _docker_exec(container, cmd, timeout=duration + 15)
    except subprocess.TimeoutExpired:
        return IperfResult(error="iperf3 client timed out")
    except Exception as e:
        return IperfResult(error=str(e))

    if result.returncode != 0:
        return IperfResult(
            error=result.stderr.strip() or result.stdout.strip() or f"rc={result.returncode}",
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return IperfResult(error="Failed to parse iperf3 JSON output")

    if "error" in data:
        return IperfResult(error=data["error"])

    end = data.get("end", {})
    sent = end.get("sum_sent", {})

    return IperfResult(
        sent_bytes=sent.get("bytes", 0),
        sent_bps=sent.get("bits_per_second", 0.0),
        retransmits=sent.get("retransmits", 0),
        duration=sent.get("seconds", 0.0),
        success=True,
    )
