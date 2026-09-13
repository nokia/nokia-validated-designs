#!/usr/bin/env python3
# © 2025 Nokia
# Licensed under the BSD 3-Clause License
# SPDX-License-Identifier: BSD-3-Clause
"""
connmon - near-real-time DCI connectivity monitor.

Drives the *running* containerlab clients (network-multitool netns endpoints
created by base-configs/*.sh) over `docker exec` and continuously reports
per-flow statistics that refresh in place.

Two modes:
  ping  (default)  one long-running `ping` per flow -> loss% + RTT, per packet
  iperf            one iperf3 TCP stream per flow   -> throughput + retransmits

Examples:
  ./tools/connmon.py                         # ping every default cross-DC flow
  ./tools/connmon.py --service l2            # only BD-A (vlan 100) flows
  ./tools/connmon.py --service l2b --bidir   # BD-B both directions
  ./tools/connmon.py --mode iperf --rate 20M # throughput monitor
  ./tools/connmon.py --mode iperf -P 4       # 4 sub-streams/flow -> L3 ECMP spread
  ./tools/connmon.py --size 1400             # 1400B packets (default 1200B)
  ./tools/connmon.py --flows mh1-l2a:mh2-l2a,sh1-l3:sh2-l3

Interactive controls (when run in a terminal): flows start OFF; each flow is
labelled with a letter; press it to start/stop that flow, '+' to start all,
'-' to stop all, and 'q' (or Ctrl-C) to quit. Use --start-all to launch every
flow immediately, or --no-interactive to disable the keyboard controls (which
also auto-starts all flows).

Run from anywhere on the host where the lab is deployed. Stop with 'q'/Ctrl-C;
a final summary is printed.
"""
from __future__ import annotations

import argparse
import collections
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Topology facts (mirrors tests/conftest.py ENDPOINTS)
# --------------------------------------------------------------------------- #

# key -> (container, netns, ip, dc, service)
ENDPOINTS: dict[str, tuple[str, str, str, int, str]] = {
    # DC1 BD-A (vlan 100)            container  netns      ip            dc svc
    "mh1-l2a":  ("mh-dc1",  "mh1-l2a",  "10.100.0.11", 1, "l2"),
    "mh1-l2b":  ("mh-dc1",  "mh1-l2b",  "10.100.0.12", 1, "l2"),
    "sh1-l2":   ("sh-dc1",  "sh1-l2",   "10.100.0.13", 1, "l2"),
    "mh1b-l2a": ("mh-dc1b", "mh1b-l2a", "10.100.0.14", 1, "l2"),
    "mh1b-l2b": ("mh-dc1b", "mh1b-l2b", "10.100.0.15", 1, "l2"),
    # DC1 BD-B (vlan 110)
    "mh1-l2c":  ("mh-dc1",  "mh1-l2c",  "10.110.0.11", 1, "l2b"),
    "mh1-l2d":  ("mh-dc1",  "mh1-l2d",  "10.110.0.12", 1, "l2b"),
    "sh1-l2b":  ("sh-dc1",  "sh1-l2b",  "10.110.0.13", 1, "l2b"),
    # DC1 L3 (vlan 200)
    "mh1-l3a":  ("mh-dc1",  "mh1-l3a",  "10.200.1.11", 1, "l3"),
    "mh1-l3b":  ("mh-dc1",  "mh1-l3b",  "10.200.1.12", 1, "l3"),
    "sh1-l3":   ("sh-dc1",  "sh1-l3",   "10.200.1.13", 1, "l3"),
    "mh1b-l3a": ("mh-dc1b", "mh1b-l3a", "10.200.1.14", 1, "l3"),
    "mh1b-l3b": ("mh-dc1b", "mh1b-l3b", "10.200.1.15", 1, "l3"),
    # DC2 BD-A (vlan 100)
    "mh2-l2a":  ("mh-dc2",  "mh2-l2a",  "10.100.0.21", 2, "l2"),
    "mh2-l2b":  ("mh-dc2",  "mh2-l2b",  "10.100.0.22", 2, "l2"),
    "sh2-l2":   ("sh-dc2",  "sh2-l2",   "10.100.0.23", 2, "l2"),
    "mh2b-l2a": ("mh-dc2b", "mh2b-l2a", "10.100.0.24", 2, "l2"),
    "mh2b-l2b": ("mh-dc2b", "mh2b-l2b", "10.100.0.25", 2, "l2"),
    # DC2 BD-B (vlan 110)
    "mh2-l2c":  ("mh-dc2",  "mh2-l2c",  "10.110.0.21", 2, "l2b"),
    "mh2-l2d":  ("mh-dc2",  "mh2-l2d",  "10.110.0.22", 2, "l2b"),
    "sh2-l2b":  ("sh-dc2",  "sh2-l2b",  "10.110.0.23", 2, "l2b"),
    # DC2 L3 (vlan 200)
    "mh2-l3a":  ("mh-dc2",  "mh2-l3a",  "10.200.2.21", 2, "l3"),
    "mh2-l3b":  ("mh-dc2",  "mh2-l3b",  "10.200.2.22", 2, "l3"),
    "sh2-l3":   ("sh-dc2",  "sh2-l3",   "10.200.2.23", 2, "l3"),
    "mh2b-l3a": ("mh-dc2b", "mh2b-l3a", "10.200.2.24", 2, "l3"),
    "mh2b-l3b": ("mh-dc2b", "mh2b-l3b", "10.200.2.25", 2, "l3"),
}

