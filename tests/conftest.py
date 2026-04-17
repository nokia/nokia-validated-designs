"""
pytest configuration and shared fixtures for network behavior tests.

Self-contained: discovers topology, clients, BDs, VRFs, and links entirely
from the .clab.yml file, client startup scripts, and fcli queries against
the live network.  No project-specific imports required.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers.faults import FaultManager
from tests.helpers.fcli import FcliClient

logger = logging.getLogger(__name__)

DEFAULT_GNMI_PORT = 57400


# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("nvd", "Nokia Validated Design test options")
    group.addoption(
        "--clab-topo",
        dest="clab_topo",
        default=None,
        help="Path to the containerlab .clab.yml topology file (required for integration tests)",
    )
    group.addoption(
        "--gnmi-port",
        dest="gnmi_port",
        type=int,
        default=None,
        help=(
            "gNMI port for SR Linux nodes (fcli -p). "
            "If omitted, auto-detected from a running srlinux container."
        ),
    )


# ---------------------------------------------------------------------------
# Topology model
# ---------------------------------------------------------------------------


@dataclass
class ClientAttachment:
    """A client's IP attachment to a bridge domain."""

    client_container: str
    client_ip: str
    bridge_domain: str
    gateway: str
    router: str


@dataclass
class RoutedClient:
    """A client with a routed (L3) attachment -- /31 or /32 prefix."""

    client_container: str
    client_ip: str
    gateway: str
    router: str
    name: str


@dataclass
class FabricInfo:
    """Discovered fabric metadata (replaces FabricIntent)."""

    bridge_domains: list[str] = field(default_factory=list)
    ip_vrfs: list[str] = field(default_factory=list)
    lags: list[dict] = field(default_factory=list)
    static_route_prefixes: list[str] = field(default_factory=list)


@dataclass
class ClabTopology:
    """Parsed containerlab topology with resolved container names."""

    topo_path: str
    topo_name: str
    topo_prefix: str = ""
    gnmi_port: int | None = None
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)

    leaf_nodes: list[str] = field(default_factory=list)
    spine_nodes: list[str] = field(default_factory=list)
    srlinux_nodes: list[str] = field(default_factory=list)
    client_containers: list[str] = field(default_factory=list)

    client_attachments: list[ClientAttachment] = field(default_factory=list)
    routed_clients: list[RoutedClient] = field(default_factory=list)

    isl_links: list[dict[str, str]] = field(default_factory=list)
    edge_links: list[dict[str, str]] = field(default_factory=list)

    fabric: FabricInfo = field(default_factory=FabricInfo)

    def container_name(self, node: str) -> str:
        if self.topo_prefix:
            return f"{self.topo_prefix}-{node}"
        return node

    def srlinux_containers(self) -> list[str]:
        return [self.container_name(n) for n in self.srlinux_nodes]


# ---------------------------------------------------------------------------
# Topology discovery (pure parsing, no project imports)
# ---------------------------------------------------------------------------


_topology_cache: dict[str, ClabTopology] = {}

_GNMI_PORT_RE = re.compile(r"port\s+(\d+)")

_IP_ADDR_RE = re.compile(
    r"ip\s+addr\s+add\s+(\d+\.\d+\.\d+\.\d+)/(\d+)\s+dev\s+(\S+)"
)
_DEFAULT_ROUTE_RE = re.compile(
    r"ip\s+route\s+add\s+default\s+via\s+(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)"
)


def _detect_gnmi_port(container: str) -> int:
    """Detect the gNMI port from a running SR Linux container.

    Tries two schema paths because SRL renamed the tree:
      - ``/system grpc-server *``  (25.x+)
      - ``/system gnmi-server *``  (older releases)

    Falls back to ``DEFAULT_GNMI_PORT`` on any failure.
    """
    cli_commands = [
        "info flat /system grpc-server *",
        "info flat /system gnmi-server *",
    ]
    for cli_cmd in cli_commands:
        cmd = ["docker", "exec", container, "sr_cli", "-e", cli_cmd]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0 or not result.stdout.strip():
                continue
            m = _GNMI_PORT_RE.search(result.stdout)
            if m:
                port = int(m.group(1))
                logger.info("Auto-detected gNMI port %d from %s", port, container)
                return port
        except Exception as exc:
            logger.debug("gNMI port detection failed for %s: %s", container, exc)

    logger.info("Using default gNMI port %d", DEFAULT_GNMI_PORT)
    return DEFAULT_GNMI_PORT


