"""
Docker-exec ping helpers for data-plane validation.

Provides single-shot, bidirectional, and continuous background ping
operations against Linux client containers in a containerlab topology.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field


@dataclass
class PingResult:
    """Result of a continuous ping session."""

    transmitted: int = 0
    received: int = 0
    loss_pct: float = 100.0
    rtt_min: float = 0.0
    rtt_avg: float = 0.0
    rtt_max: float = 0.0
    duration: float = 0.0
    timeline: list[bool] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.received > 0 and self.loss_pct < 100.0


def _docker_exec(container: str, cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container] + cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ping(
    src_container: str,
    dst_ip: str,
    *,
    src_ip: str = "",
    count: int = 3,
    timeout: int = 2,
    interval: float = 0.2,
) -> bool:
    """
    Single ping test via docker exec.

    Returns True if at least one reply is received.
    """
    cmd = ["ping", f"-c{count}", f"-W{timeout}", f"-i{interval}"]
    if src_ip:
        cmd.extend(["-I", src_ip])
    cmd.append(dst_ip)

    try:
        result = _docker_exec(src_container, cmd, timeout=count * timeout + 5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def ping_bidir(
    a_container: str,
    a_ip: str,
    b_container: str,
    b_ip: str,
    **kwargs,
) -> tuple[bool, bool]:
    """
    Bidirectional ping: A->B and B->A.

    Returns (a_to_b, b_to_a) booleans.
    """
    a_to_b = ping(a_container, b_ip, src_ip=a_ip, **kwargs)
    b_to_a = ping(b_container, a_ip, src_ip=b_ip, **kwargs)
    return a_to_b, b_to_a


class PingStream:
    """
    Background continuous ping that collects per-probe pass/fail data.

    Usage::

        stream = ping_continuous("clab-dc1-cl-l1", "172.16.10.254",
                                 src_ip="172.16.10.1")
        time.sleep(10)
        result = stream.stop()
        assert result.loss_pct < 5.0
    """

    def __init__(
        self,
        src_container: str,
        dst_ip: str,
        *,
        src_ip: str = "",
        interval: float = 0.1,
    ):
        self._container = src_container
        self._dst = dst_ip
        self._src = src_ip
        self._interval = interval
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._timeline: list[bool] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._start_time = 0.0

    def start(self) -> PingStream:
        cmd = [
            "docker", "exec", self._container,
            "ping", f"-i{self._interval}", f"-W1",
        ]
        if self._src:
            cmd.extend(["-I", self._src])
        cmd.append(self._dst)

        self._start_time = time.monotonic()
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    def _reader(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            if self._stopped.is_set():
                break
            is_reply = "bytes from" in line or "time=" in line
            with self._lock:
                self._timeline.append(is_reply)

    def stop(self) -> PingResult:
        self._stopped.set()
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

        if self._thread:
            self._thread.join(timeout=3)

        duration = time.monotonic() - self._start_time

        with self._lock:
            timeline = list(self._timeline)

        transmitted = len(timeline)
        received = sum(timeline)
        loss_pct = ((transmitted - received) / transmitted * 100) if transmitted else 100.0

        return PingResult(
            transmitted=transmitted,
            received=received,
            loss_pct=loss_pct,
            duration=duration,
            timeline=timeline,
        )


def ping_continuous(
    src_container: str,
    dst_ip: str,
    *,
    src_ip: str = "",
    interval: float = 0.1,
) -> PingStream:
    """Start a background continuous ping. Call .stop() to get results."""
    return PingStream(
        src_container, dst_ip, src_ip=src_ip, interval=interval,
    ).start()