# default cross-DC flows (src_key, dst_key) - one per client class per service
DEFAULT_FLOWS: list[tuple[str, str]] = [
    # BD-A (vlan 100): mh leaf1/2, mh leaf3/4, single-homed
    ("mh1-l2a", "mh2-l2a"),
    ("mh1b-l2a", "mh2b-l2a"),
    ("sh1-l2", "sh2-l2"),
    # BD-B (vlan 110): stretched, pinned to dcgw2/dcgw4
    ("mh1-l2c", "mh2-l2c"),
    ("sh1-l2b", "sh2-l2b"),
    # L3 DCI: mh leaf1/2, mh leaf3/4, single-homed
    ("mh1-l3a", "mh2-l3a"),
    ("mh1b-l3a", "mh2b-l3a"),
    ("sh1-l3", "sh2-l3"),
]

# --------------------------------------------------------------------------- #
# container-name resolution (prefix-agnostic, e.g. clab-<lab>-mh-dc1)
# --------------------------------------------------------------------------- #

_name_cache: list[str] | None = None


def _docker_names() -> list[str]:
    global _name_cache
    if _name_cache is None:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=30).stdout
        _name_cache = out.split()
    return _name_cache


def cname(node: str) -> str:
    for n in _docker_names():
        if n == node or n.endswith(f"-{node}"):
            return n
    return node