def _discover_topology(topo_file: str, gnmi_port: int | None = None) -> ClabTopology:
    """
    Build a complete ClabTopology from a .clab.yml file by:
      1. Parsing the YAML for nodes, links, and prefix
      2. Classifying nodes as SRL vs linux from the YAML
      3. Classifying links as ISL (SRL<->SRL) vs edge (SRL<->linux)
      4. Parsing client startup scripts for IPs and gateways
      5. Querying fcli to map gateway subnets to BDs and VRFs
    """
    topo_path = str(Path(topo_file).resolve())

    if topo_path in _topology_cache:
        return _topology_cache[topo_path]

    with open(topo_path) as f:
        clab = yaml.safe_load(f)

    topo_name = clab.get("name", "")
    nodes = clab.get("topology", {}).get("nodes", {})
    links = clab.get("topology", {}).get("links", [])
    default_kind = clab.get("topology", {}).get("defaults", {}).get("kind", "")

    raw_prefix = clab.get("prefix")
    if raw_prefix is None:
        topo_prefix = f"clab-{topo_name}"
    elif raw_prefix == "":
        topo_prefix = ""
    else:
        topo_prefix = raw_prefix

    topo = ClabTopology(
        topo_path=topo_path, topo_name=topo_name,
        topo_prefix=topo_prefix, gnmi_port=gnmi_port, nodes=nodes,
    )

    # 1. Classify nodes
    srl_nodes: set[str] = set()
    linux_nodes: set[str] = set()
    for name, ndata in nodes.items():
        kind = ndata.get("kind", default_kind)
        if kind in ("nokia_srlinux", "srl"):
            srl_nodes.add(name)
        elif kind == "linux":
            linux_nodes.add(name)
        elif "srlinux" in str(ndata.get("image", "")):
            srl_nodes.add(name)
        else:
            linux_nodes.add(name)

    # Classify SRL nodes as leaf/spine by name convention and link topology.
    # Spines connect only to leaves (never to linux), leaves connect to both.
    srl_to_linux: set[str] = set()
    for link in links:
        eps = link.get("endpoints", [])
        if len(eps) != 2:
            continue
        a_node = eps[0].split(":")[0]
        b_node = eps[1].split(":")[0]
        if a_node in srl_nodes and b_node in linux_nodes:
            srl_to_linux.add(a_node)
        if b_node in srl_nodes and a_node in linux_nodes:
            srl_to_linux.add(b_node)

    for name in sorted(srl_nodes):
        topo.srlinux_nodes.append(name)
        if name in srl_to_linux or "leaf" in name.lower():
            topo.leaf_nodes.append(name)
        else:
            topo.spine_nodes.append(name)

    topo.client_containers = sorted(linux_nodes)

    # 2. Classify links as ISL or edge
    for link in links:
        eps = link.get("endpoints", [])
        if len(eps) != 2:
            continue
        a_node, a_intf = eps[0].split(":")
        b_node, b_intf = eps[1].split(":")
        a_intf = _clab_intf_to_srl(a_intf)
        b_intf = _clab_intf_to_srl(b_intf)

        if a_node in srl_nodes and b_node in srl_nodes:
            topo.isl_links.append({
                "name": f"{a_node}-{a_intf}",
                "local_node": a_node,
                "local_interface": a_intf,
                "local_container": topo.container_name(a_node),
                "remote_node": b_node,
                "remote_interface": b_intf,
                "remote_container": topo.container_name(b_node),
            })
        elif a_node in srl_nodes and b_node in linux_nodes:
            topo.edge_links.append({
                "name": f"{a_node}-{a_intf}",
                "node": a_node,
                "interface": a_intf,
                "container": topo.container_name(a_node),
                "client": b_node,
            })
        elif b_node in srl_nodes and a_node in linux_nodes:
            topo.edge_links.append({
                "name": f"{b_node}-{b_intf}",
                "node": b_node,
                "interface": b_intf,
                "container": topo.container_name(b_node),
                "client": a_node,
            })

    # 3. Auto-detect gNMI port if not explicitly provided
    if topo.gnmi_port is None and topo.srlinux_nodes:
        container = topo.container_name(topo.srlinux_nodes[0])
        topo.gnmi_port = _detect_gnmi_port(container)

    # 4. Discover client IPs from startup scripts
    _discover_clients(topo)

    _topology_cache[topo_path] = topo
    return topo


def _clab_intf_to_srl(clab_intf: str) -> str:
    """Convert clab shorthand (e1-3) to SRL name (ethernet-1-3)."""
    m = re.match(r"e(\d+)-(\d+)", clab_intf)
    if m:
        return f"ethernet-{m.group(1)}-{m.group(2)}"
    if clab_intf.startswith("ethernet-"):
        return clab_intf.replace("/", "-")
    return clab_intf