def ep_mac(container: str, netns: str) -> str:
    """Read the eth0 MAC of a client netns (set by base-configs/*.sh)."""
    try:
        out = subprocess.run(
            ["docker", "exec", cname(container), "ip", "netns", "exec", netns,
             "cat", "/sys/class/net/eth0/address"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return out or "??:??:??:??:??:??"
    except Exception:
        return "??:??:??:??:??:??"


# --------------------------------------------------------------------------- #
# per-flow state
# --------------------------------------------------------------------------- #

@dataclass
class Flow:
    name: str
    src_key: str
    dst_key: str
    container: str          # resolved container name
    netns: str
    dst_ip: str
    service: str
    src_ip: str = ""
    src_mac: str = ""
    dst_mac: str = ""
    # live counters (guarded by lock)
    sent: int = 0
    recv: int = 0
    rtt_last: float = 0.0
    rtt_sum: float = 0.0
    rtt_n: int = 0
    rtt_min: float = float("inf")
    rtt_max: float = 0.0
    mbps: float = 0.0       # iperf throughput (last interval)
    retr: int = 0           # iperf cumulative retransmits
    mbps_sum: float = 0.0
    mbps_n: int = 0
    last_update: float = 0.0
    proc: subprocess.Popen | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    # lifecycle (interactive start/stop)
    port: int = 0
    running: bool = False
    stop_evt: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    life_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def lost(self) -> int:
        return max(0, self.sent - self.recv)

    @property
    def loss_pct(self) -> float:
        return (self.lost / self.sent * 100.0) if self.sent else 0.0

    @property
    def rtt_avg(self) -> float:
        return (self.rtt_sum / self.rtt_n) if self.rtt_n else 0.0

    @property
    def mbps_avg(self) -> float:
        return (self.mbps_sum / self.mbps_n) if self.mbps_n else 0.0


# --------------------------------------------------------------------------- #
# readers (one thread per flow)
# --------------------------------------------------------------------------- #

_RE_REPLY = re.compile(r"icmp_seq=(\d+).*?time=([\d.]+)\s*ms")
_RE_NOANS = re.compile(r"no answer yet for icmp_seq=(\d+)")
_RE_IPERF = re.compile(
    r"([\d.]+)\s+([KMG]?)bits/sec(?:\s+(\d+))?")


def _ping_reader(flow: Flow, interval: float, size: int, stop: threading.Event):
    cmd = ["docker", "exec", flow.container, "ip", "netns", "exec", flow.netns,
           "ping", "-O", "-n", "-i", str(interval), "-W", "1",
           "-s", str(size), flow.dst_ip]
    flow.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in flow.proc.stdout:                      # type: ignore[union-attr]
        if stop.is_set():
            break
        m = _RE_REPLY.search(line)
        if m:
            seq, rtt = int(m.group(1)), float(m.group(2))
            with flow.lock:
                flow.recv += 1
                flow.sent = max(flow.sent, seq, flow.recv)
                flow.rtt_last = rtt
                flow.rtt_sum += rtt
                flow.rtt_n += 1
                flow.rtt_min = min(flow.rtt_min, rtt)
                flow.rtt_max = max(flow.rtt_max, rtt)
                flow.last_update = time.time()
            continue
        m = _RE_NOANS.search(line)
        if m:
            with flow.lock:
                flow.sent = max(flow.sent, int(m.group(1)))


_RE_UDP_LOSS = re.compile(r"(\d+)/(\d+)\s+\(([\d.eE+-]+)%\)")


def _iperf_reader(flow: Flow, port: int, rate: str, size: int, parallel: int,
                  stop: threading.Event):
    """
    UDP iperf3: TCP collapses in this software-forwarding lab, so we run UDP at a
    capped rate and read the *server* side, whose per-second lines carry both the
    received throughput and the loss ratio.

    With parallel > 1 each flow spreads over N sub-streams (distinct L4 ports),
    which gives the fabric/DCGW hash enough entropy to load-balance an L3-DCI
    flow across both DCGWs (ECMP). In that case iperf3 prints per-stream interval
    lines plus a single [SUM] aggregate, so we read only the [SUM] line; with a
    single stream there is no [SUM] and we read the per-stream line directly.
    Note: --rate is per sub-stream, so total offered load is rate * parallel.
    """
    dst_c, dst_ns, _, _, _ = ENDPOINTS[flow.dst_key]
    # server in the destination netns, foreground so we can read its interval output
    srv = ["docker", "exec", cname(dst_c), "ip", "netns", "exec", dst_ns,
           "iperf3", "-s", "-1", "-i", "1", "-p", str(port), "--forceflush"]
    flow.proc = subprocess.Popen(srv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
    time.sleep(0.5)
    # client in the source netns, detached (we don't need its output)
    # -l <size> keeps each datagram under the VXLAN-reduced fabric MTU (else dropped)
    # -P <parallel> fans out into N sub-streams for ECMP hash entropy
    cli = ["docker", "exec", "-d", flow.container, "ip", "netns", "exec", flow.netns,
           "iperf3", "-c", flow.dst_ip, "-u", "-b", rate, "-l", str(size),
           "-P", str(parallel), "-p", str(port), "-t", "86400"]
    subprocess.run(cli, capture_output=True, text=True, timeout=30)

    want_sum = parallel > 1
    for line in flow.proc.stdout:                      # type: ignore[union-attr]
        if stop.is_set():
            break
        if "bits/sec" not in line or "receiver" in line or "sender" in line:
            continue
        # match per-stream lines for a single stream, the [SUM] aggregate for many
        if ("[SUM]" in line) != want_sum:
            continue
        m = _RE_IPERF.search(line)
        if not m:
            continue
        val, unit = float(m.group(1)), m.group(2)
        mbps = val * {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}[unit]
        loss = _RE_UDP_LOSS.search(line)
        with flow.lock:
            flow.mbps = mbps
            flow.mbps_sum += mbps
            flow.mbps_n += 1
            if loss:
                flow.recv += int(loss.group(2)) - int(loss.group(1))
                flow.sent += int(loss.group(2))
            flow.last_update = time.time()


# --------------------------------------------------------------------------- #
# flow lifecycle (start/stop, for interactive control)
# --------------------------------------------------------------------------- #

def _reset_counters(f: Flow):
    with f.lock:
        f.sent = f.recv = 0
        f.rtt_last = f.rtt_sum = 0.0
        f.rtt_n = 0
        f.rtt_min = float("inf")
        f.rtt_max = 0.0
        f.mbps = f.mbps_sum = 0.0
        f.mbps_n = 0
        f.last_update = 0.0


def start_flow(f: Flow, mode: str, args) -> None:
    """(Re)start a flow's generator + reader thread with fresh counters."""
    with f.life_lock:
        if f.running:
            return
        f.stop_evt = threading.Event()
        _reset_counters(f)
        if mode == "ping":
            t = threading.Thread(target=_ping_reader,
                                 args=(f, args.interval, args.size, f.stop_evt),
                                 daemon=True)
        else:
            t = threading.Thread(target=_iperf_reader,
                                 args=(f, f.port, args.rate, args.size,
                                       args.parallel, f.stop_evt),
                                 daemon=True)
        f.thread = t
        f.running = True
        t.start()


def stop_flow(f: Flow, mode: str) -> None:
    """Stop a flow: signal its reader, kill the generator process(es)."""
    with f.life_lock:
        if not f.running:
            return
        f.stop_evt.set()
        f.running = False
        proc = f.proc
    if proc and proc.poll() is None:
        proc.terminate()
    # NB: terminating the host-side `docker exec` does NOT signal the process
    # running inside the container, so we must explicitly kill it in its netns
    # (otherwise ping/iperf keeps generating traffic after the flow is "stopped").
    if mode == "ping":
        dst_re = re.escape(f.dst_ip)
        subprocess.run(["docker", "exec", f.container, "ip", "netns", "exec",
                        f.netns, "pkill", "-9", "-f", f"ping .* {dst_re}$"],
                       capture_output=True, text=True)
    else:
        # kill only THIS flow's iperf3 (matched by its unique port) in both netns
        dst_c, dst_ns, _, _, _ = ENDPOINTS[f.dst_key]
        for c, ns in ((f.container, f.netns), (cname(dst_c), dst_ns)):
            subprocess.run(["docker", "exec", c, "ip", "netns", "exec", ns,
                            "pkill", "-9", "-f", f"iperf3.*-p {f.port}"],
                           capture_output=True, text=True)
    with f.lock:
        f.last_update = 0.0


def _key_reader(keyq: "collections.deque[str]", stop: threading.Event):
    """Push single keystrokes onto keyq (terminal already in cbreak mode)."""
    import select
    while not stop.is_set():
        r, _, _ = select.select([sys.stdin], [], [], 0.2)
        if r:
            try:
                ch = sys.stdin.read(1)
            except Exception:
                break
            if ch:
                keyq.append(ch)


def _handle_key(ch: str, flows: list[Flow], args, stop: threading.Event):
    if ch in ("q", "Q"):
        stop.set()
    elif ch == "+":
        for f in flows:
            start_flow(f, args.mode, args)
    elif ch == "-":
        for f in flows:
            stop_flow(f, args.mode)
    elif "a" <= ch <= "z":
        i = ord(ch) - ord("a")
        if i < len(flows):
            f = flows[i]
            if f.running:
                stop_flow(f, args.mode)
            else:
                start_flow(f, args.mode, args)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

GREEN, RED, YELLOW, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _color(s: str, c: str, enabled: bool) -> str:
    return f"{c}{s}{RST}" if enabled else s


def _state(flow: Flow, interval: float, mode: str) -> tuple[str, str]:
    """Return (label, ansi-color)."""
    if not flow.running:
        return "OFF", DIM
    age = time.time() - flow.last_update
    if flow.last_update == 0.0:
        return "INIT", YELLOW
    if age > max(3.0, 3 * interval):
        return "DOWN", RED
    if mode == "ping":
        return ("UP", GREEN) if flow.loss_pct < 1.0 else ("LOSS", YELLOW)
    return ("UP", GREEN) if flow.mbps > 0.01 else ("DOWN", RED)


def render(flows: list[Flow], mode: str, interval: float, start: float,
           color: bool, interactive: bool = False):
    now = time.time()
    lines = []
    title = "ping (loss/RTT)" if mode == "ping" else "iperf3 UDP (throughput/loss)"
    nrun = sum(1 for f in flows if f.running)
    lines.append(f"DCI connectivity monitor - {title}   "
                 f"elapsed {int(now - start):5d}s   flows {nrun}/{len(flows)} up   "
                 f"{time.strftime('%H:%M:%S')}")
    lines.append("")
    ids = (f"{'K':<3}{'FLOW':<22}{'SRC-IP':<16}{'SRC-MAC':<19}"
           f"{'DST-IP':<16}{'DST-MAC':<19}{'SVC':<5}{'STATE':<7}")
    if mode == "ping":
        hdr = (ids + f"{'SENT':>7}{'RECV':>7}{'LOSS%':>8}"
               f"{'LAST':>9}{'AVG':>9}{'MAX':>9}")
    else:
        hdr = ids + f"{'Mbps':>10}{'AVG Mbps':>11}{'LOSS%':>9}"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for i, f in enumerate(flows):
        key = chr(97 + i) if i < 26 else " "
        with f.lock:
            st, c = _state(f, interval, mode)
            svc = {"l2": "BD-A", "l2b": "BD-B", "l3": "L3"}.get(f.service, f.service)
            ident = (f"{key:<3}{f.name:<22}{f.src_ip:<16}{f.src_mac:<19}"
                     f"{f.dst_ip:<16}{f.dst_mac:<19}")
            if mode == "ping":
                row = (f"{ident}{svc:<5}{_color(f'{st:<7}', c, color)}"
                       f"{f.sent:>7}{f.recv:>7}{f.loss_pct:>7.2f}%"
                       f"{f.rtt_last:>8.2f}m{f.rtt_avg:>8.2f}m{f.rtt_max:>8.2f}m")
            else:
                row = (f"{ident}{svc:<5}{_color(f'{st:<7}', c, color)}"
                       f"{f.mbps:>10.2f}{f.mbps_avg:>11.2f}{f.loss_pct:>8.2f}%")
        lines.append(row)
    lines.append("")
    if interactive:
        lines.append("keys: [a-z] toggle flow   [+] start all   [-] stop all   [q] quit")
    else:
        lines.append("(Ctrl-C to stop)")
    out = "\n".join(lines)
    if color and sys.stdout.isatty():
        sys.stdout.write("\033[H\033[J" + out + "\n")     # home + clear-to-end
    else:
        sys.stdout.write(out + "\n\n")
    sys.stdout.flush()


def summary(flows: list[Flow], mode: str):
    print("\n=== final summary ===")
    for f in flows:
        ident = (f"{f.name:<22} [src {f.src_ip} {f.src_mac}"
                 f" -> dst {f.dst_ip} {f.dst_mac}]")
        if mode == "ping":
            print(f"  {ident} sent={f.sent} recv={f.recv} "
                  f"loss={f.loss_pct:.2f}% rtt avg/max={f.rtt_avg:.2f}/{f.rtt_max:.2f} ms")
        else:
            print(f"  {ident} last={f.mbps:.2f} avg={f.mbps_avg:.2f} Mbps "
                  f"loss={f.loss_pct:.2f}%")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_flows(args) -> list[Flow]:
    if args.flows:
        pairs = []
        for spec in args.flows.split(","):
            src, _, dst = spec.partition(":")
            src, dst = src.strip(), dst.strip()
            if src not in ENDPOINTS or dst not in ENDPOINTS:
                sys.exit(f"unknown endpoint in flow '{spec}'. "
                         f"valid keys: {', '.join(sorted(ENDPOINTS))}")
            pairs.append((src, dst))
    else:
        pairs = [(s, d) for (s, d) in DEFAULT_FLOWS
                 if args.service == "all" or ENDPOINTS[s][4] == args.service]
        if args.bidir:
            pairs += [(d, s) for (s, d) in pairs]

    flows = []
    for i, (src, dst) in enumerate(pairs):
        c, ns, ip, _, svc = ENDPOINTS[src]
        dc, dns, dip, _, _ = ENDPOINTS[dst]
        flows.append(Flow(name=f"{src} -> {dst}", src_key=src, dst_key=dst,
                          container=cname(c), netns=ns,
                          dst_ip=dip, service=svc,
                          src_ip=ip, src_mac=ep_mac(c, ns),
                          dst_mac=ep_mac(dc, dns),
                          # base 5301 (NOT 5201): base-configs already run a
                          # persistent `iperf3 -s -D` on the default port 5201 in
                          # every client netns, so starting on 5201 collides and
                          # leaves the first flow stuck in INIT.
                          port=5301 + i))
    return flows


def preflight(flows: list[Flow]):
    have = set(_docker_names())
    need = {f.container for f in flows}
    need |= {cname(ENDPOINTS[f.dst_key][0]) for f in flows}
    missing = sorted(c for c in need if c not in have)
    if missing:
        sys.exit(f"client containers not running: {missing}\n"
                 f"deploy the lab first: containerlab deploy -t dci-without-eda.clab.yaml")


def main():
    ap = argparse.ArgumentParser(description="near-real-time DCI connectivity monitor")
    ap.add_argument("--mode", choices=["ping", "iperf"], default="ping")
    ap.add_argument("--service", choices=["all", "l2", "l2b", "l3"], default="all",
                    help="restrict default flows to one service (l2=BD-A, l2b=BD-B, l3)")
    ap.add_argument("--flows", help="custom 'src:dst,src:dst' endpoint-key pairs")
    ap.add_argument("--bidir", action="store_true", help="also add reverse flows")
    ap.add_argument("--interval", type=float, default=1.0, help="ping interval (s)")
    ap.add_argument("--rate", default="3M",
                    help="iperf UDP per-sub-stream rate (keep modest; software forwarding)")
    ap.add_argument("--parallel", "-P", type=int, default=1,
                    help="iperf3 parallel sub-streams per flow (-P). >1 adds L4 "
                         "entropy so L3-DCI flows hash across both DCGWs (ECMP). "
                         "Total offered load per flow = rate * parallel.")
    ap.add_argument("--size", type=int, default=1200,
                    help="packet/datagram payload size in bytes "
                         "(ping -s / iperf -l; keep under the VXLAN MTU)")
    ap.add_argument("--refresh", type=float, default=1.0, help="screen refresh (s)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-interactive", action="store_true",
                    help="disable keyboard start/stop controls (just run all flows)")
    ap.add_argument("--start-all", action="store_true",
                    help="start every flow running immediately (default: interactive "
                         "sessions start with all flows OFF — press a letter or '+' to start). "
                         "Always implied when --no-interactive / non-TTY.")
    args = ap.parse_args()

    color = not args.no_color
    flows = build_flows(args)
    preflight(flows)

    interactive = (not args.no_interactive
                   and sys.stdin.isatty() and sys.stdout.isatty())

    stop = threading.Event()
    # Interactive sessions start with all flows OFF (toggle with a letter / '+').
    # Without a keyboard there is no way to start them, so auto-start everything.
    if not interactive or args.start_all:
        for f in flows:
            start_flow(f, args.mode, args)

    def _stop(_sig, _frm):
        stop.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    keyq: "collections.deque[str]" = collections.deque()
    old_termios = None
    if interactive:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)                      # 1-key reads, but keep Ctrl-C (ISIG)
        threading.Thread(target=_key_reader, args=(keyq, stop), daemon=True).start()

    start = time.time()
    if color and sys.stdout.isatty():
        sys.stdout.write("\033[2J")
    try:
        while not stop.is_set():
            while keyq:
                _handle_key(keyq.popleft(), flows, args, stop)
            render(flows, args.mode, args.interval, start, color, interactive)
            stop.wait(args.refresh)
    finally:
        stop.set()
        for f in flows:
            stop_flow(f, args.mode)
        if old_termios is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
        render(flows, args.mode, args.interval, start, color, interactive)
        summary(flows, args.mode)


if __name__ == "__main__":
    main()