def _discover_clients(topo: ClabTopology) -> None:
    """
    Parse client startup scripts to extract IPs and gateways, then query
    fcli to map gateways to bridge domains and VRFs.

    Strategy:
      1. Each mac-vrf NI entry with an IRB sub-interface has the gateway IP
         directly (ip-prefix field).  Build gateway -> mac-vrf mapping.
      2. Each ip-vrf NI entry with the same IRB sub-interface on the same
         node gives the VRF name.  Build gateway -> vrf mapping.
      3. Match each client's gateway to the discovered BD/VRF pair.
    """
    # 1. Query fcli for NI data and build gateway -> (BD, VRF) mapping
    gw_to_bd_vrf: dict[str, tuple[str, str]] = {}
    irb_to_vrf: dict[str, str] = {}  # keyed by "irb0.N"
    ni_data: list[dict] = []

    try:
        fcli = FcliClient(topo_path=topo.topo_path, gnmi_port=topo.gnmi_port)
        ni_data.extend(fcli.network_instances())

        bd_names: set[str] = set()
        vrf_names: set[str] = set()

        # First pass: collect VRF names for each IRB sub-interface
        for entry in ni_data:
            ni_name = entry.get("NI", "")
            ni_type = entry.get("type", "")
            subitf = entry.get("Subitf", "")

            if ni_type == "ip-vrf" and ni_name != "mgmt" and "irb" in subitf:
                vrf_names.add(ni_name)
                irb_to_vrf[subitf] = ni_name

        # Second pass: mac-vrfs with IRB gateway IPs
        for entry in ni_data:
            ni_name = entry.get("NI", "")
            ni_type = entry.get("type", "")
            subitf = entry.get("Subitf", "")
            prefixes = entry.get("ip-prefix", [])
            if isinstance(prefixes, str):
                prefixes = [prefixes]

            if ni_type == "mac-vrf":
                bd_names.add(ni_name)
                if "irb" in subitf and prefixes:
                    vrf = irb_to_vrf.get(subitf, "")
                    for pfx in prefixes:
                        gw_ip = pfx.split("/")[0]
                        if ":" not in gw_ip:  # skip IPv6
                            gw_to_bd_vrf[gw_ip] = (ni_name, vrf)

            elif ni_type == "ip-vrf" and ni_name != "mgmt":
                vrf_names.add(ni_name)
                # Also collect non-IRB prefixes (routed interfaces)
                if "irb" not in subitf and prefixes:
                    for pfx in prefixes:
                        ip = pfx.split("/")[0]
                        if ":" not in ip:
                            gw_to_bd_vrf[ip] = ("", ni_name)

        topo.fabric.bridge_domains = sorted(bd_names)
        topo.fabric.ip_vrfs = sorted(vrf_names)

        # Discover static routes
        try:
            rib = fcli.ipv4_rib()
            for entry in rib:
                if entry.get("type", "").lower() == "static":
                    pfx = entry.get("Prefix", "")
                    if pfx and pfx not in topo.fabric.static_route_prefixes:
                        topo.fabric.static_route_prefixes.append(pfx)
        except Exception:
            pass

        # Discover LAGs
        try:
            lag_data = fcli.lag()
            if lag_data:
                seen: set[str] = set()
                for entry in lag_data:
                    lag_id = f"{entry.get('Node', '')}:{entry.get('lag', '')}"
                    if lag_id not in seen:
                        seen.add(lag_id)
                        topo.fabric.lags.append(entry)
        except Exception:
            pass

    except Exception as e:
        logger.warning("fcli discovery failed, BD/VRF mapping unavailable: %s", e)

    # 2. Parse client startup scripts for IPs and gateways
    build_dir = Path(topo.topo_path).parent
    config_dir = build_dir / "client-configs"
    if not config_dir.exists():
        return

    for script in sorted(config_dir.glob("*.sh")):
        client_name = script.stem
        container = topo.container_name(client_name)
        content = script.read_text()

        ip_lines = _IP_ADDR_RE.findall(content)
        gw_lines = _DEFAULT_ROUTE_RE.findall(content)

        # Build dev -> gateway mapping from route entries
        dev_gw: dict[str, str] = {}
        for gw_ip, gw_dev in gw_lines:
            dev_gw[gw_dev] = gw_ip

        for ip, mask, dev in ip_lines:
            if dev == "lo":
                continue
            mask_int = int(mask)

            # Find gateway for this dev (or parent dev for VLAN sub-intf)
            gw = dev_gw.get(dev, "")
            if not gw:
                base_dev = dev.split(".")[0]
                gw = dev_gw.get(base_dev, "")

            if not gw:
                continue

            bd, vrf = gw_to_bd_vrf.get(gw, ("", ""))

            if bd:
                topo.client_attachments.append(ClientAttachment(
                    client_container=container,
                    client_ip=ip,
                    bridge_domain=bd,
                    gateway=gw,
                    router=vrf,
                ))
            elif mask_int == 31:
                # /31 routed interface -- find the VRF that contains this prefix
                if not vrf:
                    vrf = _find_vrf_for_ip(gw, ni_data)
                topo.routed_clients.append(RoutedClient(
                    client_container=container,
                    client_ip=ip,
                    gateway=gw,
                    router=vrf,
                    name=client_name,
                ))


def _find_vrf_for_ip(ip: str, ni_data: list[dict]) -> str:
    """Find which ip-vrf contains a given IP address."""
    for entry in ni_data:
        if entry.get("type") != "ip-vrf" or entry.get("NI") == "mgmt":
            continue
        prefixes = entry.get("ip-prefix", [])
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        for pfx in prefixes:
            try:
                net = ipaddress.ip_network(pfx, strict=False)
                if ipaddress.ip_address(ip) in net:
                    return entry.get("NI", "")
            except ValueError:
                continue
    return ""


# ---------------------------------------------------------------------------
# Helper for parametrization and convergence tests
# ---------------------------------------------------------------------------


def _build_topology_from_metafunc(metafunc) -> ClabTopology | None:
    topo_file = metafunc.config.getoption("clab_topo")
    if not topo_file:
        return None
    gnmi_port = metafunc.config.getoption("gnmi_port")
    return _discover_topology(topo_file, gnmi_port=gnmi_port)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def clab_topology(request: pytest.FixtureRequest) -> ClabTopology:
    """Session-scoped topology discovered from the live .clab.yml."""
    topo_file = request.config.getoption("clab_topo")
    if not topo_file:
        pytest.skip("--clab-topo not provided")
    gnmi_port = request.config.getoption("gnmi_port")
    topo = _discover_topology(topo_file, gnmi_port=gnmi_port)
    return topo


@pytest.fixture(scope="session")
def fcli(clab_topology: ClabTopology) -> FcliClient:
    """Session-scoped fcli client bound to the running topology."""
    return FcliClient(
        topo_path=clab_topology.topo_path, gnmi_port=clab_topology.gnmi_port,
    )


@pytest.fixture
def fault_manager() -> FaultManager:
    """Per-test FaultManager with automatic cleanup."""
    fm = FaultManager()
    yield fm
    fm.recover_all()


# ---------------------------------------------------------------------------
# pytest-html integration
# ---------------------------------------------------------------------------


def pytest_html_report_title(report):
    report.title = "NVD Network Behavior Test Report"


def pytest_configure(config):
    """Add topology metadata to the report's Environment table."""
    topo_file = config.getoption("clab_topo", default=None)
    if not topo_file:
        return

    gnmi_port = config.getoption("gnmi_port", default=None)
    try:
        topo = _discover_topology(topo_file, gnmi_port=gnmi_port)
    except Exception:
        return

    try:
        from pytest_metadata.plugin import metadata_key
        md = config.stash[metadata_key]
    except Exception:
        return

    md["Topology file"] = topo.topo_path
    md["Topology name"] = topo.topo_name
    md["gNMI port"] = str(topo.gnmi_port or DEFAULT_GNMI_PORT)
    md["Leaf nodes"] = f"{len(topo.leaf_nodes)} ({', '.join(topo.leaf_nodes)})"
    md["Spine nodes"] = f"{len(topo.spine_nodes)} ({', '.join(topo.spine_nodes)})"
    md["Client containers"] = f"{len(topo.client_containers)} ({', '.join(topo.client_containers)})"
    md["ISL links"] = str(len(topo.isl_links))
    md["Edge links"] = str(len(topo.edge_links))
    md["Bridge domains"] = ", ".join(topo.fabric.bridge_domains) or "(discovered at test time)"
    md["IP-VRFs"] = ", ".join(topo.fabric.ip_vrfs) or "(discovered at test time)"
    md["Client attachments"] = str(len(topo.client_attachments))
    md["Routed clients"] = str(len(topo.routed_clients))
    md["LAGs"] = str(len(topo.fabric.lags))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Inject record_property values as HTML extras in the report."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return

    try:
        import pytest_html
    except ImportError:
        return

    extras = getattr(report, "extras", [])

    props = [(k, v) for k, v in report.user_properties if k != ""]
    if props:
        rows = "".join(
            f"<tr><td style='padding:2px 8px;font-weight:bold;'>{k}</td>"
            f"<td style='padding:2px 8px;'>{v}</td></tr>"
            for k, v in props
        )
        html = (
            "<table style='border-collapse:collapse;margin:8px 0;'>"
            f"<tbody>{rows}</tbody></table>"
        )
        extras.append(pytest_html.extras.html(html))

    report.extras = extras
