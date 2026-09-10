#!/usr/bin/env python3
"""One-shot generator for the dci-with-eda EDA manifests.

Emits the static YAML that lives under eda-manifests/. Run once; the YAML is the
deliverable. Schemas are aligned to what the live 26.8.1 cluster actually serves:
interfaces/routing/fabrics = v1, protocols/services = v2, mpls = v1alpha1.
"""
from pathlib import Path

OUT = Path("/home/wds/github/nokia-validated-designs/validated-designs/dci/dci-with-eda/eda-manifests")
OUT.mkdir(parents=True, exist_ok=True)

NS = "dci"
SRL_VER = "26.7.1"
NODE_PROFILE = f"clab-srlinux-{SRL_VER}"

HDR = """# © 2026 Nokia
# Licensed under the BSD 3-Clause License
# SPDX-License-Identifier: BSD-3-Clause
"""

P_LEAF, P_SPINE, P_GW = "7220 IXR-D2L", "7220 IXR-D3L", "7250 IXR-X3B"

NODES = {
    "leaf1":  (P_LEAF,  "172.21.21.11",  "leaf",  "dc1", "192.0.2.11"),
    "leaf2":  (P_LEAF,  "172.21.21.12",  "leaf",  "dc1", "192.0.2.12"),
    "leaf3":  (P_LEAF,  "172.21.21.13",  "leaf",  "dc1", "192.0.2.13"),
    "leaf4":  (P_LEAF,  "172.21.21.14",  "leaf",  "dc1", "192.0.2.14"),
    "spine1": (P_SPINE, "172.21.21.101", "spine", "dc1", "192.0.2.101"),
    "spine2": (P_SPINE, "172.21.21.102", "spine", "dc1", "192.0.2.102"),
    "dcgw1":  (P_GW,    "172.21.21.151", "dcgw",  "dc1", "192.0.2.151"),
    "dcgw2":  (P_GW,    "172.21.21.152", "dcgw",  "dc1", "192.0.2.152"),
    "leaf5":  (P_LEAF,  "172.21.21.15",  "leaf",  "dc2", "192.0.3.15"),
    "leaf6":  (P_LEAF,  "172.21.21.16",  "leaf",  "dc2", "192.0.3.16"),
    "leaf7":  (P_LEAF,  "172.21.21.17",  "leaf",  "dc2", "192.0.3.17"),
    "leaf8":  (P_LEAF,  "172.21.21.18",  "leaf",  "dc2", "192.0.3.18"),
    "spine3": (P_SPINE, "172.21.21.103", "spine", "dc2", "192.0.3.103"),
    "spine4": (P_SPINE, "172.21.21.104", "spine", "dc2", "192.0.3.104"),
    "dcgw3":  (P_GW,    "172.21.21.153", "dcgw",  "dc2", "192.0.3.153"),
    "dcgw4":  (P_GW,    "172.21.21.154", "dcgw",  "dc2", "192.0.3.154"),
    "p1":     (P_GW,    "172.21.21.201", "pe",    "wan", "192.0.100.201"),
    "p2":     (P_GW,    "172.21.21.202", "pe",    "wan", "192.0.100.202"),
}

LEAF_ASN = {"leaf1": 65401, "leaf2": 65402, "leaf3": 65403, "leaf4": 65404,
            "leaf5": 65405, "leaf6": 65406, "leaf7": 65407, "leaf8": 65408}

ISLS = []
for i, leaf in enumerate(["leaf1", "leaf2", "leaf3", "leaf4"], start=1):
    ISLS += [(leaf, 1, "spine1", i), (leaf, 2, "spine2", i)]
for i, leaf in enumerate(["leaf5", "leaf6", "leaf7", "leaf8"], start=1):
    ISLS += [(leaf, 1, "spine3", i), (leaf, 2, "spine4", i)]

# spine <-> DCGW. Kept out of ISLS because the DCGWs are not fabric nodes; these
# links are configured explicitly in 21-dcgw-uplinks.yaml.
UPLINKS = [("spine1", 5, "dcgw1", 1), ("spine2", 5, "dcgw1", 2),
           ("spine1", 6, "dcgw2", 1), ("spine2", 6, "dcgw2", 2),
           ("spine3", 5, "dcgw3", 1), ("spine4", 5, "dcgw3", 2),
           ("spine3", 6, "dcgw4", 1), ("spine4", 6, "dcgw4", 2)]

WAN = [
    ("dcgw1", 5, "10.255.0.0/31",  "dcgw2", 5, "10.255.0.1/31",  10,  "direct"),
    ("dcgw1", 3, "10.255.0.2/31",  "dcgw3", 3, "10.255.0.3/31",  10,  "direct"),
    ("dcgw1", 4, "10.255.0.4/31",  "dcgw4", 4, "10.255.0.5/31",  10,  "direct"),
    ("dcgw2", 4, "10.255.0.6/31",  "dcgw3", 4, "10.255.0.7/31",  10,  "direct"),
    ("dcgw2", 3, "10.255.0.8/31",  "dcgw4", 3, "10.255.0.9/31",  10,  "direct"),
    ("dcgw3", 5, "10.255.0.10/31", "dcgw4", 5, "10.255.0.11/31", 10,  "direct"),
    ("dcgw1", 6, "10.255.1.0/31",  "p1",    1, "10.255.1.1/31",  100, "core"),
    ("dcgw2", 6, "10.255.1.2/31",  "p1",    2, "10.255.1.3/31",  100, "core"),
    ("dcgw3", 6, "10.255.1.4/31",  "p1",    3, "10.255.1.5/31",  100, "core"),
    ("dcgw4", 6, "10.255.1.6/31",  "p1",    4, "10.255.1.7/31",  100, "core"),
    ("dcgw1", 7, "10.255.1.8/31",  "p2",    1, "10.255.1.9/31",  100, "core"),
    ("dcgw2", 7, "10.255.1.10/31", "p2",    2, "10.255.1.11/31", 100, "core"),
    ("dcgw3", 7, "10.255.1.12/31", "p2",    3, "10.255.1.13/31", 100, "core"),
    ("dcgw4", 7, "10.255.1.14/31", "p2",    4, "10.255.1.15/31", 100, "core"),
    ("p1",    5, "10.255.1.16/31", "p2",    5, "10.255.1.17/31", 100, "core"),
]

ES = {
    "mh-dc1":  (100, "00:00:00:01:01:01", "00:00:00:00:01:01:01:01:01:01", [("leaf1", 3), ("leaf2", 3)]),
    "mh-dc1b": (300, "00:00:00:03:03:03", "00:00:00:00:03:03:03:03:03:03", [("leaf3", 4), ("leaf4", 3)]),
    "mh-dc2":  (200, "00:00:00:02:02:02", "00:00:00:00:02:02:02:02:02:02", [("leaf5", 3), ("leaf6", 3)]),
    "mh-dc2b": (400, "00:00:00:04:04:04", "00:00:00:00:04:04:04:04:04:04", [("leaf7", 4), ("leaf8", 3)]),
}
SH = [("leaf3", 3), ("leaf7", 3)]

DC = {
    "dc1": dict(leaves=["leaf1", "leaf2", "leaf3", "leaf4"], spines=["spine1", "spine2"],
                gws=["dcgw1", "dcgw2"], spine_asn=65501, leaf_asn_start=65401,
                sys_subnet="192.0.2.0/24", anycast="192.0.2.150",
                l3_evi=201, irb="10.200.1.254/24", soo="65000:1", dcgw_asn_start=65001),
    "dc2": dict(leaves=["leaf5", "leaf6", "leaf7", "leaf8"], spines=["spine3", "spine4"],
                gws=["dcgw3", "dcgw4"], spine_asn=65502, leaf_asn_start=65405,
                sys_subnet="192.0.3.0/24", anycast="192.0.3.150",
                l3_evi=202, irb="10.200.2.254/24", soo="65000:2", dcgw_asn_start=65003),
}
GW_ASN = 65000
WAN_DPATH = "65000:100"
MPLS_NODES = ["dcgw1", "dcgw2", "dcgw3", "dcgw4", "p1", "p2"]
GWS = ["dcgw1", "dcgw2", "dcgw3", "dcgw4"]


def ifn(node, port):
    return f"{node}-ethernet-1-{port}"


def write(fname, docs, comment=""):
    txt = HDR
    if comment:
        txt += "\n" + comment.rstrip() + "\n"
    txt += "\n" + "\n---\n".join(d.strip() for d in docs) + "\n"
    (OUT / fname).write_text(txt)
    print(f"wrote {fname} ({len(docs)} CRs)")


# ---------------------------------------------------------------- 00 namespace
write("00-namespace.yaml", [f"""apiVersion: core.eda.nokia.com/v1
kind: Namespace
metadata:
  name: {NS}
  namespace: eda-system
spec:
  description: DCI validated design - two fabrics plus a WAN core in one namespace"""],
"""# One namespace holds BOTH datacenters and the WAN core.
#
# This is deliberate, and it is the single most important structural decision in
# this design. TopoLink.links[].local.node and Interface.members[].node are bare
# TopoNode name references with no namespace field, so a link cannot cross an EDA
# namespace boundary. Four of the six DCGW-to-DCGW mesh links do cross the
# DC1/DC2 boundary (dcgw1-dcgw3, dcgw1-dcgw4, dcgw2-dcgw3, dcgw2-dcgw4), which
# rules out the namespace-per-fabric layout used by the ai-dc designs.
#
# Consequence: resource names carry a -dc1/-dc2 suffix wherever the same service
# exists in both fabrics, because names are only unique within a namespace. The
# on-device name is pinned back to the neutral form via configuredName.
#
# spec.bootstrap.fromNamespace would seed this namespace from EDA's own "eda"
# namespace (NodeGroups, default pools) instead of declaring them by hand as
# 02-nodeuser.yaml and 04-pools.yaml do. This design spells them out so the
# manifest set is self-contained and reviewable.""")

# ------------------------------------------------------------------- 01 init
write("01-init.yaml", [f"""apiVersion: bootstrap.eda.nokia.com/v1
kind: Init
metadata:
  name: init-base
  namespace: {NS}
spec:
  commitSave: true"""],
"""# commitSave makes EDA persist config to startup on the node, so a containerlab
# node that is restarted comes back with its config.
#
# There is deliberately no spec.mgmt block. Containerlab already assigns each
# node a static mgmt address from the eda_mgmt subnet, and that address is how
# EDA reaches the node in the first place - handing mgmt0 to EDA risks cutting
# the very session that configures it. (The DHCP knobs the older 3-stage design
# sets here no longer exist: bootstrap v1 replaced mgmt.ipv4DHCP/ipv6DHCP with
# mgmt.interface.)""")

# --------------------------------------------------------------- 02 nodeuser
write("02-nodeuser.yaml", [f"""apiVersion: aaa.eda.nokia.com/v1
kind: NodeGroup
metadata:
  name: sudo
  namespace: {NS}
spec:
  superuser: true
  services:
    - GNMI
    - CLI
    - NETCONF
    - GNSI
    - GNOI""", f"""apiVersion: core.eda.nokia.com/v1
kind: NodeUser
metadata:
  name: admin
  namespace: {NS}
spec:
  username: admin
  password: NokiaSrl1!
  groupBindings:
    - groups:
        - sudo
      nodeSelector:
        - \"\""""],
"""# The node login EDA uses to reach the switches.
#
# The "sudo" NodeGroup has to be declared here even though EDA ships one: the
# seeded copy lives in EDA's own "eda" namespace, and NodeGroup references do not
# cross namespaces. A design that creates its own namespace starts with none, and
# both the NodeUser and Init resources fail with "Node group sudo not found"
# until it exists.
#
# Containerlab SR Linux images ship with admin/NokiaSrl1!. An empty nodeSelector
# entry matches every node in the namespace.""")

# ------------------------------------------------------------ 03 nodeprofile
write("03-nodeprofile.yaml", [f"""apiVersion: core.eda.nokia.com/v1
kind: NodeProfile
metadata:
  name: {NODE_PROFILE}
  namespace: {NS}
spec:
  operatingSystem: srl
  version: {SRL_VER}
  versionMatch: v26\\.7\\.1.*
  versionPath: .system.information.version
  platformPath: .platform.chassis.type
  containerImage: ghcr.io/nokia/srlinux:{SRL_VER}
  images:
    - image: _base_/srlimages/srlinux-26.7.1-554
      imageMd5: _base_/srlimages/srlinux-26.7.1-554-md5
  nodeUser: admin
  onboardingUsername: admin
  onboardingPassword: NokiaSrl1!
  port: 57410
  annotate: false
  yang: https://eda-asvr.eda-system.svc/_base_/schemaprofiles/srlinux-ghcr-{SRL_VER}/srlinux-{SRL_VER}.zip
  llmDb: https://eda-asvr.eda-system.svc/_base_/llm-dbs/llm-db-srlinux-ghcr-{SRL_VER}/llm-embeddings-srl-26-7-1.tar.gz"""],
f"""# Containerlab flavour of the stock srlinux-ghcr-{SRL_VER} profile.
#
# The stock profile EDA ships targets HARDWARE: gNMI on 57400, a DHCP block, and
# an imagePullSecret. A containerlab node needs gNMI on 57410 and the plain
# ghcr.io/nokia/srlinux:{SRL_VER} tag the topology file actually runs, so the two
# profiles cannot be interchanged.
#
# Everything version-specific is copied verbatim from the stock profile:
#   yang    the schema profile EDA validates the node's YANG models against
#   llmDb   the embeddings behind EDA's natural-language query features
#   images  the .bin artifact. Never downloaded for a container node, but the
#           bootstrap intent indexes into this list unconditionally and dies with
#           an IndexError if it is empty.
#
# annotate:false keeps the rendered device config readable - with it on, every
# config line carries the originating CR as a description.""")

# ------------------------------------------------------------------ 04 pools
pools = []
for site in ("dc1", "dc2"):
    d = DC[site]
    pools.append(f"""apiVersion: core.eda.nokia.com/v1
kind: IndexAllocationPool
metadata:
  name: leaf-asn-{site}
  namespace: {NS}
spec:
  segments:
    - start: {d['leaf_asn_start']}
      size: 8""")
    pools.append(f"""apiVersion: core.eda.nokia.com/v1
kind: IndexAllocationPool
metadata:
  name: spine-asn-{site}
  namespace: {NS}
spec:
  segments:
    - start: {d['spine_asn']}
      size: 1""")
    pools.append(f"""apiVersion: core.eda.nokia.com/v1
kind: IndexAllocationPool
metadata:
  name: dcgw-asn-{site}
  namespace: {NS}
spec:
  segments:
    - start: {d['dcgw_asn_start']}
      size: 2""")
    pools.append(f"""apiVersion: core.eda.nokia.com/v1
kind: IPAllocationPool
metadata:
  name: systemipv4-{site}
  namespace: {NS}
spec:
  segments:
    - subnet: {d['sys_subnet']}""")

DEFAULT_POOLS = {"es-index-pool": (1, 16777216), "evi-pool": (100, 4000),
                 "irb-subif-pool": (0, 4000), "lag-admin-key-pool": (1, 65535),
                 "lagid-pool": (1, 128), "loopback-id-pool": (0, 255),
                 "node-sid-index-pool": (1, 1000), "tunnel-index-pool": (1, 16777215),
                 "vlan-pool": (1, 4000), "vni-pool": (200, 4000)}
for name, (start, size) in DEFAULT_POOLS.items():
    pools.append(f"""apiVersion: core.eda.nokia.com/v1
kind: IndexAllocationPool
metadata:
  name: {name}
  namespace: {NS}
spec:
  segments:
    - start: {start}
      size: {size}""")

write("04-pools.yaml", pools,
"""# Allocation pools: one set per fabric, plus the EDA well-known defaults.
#
# Nothing here is pinned to a particular node, and nothing downstream depends on
# a specific value. That is a deliberate correction: IndexAllocationPool
# segments[].allocations look like they pin a value to a node, but the Fabric
# does not request its ASNs under the bare TopoNode name, so a pinned entry only
# ever RESERVES the value away from general allocation. Pinning leaf1..leaf4 to
# 65401..65404 in a size-4 segment simply exhausts the pool. Everywhere a peer
# address would otherwise have to be written down, the manifests reference the
# peer's SystemInterface resource by name instead (see 34-wan-ibgp.yaml).
#
# The spine pools are size 1 because every spine in a fabric shares one ASN. The
# DCGW pools are size 2 because a Fabric insists on a distinct ASN per border
# leaf - see the note in 34-wan-ibgp.yaml for how the WAN core is still iBGP
# despite that.
#
# The trailing well-known pools (evi-pool, vni-pool, tunnel-index-pool, ...) are
# seeded by EDA into its OWN namespace at install time. A design that creates its
# own namespace inherits none of them, and the first BridgeDomain or LAG fails
# with "pool template does not exist in namespace dci". Ranges mirror the install
# defaults.""")

# --------------------------------------------------------------- 05 toponodes
tn = []
for name, (plat, mgmt, role, site, _s) in NODES.items():
    tn.append(f"""apiVersion: core.eda.nokia.com/v1
kind: TopoNode
metadata:
  name: {name}
  namespace: {NS}
  labels:
    eda.nokia.com/role: {role}
    eda.nokia.com/site: {site}
    eda.nokia.com/name: {name}
    eda.nokia.com/security-profile: managed
spec:
  platform: {plat}
  version: {SRL_VER}
  operatingSystem: srl
  nodeProfile: {NODE_PROFILE}
  onBoarded: false
  productionAddress:
    ipv4: {mgmt}/24
    ipv6: ""
  npp:
    mode: normal""")
write("05-toponodes.yaml", tn,
"""# 18 switches: 8 leaves, 4 spines, 4 DCGWs, 2 WAN P routers.
#
# The role/site label pair is what every downstream selector keys on. role
# separates leaf / spine / dcgw / pe; site separates dc1 / dc2 / wan. The two
# Fabric CRs select on BOTH, which is how one namespace holds two independent
# fabrics without their selectors overlapping.
#
# onBoarded stays false because the field is EDA-owned, not ours: the engine
# rejects a user-supplied true ("cannot create toponode with onBoarded as true")
# and sets it itself once its own onboarding transaction commits. Containerlab
# has already given every node its mgmt address, so that transaction is the only
# thing standing between NotOnBoarded and Synced - there is no ZTP to wait on.
#
# EDA batches every not-yet-onboarded node in the namespace into ONE onboarding
# transaction, and applying the EDA TLS profile restarts the node's gRPC server
# mid-commit. Past roughly half a dozen nodes the reconnect lands after the
# commit-confirm window and the whole batch reverts together, so bring nodes up
# in small groups rather than all 18 at once (see deploy-dci-nvd.sh).
#
# productionAddress.ipv4 carries a /24 prefix, not a bare address: the bootstrap
# intent writes this value straight into mgmt0's ip-prefix leaf, and SR Linux
# rejects a bare address there. EDA strips the prefix for the gNMI endpoint
# itself (the node reports as 172.21.21.x:57410), so the two uses do not
# conflict.""")

# ------------------------------------------------------------- 10 interfaces
def iface_doc(node, port, role, labels=None, encap=None, desc=None, aggregate=None):
    lab = [f"    eda.nokia.com/role: {role}"] + [f"    {k}: {v}" for k, v in (labels or {}).items()]
    member = [f"    - node: {node}", f"      interface: ethernet-1-{port}", "      enabled: true"]
    if aggregate:
        member.append(f"      aggregateID: {aggregate}")
    spec = [f"  description: {desc}", "  enabled: true", "  lldp: true", "  type: Interface"]
    if encap:
        spec.append(f"  encapType: {encap}")
    spec.append("  members:")
    return (f"apiVersion: interfaces.eda.nokia.com/v1\nkind: Interface\nmetadata:\n"
            f"  name: {ifn(node, port)}\n  namespace: {NS}\n  labels:\n" + "\n".join(lab) +
            "\nspec:\n" + "\n".join(spec) + "\n" + "\n".join(member))


iface, seen = [], set()
for a, ap, b, bp in ISLS:
    for node, port in ((a, ap), (b, bp)):
        if (node, port) not in seen:
            seen.add((node, port))
            iface.append(iface_doc(node, port, "interSwitch", desc=f"isl-{node}-ethernet-1-{port}"))
for a, ap, b, bp in UPLINKS:
    for node, port, peer in ((a, ap, b), (b, bp, a)):
        if (node, port) not in seen:
            seen.add((node, port))
            iface.append(iface_doc(node, port, "dcgwUplink", desc=f"{node}-to-{peer}"))
for a, ap, _x, b, bp, _y, _m, kind in WAN:
    for node, port, peer in ((a, ap, b), (b, bp, a)):
        if (node, port) not in seen:
            seen.add((node, port))
            iface.append(iface_doc(node, port, "wan", labels={"eda.nokia.com/wan-path": kind},
                                   desc=f"{node}-to-{peer}-{kind}"))
lags = []
LAG_PORT = {}  # (node, port) -> LAG interface resource name
for es_name, (adminkey, sysmac, esi, members) in ES.items():
    member_docs = []
    for node, port in members:
        LAG_PORT[(node, port)] = es_name
        member_docs.append(f"    - node: {node}\n      interface: ethernet-1-{port}\n"
                           f"      enabled: true")
    site = NODES[members[0][0]][3]
    lags.append(f"""apiVersion: interfaces.eda.nokia.com/v1
kind: Interface
metadata:
  name: {es_name}
  namespace: {NS}
  labels:
    eda.nokia.com/role: edge
    eda.nokia.com/es: {es_name}
    eda.nokia.com/site: {site}
spec:
  description: {es_name}
  enabled: true
  lldp: true
  type: LAG
  encapType: Dot1q
  mtu: 9232
  members:
""" + "\n".join(member_docs) + f"""
  lag:
    type: LACP
    minLinks: 1
    lacp:
      mode: Active
      interval: Fast
      adminKey: {adminkey}
      systemMAC: {sysmac}
      systemPriority: 100
    multihoming:
      mode: AllActive
      dfElection: Default
      esi: {esi}""")
for node, port in SH:
    iface.append(iface_doc(node, port, "edge", encap="Dot1q",
                           labels={"eda.nokia.com/site": NODES[node][3]},
                           desc=f"sh-{NODES[node][3]}-single-homed"))

write("10-interfaces.yaml", iface,
"""# Physical port inventory. One Interface CR per port that carries traffic.
#
# The role label drives selection downstream:
#   interSwitch - fabric ISLs, picked up by the Fabric interSwitchLinks selector
#   wan         - the DCI mesh and the links to the P routers. Deliberately NOT
#                 interSwitch: these are numbered IS-IS/LDP links in the default
#                 network-instance, not eBGP-unnumbered fabric ISLs, and a Fabric
#                 that selected them would try to run its underlay over them.
#   edge        - single-homed access ports, dot1q tagged
#
# The wan-path label (direct|core) records which DCI plane a port belongs to; the
# IS-IS metric CRs key on the same distinction.
#
# The eight LAG member ports are absent from this file by design. A port that
# belongs to an aggregate is declared inside the LAG's members list (see
# 11-lags.yaml), not as an Interface of its own - EDA rejects an Interface of
# type Interface that carries an aggregateID.""")

write("11-lags.yaml", lags,
"""# LAG aggregates carrying the four all-active Ethernet Segments.
#
# Each ES spans two leaves. Both members of a pair advertise the SAME esi and the
# SAME LACP systemMAC, which is what makes the client's single 802.3ad bond look
# like one multi-homed attachment. AllActive lets both leaves forward at once.
#
# Note leaf3 and leaf7 carry both a LAG (for the mh-*b client, on ethernet-1/4)
# and a plain single-homed access port (ethernet-1/3), which is why the ES members
# are not simply "port 3 on every leaf".
#
# One Interface CR per ES, with a member on EACH leaf - not one CR per leaf.
# This is what actually creates the ethernet segment: EDA reads the members list
# as "the ports that make up this segment", so a CR listing a single node is a
# plain LAG. Split per leaf, the aggregates and their VLAN subinterfaces still
# render correctly and the config looks right, but no ethernet-segment appears
# under system network-instance protocols evpn, and the multihoming block is
# silently ignored.""")

# ---------------------------------------------------------------- 12 topolinks
tl = []
for a, ap, b, bp in ISLS:
    tl.append(f"""apiVersion: core.eda.nokia.com/v1
kind: TopoLink
metadata:
  name: {a}-{b}
  namespace: {NS}
  labels:
    eda.nokia.com/role: interSwitch
    eda.nokia.com/site: {NODES[a][3]}
spec:
  links:
    - type: interSwitch
      local:
        node: {a}
        interface: ethernet-1-{ap}
        interfaceResource: {ifn(a, ap)}
      remote:
        node: {b}
        interface: ethernet-1-{bp}
        interfaceResource: {ifn(b, bp)}""")
for a, ap, b, bp in UPLINKS:
    tl.append(f"""apiVersion: core.eda.nokia.com/v1
kind: TopoLink
metadata:
  name: {a}-{b}
  namespace: {NS}
  labels:
    eda.nokia.com/role: dcgwUplink
    eda.nokia.com/site: {NODES[a][3]}
spec:
  links:
    - type: interSwitch
      local:
        node: {a}
        interface: ethernet-1-{ap}
        interfaceResource: {ifn(a, ap)}
      remote:
        node: {b}
        interface: ethernet-1-{bp}
        interfaceResource: {ifn(b, bp)}""")
for a, ap, _x, b, bp, _y, _m, kind in WAN:
    tl.append(f"""apiVersion: core.eda.nokia.com/v1
kind: TopoLink
metadata:
  name: {a}-{b}-{kind}
  namespace: {NS}
  labels:
    eda.nokia.com/role: wan
    eda.nokia.com/wan-path: {kind}
spec:
  links:
    - type: interSwitch
      local:
        node: {a}
        interface: ethernet-1-{ap}
        interfaceResource: {ifn(a, ap)}
      remote:
        node: {b}
        interface: ethernet-1-{bp}
        interfaceResource: {ifn(b, bp)}""")
for node, port, client in [("leaf1", 3, "mh-dc1"), ("leaf2", 3, "mh-dc1"), ("leaf3", 3, "sh-dc1"),
                           ("leaf3", 4, "mh-dc1b"), ("leaf4", 3, "mh-dc1b"),
                           ("leaf5", 3, "mh-dc2"), ("leaf6", 3, "mh-dc2"), ("leaf7", 3, "sh-dc2"),
                           ("leaf7", 4, "mh-dc2b"), ("leaf8", 3, "mh-dc2b")]:
    # A LAG member port has no Interface CR of its own - it lives in the LAG's
    # members list - so the link points at the LAG resource instead.
    resource = LAG_PORT.get((node, port), ifn(node, port))
    tl.append(f"""apiVersion: core.eda.nokia.com/v1
kind: TopoLink
metadata:
  name: {node}-{client}
  namespace: {NS}
  labels:
    eda.nokia.com/role: edge
    eda.nokia.com/site: {NODES[node][3]}
spec:
  links:
    - type: edge
      local:
        node: {node}
        interface: ethernet-1-{port}
        interfaceResource: {resource}""")

write("12-topolinks.yaml", tl,
"""# Topology adjacencies.
#
# role=interSwitch links are the only ones the Fabric CRs consume. role=wan links
# describe the DCI mesh and the P-router core so EDA's topology view and health
# checks know about them, but they are configured by the WAN underlay CRs
# (30-* through 34-*), not by a Fabric.
#
# role=edge links have only a local endpoint - the far side is a Linux client
# container, which is not an EDA-managed node.""")

# ------------------------------------------------------------------ 20 fabrics
fab = []
for site in ("dc1", "dc2"):
    fab.append(f"""apiVersion: fabrics.eda.nokia.com/v1
kind: Fabric
metadata:
  name: {site}
  namespace: {NS}
spec:
  systemPoolIPv4: systemipv4-{site}
  leafs:
    leafNodeSelectors:
      - eda.nokia.com/role=leaf,eda.nokia.com/site={site}
    asnPool: leaf-asn-{site}
    systemPoolIPv4: systemipv4-{site}
  spines:
    spineNodeSelectors:
      - eda.nokia.com/role=spine,eda.nokia.com/site={site}
    asnPool: spine-asn-{site}
    systemPoolIPv4: systemipv4-{site}
  borderLeafs:
    borderLeafNodeSelectors:
      - eda.nokia.com/role=dcgw,eda.nokia.com/site={site}
    asnPool: dcgw-asn-{site}
    systemPoolIPv4: systemipv4-{site}
  interSwitchLinks:
    unnumbered: IPv6
    ipMTU: 9198
    linkSelectors:
      - eda.nokia.com/role=interSwitch,eda.nokia.com/site={site}
      - eda.nokia.com/role=dcgwUplink,eda.nokia.com/site={site}
  underlayProtocol:
    protocols:
      - EBGP
    bfd:
      enabled: true
      detectionMultiplier: 3
      desiredMinTransmitIntMs: 1000
      requiredMinReceiveIntMs: 1000
  overlayProtocol:
    protocol: EBGP""")

write("20-fabrics.yaml", fab,
"""# One Fabric per datacenter, both in the same namespace.
#
# Each selector is ONE comma-joined string, not a list of strings: EDA ORs the
# entries of a selector list, so ["role=leaf", "site=dc1"] would match every leaf
# AND every dc1 node, and the fabrics would fight over each other's spines. The
# comma inside a single entry is the AND.
#
# Underlay is eBGP over IPv6-unnumbered ISLs and the overlay is eBGP, matching
# the reference design.
#
# The DCGWs are deliberately NOT borderLeafs here. A Fabric allocates one ASN per
# border leaf from borderLeafs.asnPool and refuses to hand the same value to two
# nodes ("allocation dcgw2=65000 is already used by dcgw1"), but this design needs
# all four DCGWs in a single AS 65000 so the WAN core between them can be iBGP -
# which in turn is what lets the anycast loopback and the shared RDs work. So the
# DCGWs are configured explicitly instead, in 21-dcgw-uplinks.yaml.""")

# --------------------------------------------------------- 30 wan label blocks
lb = []
BLOCKS = [("dyn-ldp", "Dynamic", 20000, 29999), ("dyn-svc", "Dynamic", 30000, 39999),
          ("dyn-evpn", "Dynamic", 40000, 49999), ("dyn-mcast", "Dynamic", 50000, 59999),
          ("sr-global", "Static", 15000, 15999), ("sr-adj", "Static", 16000, 16999)]
for name, typ, start, end in BLOCKS:
    lb.append(f"""apiVersion: mpls.eda.nokia.com/v1alpha1
kind: LabelBlock
metadata:
  name: {name}
  namespace: {NS}
spec:
  type: {typ}
  startLabel: {start}
  endLabel: {end}""")
for name, typ, _s, _e in BLOCKS:
    for node in MPLS_NODES:
        if node in ("p1", "p2") and name in ("dyn-svc", "dyn-evpn", "dyn-mcast"):
            continue
        extra = "\n  type: SRGB" if name == "sr-global" else ""
        lb.append(f"""apiVersion: mpls.eda.nokia.com/v1alpha1
kind: LabelBlockDeployment
metadata:
  name: {name}-{node}
  namespace: {NS}
spec:
  labelBlock: {name}
  node: {node}{extra}""")

write("30-wan-labelblocks.yaml", lb,
"""# MPLS label ranges for the DCI plane, and the per-node deployments that push
# them.
#
# LabelBlock only declares a range; LabelBlockDeployment binds it to a node. The
# dynamic blocks feed LDP (dyn-ldp), the VPN services (dyn-svc), EVPN (dyn-evpn)
# and EVPN inclusive-multicast (dyn-mcast). The two static blocks back segment
# routing: sr-global is the shared SRGB that node SIDs index into - hence
# type:SRGB on its deployments - and sr-adj is the non-shared adjacency-SID block.
#
# sr-adj is reserved rather than used. It lands in system mpls label-ranges, but
# nothing binds it to the ISIS instance: DefaultISISInstance's segmentRouting has
# srgbLabelBlock and nodeSIDIndexPool and no adjacency equivalent, so EDA leaves
# instance-level segment-routing an empty container and adjacency SIDs come from
# the dynamic range. It is kept because the range is what the reference design
# reserves, and because reserving it costs nothing.
#
# p1/p2 are pure LSRs: they switch labels but terminate no service, so they get
# only the LDP and SR blocks.""")

# ------------------------------------------------- 31 wan routing + interfaces
wr = []
for node in ("p1", "p2"):
    rid = NODES[node][4]
    wr.append(f"""apiVersion: routing.eda.nokia.com/v1
kind: DefaultRouter
metadata:
  name: router-{node}
  namespace: {NS}
spec:
  node: {node}
  routerID: {rid}
  description: wan-core role P/LSR
  ecmp: 8
  bgp:
    enabled: false""")
    wr.append(f"""apiVersion: routing.eda.nokia.com/v1
kind: SystemInterface
metadata:
  name: {node}-system0
  namespace: {NS}
spec:
  defaultRouter: router-{node}
  ipv4Address: {rid}/32
  description: {node} system0 loopback""")
for site in ("dc1", "dc2"):
    for node in DC[site]["gws"]:
        wr.append(f"""apiVersion: routing.eda.nokia.com/v1
kind: DefaultLoopbackInterface
metadata:
  name: {node}-lo1
  namespace: {NS}
spec:
  node: {node}
  interfaceIndex: 1
  subinterfaceIndex: 0
  description: anycast DCI loopback shared by the {site} DCGW pair
  ipv4Addresses:
    - {DC[site]['anycast']}/32""")
for a, ap, aip, b, bp, bip, _m, kind in WAN:
    for node, port, ip, peer in ((a, ap, aip, b), (b, bp, bip, a)):
        wr.append(f"""apiVersion: routing.eda.nokia.com/v1
kind: DefaultInterface
metadata:
  name: {node}-ethernet-1-{port}-wan
  namespace: {NS}
spec:
  defaultRouter: router-{node}
  interface: {ifn(node, port)}
  subinterfaceIndex: 0
  description: {node}-to-{peer}-{kind}
  ipMTU: 9198
  ipv4Addresses:
    - ipPrefix: {ip}
      primary: true""")

write("31-wan-interfaces.yaml", wr,
"""# The numbered side of the WAN: /31 links, the anycast DCI loopback, and the two
# P routers' default network-instance.
#
# p1/p2 belong to no Fabric, so nothing creates their default network-instance or
# system0 for them - hence the explicit DefaultRouter (with BGP off; they are
# label switches, not PEs) and SystemInterface. The DCGWs get theirs from their
# Fabric, and this file only adds their WAN-facing interfaces on top - which is
# why every DefaultInterface here references <node>-default, a router the Fabric
# created for the DCGWs and this file created for the P routers.
#
# lo1 is the anycast loopback: BOTH DCGWs in a pair carry the same address
# (192.0.2.150 in dc1, 192.0.3.150 in dc2). It is the next hop the far DC resolves
# for the stretched L2 services, so either gateway can answer and losing one does
# not withdraw the route. It is also the RD administrator field on the
# BridgeDomainInterconnects.""")

# --------------------------------------------------------------- 32 wan isis
isis = [f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultISISInstance
metadata:
  name: wan-isis
  namespace: {NS}
spec:
  enabled: true
  levelCapability: L2
  areaIDs:
    - "49.0001"
  addressFamilies:
    - IPv4
  maxECMP: 8
  lspMTU: 1492
  segmentRouting:
    mpls:
      enabled: true
      srgbLabelBlock: sr-global
      nodeSIDIndexPool: node-sid-index-pool"""]
for node in MPLS_NODES:
    isis.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultISISInstanceDeployment
metadata:
  name: wan-isis-{node}
  namespace: {NS}
spec:
  defaultISISInstance: wan-isis
  defaultRouter: router-{node}
  node: {node}""")
for node in MPLS_NODES:
    isis.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultISISInterface
metadata:
  name: wan-isis-{node}-system0
  namespace: {NS}
spec:
  defaultISISInstance: wan-isis
  interface: {node}-system0
  interfaceKind: SystemInterface
  passive: true
  level: L2""")
for site in ("dc1", "dc2"):
    for node in DC[site]["gws"]:
        isis.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultISISInterface
metadata:
  name: wan-isis-{node}-lo1
  namespace: {NS}
spec:
  defaultISISInstance: wan-isis
  interface: {node}-lo1
  interfaceKind: DefaultLoopbackInterface
  passive: true
  level: L2""")
for a, ap, _x, b, bp, _y, metric, kind in WAN:
    for node, port in ((a, ap), (b, bp)):
        isis.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultISISInterface
metadata:
  name: wan-isis-{node}-ethernet-1-{port}
  namespace: {NS}
spec:
  defaultISISInstance: wan-isis
  interface: {node}-ethernet-1-{port}-wan
  interfaceKind: DefaultInterface
  type: PointToPoint
  level: L2
  level2:
    metric: {metric}""")

write("32-wan-isis.yaml", isis,
"""# IS-IS L2 as the WAN IGP, with segment routing enabled.
#
# The metric is the whole traffic-engineering story of this lab: 10 on the direct
# DCGW-to-DCGW mesh and 100 on the links through p1/p2. Both paths are always up,
# so steady-state DCI traffic takes the direct mesh and fails over to the P core
# without any protocol change - which is what makes the WAN-failover tests
# meaningful.
#
# system0 and lo1 are passive: advertised, but forming no adjacency. system0
# carries the SR node SID (indexed into the sr-global SRGB), giving every DCGW and
# P router an SR-MPLS transport tunnel alongside LDP.""")

# ----------------------------------------------------------------- 33 wan ldp
ldp = [f"""apiVersion: mpls.eda.nokia.com/v1alpha1
kind: DefaultLDPRouter
metadata:
  name: wan-ldp
  namespace: {NS}
spec:
  enabled: true
  labelBlockRef: dyn-ldp
  longestPrefixMatch: false
  maxECMP: 8
  linkAdjacency:
    helloIntervalSeconds: 5
    helloHoldTimeSeconds: 15
  keepAlive:
    keepAliveIntervalSeconds: 30
    keepAliveHoldTimeSeconds: 90"""]
for node in MPLS_NODES:
    ldp.append(f"""apiVersion: mpls.eda.nokia.com/v1alpha1
kind: DefaultLDPRouterDeployment
metadata:
  name: wan-ldp-{node}
  namespace: {NS}
spec:
  defaultLDPRouter: wan-ldp
  node: {node}""")
for a, ap, _x, b, bp, _y, _m, _k in WAN:
    for node, port in ((a, ap), (b, bp)):
        ldp.append(f"""apiVersion: mpls.eda.nokia.com/v1alpha1
kind: DefaultLDPInterface
metadata:
  name: wan-ldp-{node}-ethernet-1-{port}
  namespace: {NS}
spec:
  defaultLDPRouter: wan-ldp
  interface: {node}-ethernet-1-{port}-wan
  ipv4:
    enabled: true""")

write("33-wan-ldp.yaml", ldp,
"""# LDP over every WAN link, drawing labels from the dyn-ldp block.
#
# LDP and SR-ISIS run in parallel. Both are listed in allowedTunnelTypes on the
# interconnect CRs, so a service can resolve its next hop over either and killing
# one transport leaves the other carrying traffic.
#
# linkAdjacency deliberately omits transportAddress: EDA rejects it for SR Linux
# ("Link adjancency transport address is not supported on srl platform"). SR
# Linux derives the LDP transport address from the router-id, which is system0
# here, so the effect the knob would have had is already the default.
#
# The keepalive interval is 30s, not the 10s that would mirror the hello timers:
# SR Linux constrains session-keepalive-interval to 15..1200.""")

# ------------------------------------------------------------------ 40 policies
pol = [f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: PrefixSet
metadata:
  name: host-routes-l3dci
  namespace: {NS}
spec:
  prefixes:
    - prefix: 10.200.0.0/16
      startRange: 32
      endRange: 32"""]
for site in ("dc1", "dc2"):
    pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: CommunitySet
metadata:
  name: soo-{site}
  namespace: {NS}
spec:
  type: Extended
  members:
    - origin:{DC[site]['soo']}""")
pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: CommunitySet
metadata:
  name: rt-wan-l2dci-b
  namespace: {NS}
spec:
  type: Extended
  members:
    - target:{GW_ASN}:110""")
pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: CommunitySet
metadata:
  name: rt-wan-l2dci-a
  namespace: {NS}
spec:
  type: Extended
  members:
    - target:{GW_ASN}:100""")
pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: CommunitySet
metadata:
  name: rt-wan-l3dci
  namespace: {NS}
spec:
  type: Extended
  members:
    - target:{GW_ASN}:3000""")
for bd, rt in (("a", "100:100"), ("b", "110:110")):
    pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: CommunitySet
metadata:
  name: rt-dc-l2dci-{bd}
  namespace: {NS}
spec:
  type: Extended
  members:
    - target:{rt}""")
# One policy per gateway, each de-preferring the bridge domain the OTHER gateway
# of the pair is primary for. Chained AHEAD of the fabric's own ISL export policy
# by a Configlet, so the result is NextPolicy: prepend, then let the fabric
# policy make the accept decision it would have made anyway.
for site, d in DC.items():
    for gw, other_bd in zip(d["gws"], ("b", "a")):
        pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: Policy
metadata:
  name: dcgw-dc-pin-{gw}
  namespace: {NS}
spec:
  defaultAction:
    policyResult: NextPolicy
  statements:
    - name: "10-depref-bd-{other_bd}-toward-fabric"
      match:
        families:
          - EVPN
        bgp:
          communitySet: rt-dc-l2dci-{other_bd}
      action:
        policyResult: NextPolicy
        bgp:
          asPathPrepend:
            asn: "{GW_ASN}"
            count: 2""")
        # A Policy on its own never reaches a node - EDA pushes it only when
        # something it manages references it, and a Configlet reference does not
        # count. Without this the Configlet fails validation on a leafref to a
        # policy that exists in the cluster but not on the device.
        pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: PolicyDeployment
metadata:
  name: policy-dcgw-dc-pin-{gw}
  namespace: {NS}
spec:
  policy: dcgw-dc-pin-{gw}
  node: {gw}""")
for site in ("dc1", "dc2"):
    pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: Policy
metadata:
  name: dcgw-wan-export-{site}
  namespace: {NS}
spec:
  defaultAction:
    policyResult: Reject
  statements:
    - name: "08-drop-tenant-host-routes"
      match:
        prefixSet: host-routes-l3dci
      action:
        policyResult: Reject
    - name: "15-depref-bd-b"
      match:
        families:
          - EVPN
        bgp:
          communitySet: rt-wan-l2dci-b
      action:
        policyResult: Accept
        bgp:
          setLocalPreference: 90
          communities:
            addSets:
              - soo-{site}
    - name: "18-accept-bd-a"
      match:
        families:
          - EVPN
        bgp:
          communitySet: rt-wan-l2dci-a
      action:
        policyResult: Accept
        bgp:
          communities:
            addSets:
              - soo-{site}
    - name: "20-accept-l3vpn"
      match:
        bgp:
          communitySet: rt-wan-l3dci
      action:
        policyResult: Accept
        bgp:
          communities:
            addSets:
              - soo-{site}""")
    pol.append(f"""apiVersion: routingpolicies.eda.nokia.com/v1
kind: Policy
metadata:
  name: dcgw-wan-import-{site}
  namespace: {NS}
spec:
  defaultAction:
    policyResult: Reject
  statements:
    - name: "10-reject-own-soo"
      match:
        bgp:
          communitySet: soo-{site}
      action:
        policyResult: Reject
    - name: "20-accept-evpn"
      match:
        families:
          - EVPN
      action:
        policyResult: Accept
    - name: "30-accept-the-rest"
      action:
        policyResult: Accept""")

write("24-policies.yaml", pol,
"""# Loop prevention, host-route scoping and per-service gateway pinning.
#
# None of these three has a field on the interconnect resources, so each is
# expressed as a Policy CR and referenced from the WAN BGP group.
#
# 1. Site-of-origin loop prevention. Every route a DC exports to the WAN is
#    stamped with origin:65000:<dc>, and each DC rejects its own stamp on import.
#    Without it, a DC's peer gateway re-imports that DC's own re-originated routes
#    back off the WAN. This is the L2 equivalent of the D-PATH domain IDs the
#    RouterInterconnect handles natively for L3.
#
# 2. Host-route scoping. The DCGWs install /32 ARP host routes from the local
#    fabric so traffic to a local host is not tromboned, but re-originating those
#    /32s across the WAN would leak the whole host table into the other DC.
#    Statement 08 drops anything matching a /32 out of 10.200.0.0/16, so only the
#    /24 subnet route crosses. Note the match is on prefix alone: the v1 policy
#    families enum is IPv4/IPv6/EVPN/RTC and has no VPN-IPv4 member, so the
#    family cannot be named the way the hand-built config names it.
#
# 3. Per-service gateway pinning. BD-B's WAN route target is de-preferred to
#    local-pref 90 so BD-B settles on dcgw2/dcgw4 while BD-A rides dcgw1/dcgw3.
#    The de-preferred path stays present as a hot standby rather than being
#    filtered out, so losing a gateway is a reconvergence and not an outage.
#    Local-pref rather than AS-path prepend because the WAN core is iBGP - a
#    prepend of our own AS would look like a loop to the peer DCGW, which would
#    drop the standby path entirely.
#
# 4. Keeping the fabric out of the WAN. The DCGW's WAN iBGP sessions and its
#    fabric eBGP sessions live in the same network-instance and the same EVPN
#    address family, so by default every EVPN route the DCGW learns from its
#    spines is re-advertised straight to the far DC - original RD, original
#    route target, original VXLAN next hop - and the far DC installs leaf VTEP
#    addresses it has no tunnel to. The stitch still looks healthy: sessions
#    come up, prefix counters move, and only the MAC tables give it away.
#
#    The hand-built config catches this with internal-tags: the VXLAN-side BGP
#    instance stamps tag-1 and the WAN export rejects tag-1. Internal tags are
#    not on any of the service CRs, so the export policy discriminates on the
#    route target instead, which separates the two sides just as cleanly - the
#    DC instances carry target:<evi>:<evi> and the interconnect instances
#    target:65000:*. Hence statements 15/18/20 accept exactly the three WAN
#    targets over a default-action of reject, rather than accepting everything.
#    Ethernet-segment routes fall to the default and are rejected, which is
#    correct here: the ES are DC-local and must not cross the DCI.""")

# ---------------------------------------------------------------- 34 wan ibgp
ibgp = []
for site in ("dc1", "dc2"):
    ibgp.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultBGPGroup
metadata:
  name: wan-ibgp-{site}
  namespace: {NS}
spec:
  configuredName: wan-ibgp
  description: DCI iBGP core between all four DCGWs
  peerAS:
    autonomousSystem: {GW_ASN}
  localAS:
    autonomousSystem: {GW_ASN}
  nextHopSelf: false
  exportPolicies:
    - dcgw-wan-export-{site}
  importPolicies:
    - dcgw-wan-import-{site}
  l2VPNEVPN:
    enabled: true
  vpnIPv4Unicast:
    enabled: true
  ipv4Unicast:
    enabled: false
  ipv6Unicast:
    enabled: false""")
for site in ("dc1", "dc2"):
    for node in DC[site]["gws"]:
        ibgp.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultBGPGroupDeployment
metadata:
  name: wan-ibgp-{node}
  namespace: {NS}
spec:
  defaultBGPGroup: wan-ibgp-{site}
  node: {node}""")
for site in ("dc1", "dc2"):
    for node in DC[site]["gws"]:
        for peer in GWS:
            if peer == node:
                continue
            ibgp.append(f"""apiVersion: protocols.eda.nokia.com/v2
kind: DefaultBGPPeer
metadata:
  name: wan-ibgp-{node}-to-{peer}
  namespace: {NS}
spec:
  group: wan-ibgp-{site}
  interface: {node}-system0
  interfaceKind: SystemInterface
  peerInterface: {peer}-system0
  peerInterfaceKind: SystemInterface
  description: DCI iBGP to {peer}
  peerAS:
    autonomousSystem: {GW_ASN}
  localAS:
    autonomousSystem: {GW_ASN}
  l2VPNEVPN:
    enabled: true
  vpnIPv4Unicast:
    enabled: true
  ipv4Unicast:
    enabled: false
  ipv6Unicast:
    enabled: false""")

write("34-wan-ibgp.yaml", ibgp,
"""# The DCI control plane: a 4-node iBGP full mesh between all DCGWs in AS 65000,
# carrying EVPN (for the stretched L2 services) and VPN-IPv4 (for the L3 service).
#
# Sessions are sourced from system0 and peer to the other DCGWs' system0, so they
# ride whichever WAN path IS-IS currently prefers rather than being pinned to a
# link. There is one group per site because the import/export policies differ per
# DC - each stamps its own site-of-origin - while the AS number and address
# families are identical.
#
# 12 peer CRs = 4 gateways x 3 remote gateways.""")

# ------------------------------------------------------- 50/51 services per DC
for site in ("dc1", "dc2"):
    d = DC[site]
    docs = []
    for bd, vlan, evi, rt in (("l2dci", 100, 100, "100:100"), ("l2dci-b", 110, 110, "110:110")):
        docs.append(f"""apiVersion: services.eda.nokia.com/v2
kind: BridgeDomain
metadata:
  name: macvrf-{bd}-{site}
  namespace: {NS}
spec:
  configuredName: macvrf-{bd}
  description: L2 DCI stretched bridge-domain vlan{vlan} ({site})
  type: EVPNVXLAN
  evi: {evi}
  exportTarget: target:{rt}
  importTarget: target:{rt}
  encapOptions:
    vxlan:
      vni: {vlan}
      tunnelIndexPool: tunnel-index-pool
  macLearning:
    enabled: true
    agingTimeSeconds: 300
  macDuplicationDetection:
    enabled: true
    action: StopLearning
    holdDownTimeMinutes: 9
    monitoringWindowMinutes: 3
    numMoves: 5""")
    docs.append(f"""apiVersion: services.eda.nokia.com/v2
kind: BridgeDomain
metadata:
  name: macvrf-l3dci-{site}
  namespace: {NS}
spec:
  configuredName: macvrf-l3dci
  description: L3 DCI access bridge-domain vlan200 ({site}) - local only
  type: EVPNVXLAN
  evi: {d['l3_evi']}
  exportTarget: target:{d['l3_evi']}:{d['l3_evi']}
  importTarget: target:{d['l3_evi']}:{d['l3_evi']}
  encapOptions:
    vxlan:
      vni: {d['l3_evi']}
      tunnelIndexPool: tunnel-index-pool
  macLearning:
    enabled: true
    agingTimeSeconds: 300""")
    docs.append(f"""apiVersion: services.eda.nokia.com/v2
kind: Router
metadata:
  name: ipvrf-l3dci-{site}
  namespace: {NS}
spec:
  configuredName: ipvrf-l3dci
  description: L3 DCI ip-vrf ({site}) - IFL type-5 vxlan to ip-vpn stitch
  type: EVPNVXLAN
  evi: 3000
  exportTarget: target:3000:3000
  importTarget: target:3000:3000
  ecmp: 8
  encapOptions:
    vxlan:
      vni: 3000
      tunnelIndexPool: tunnel-index-pool
  bgp:
    enabled: false""")
    docs.append(f"""apiVersion: services.eda.nokia.com/v2
kind: IRBInterface
metadata:
  name: irb-l3dci-{site}
  namespace: {NS}
spec:
  bridgeDomain: macvrf-l3dci-{site}
  router: ipvrf-l3dci-{site}
  description: anycast gateway for the L3 DCI tenant subnet in {site}
  ipMTU: 1500
  ipAddresses:
    - ipv4Address:
        ipPrefix: {d['irb']}
        anycast: true
        primary: true
  ipv4:
    arpTimeoutSeconds: 250
  l3ProxyARPND:
    proxyARP: true
  evpnRouteAdvertisementType:
    arpDynamic: true
    rfc9135SymmetricMode: true""")
    for bd, vlan in (("l2dci", 100), ("l2dci-b", 110), ("l3dci", 200)):
        docs.append(f"""apiVersion: services.eda.nokia.com/v2
kind: VLAN
metadata:
  name: vlan{vlan}-{site}
  namespace: {NS}
spec:
  bridgeDomain: macvrf-{bd}-{site}
  vlanID: "{vlan}"
  interfaceSelectors:
    - eda.nokia.com/role=edge,eda.nokia.com/site={site}""")
    write({"dc1": "50", "dc2": "51"}[site] + f"-services-{site}.yaml", docs,
f"""# Overlay services for {site}.
#
# Names carry the -{site} suffix because both fabrics share one namespace, but
# configuredName pins the on-device network-instance name back to the neutral
# macvrf-l2dci / ipvrf-l3dci that the reference design and the test suite expect.
#
# Which of these stretch across the DCI is a per-service decision:
#   macvrf-l2dci / -b  stretched L2. Route targets (100:100, 110:110) are per-DC;
#                      the stretch happens because the matching
#                      BridgeDomainInterconnect re-originates them onto the WAN
#                      under a SHARED target (65000:100 / 65000:110).
#   macvrf-l3dci       deliberately NOT stretched. Each DC keeps its own subnet
#                      (10.200.1.0/24 vs 10.200.2.0/24) behind an anycast IRB;
#                      only the ip-vrf crosses the WAN, as type-5 routes.
#   ipvrf-l3dci        the tenant L3 VRF, stitched to IP-VPN by the
#                      RouterInterconnect.
#
# The IRB is an anycast gateway - the same 10.200.x.254 on every leaf in the
# fabric. It advertises dynamic ARP entries as interface-less (RFC 9014) host
# routes, and does NOT populate host routes from ARP: those two look
# interchangeable but are not. Host-route population creates the /32 locally on
# whichever leaf happened to ARP the host, and that /32 leaves as a type-5 with
# no ESI, so no other node can alias it - traffic to a multi-homed host pins to
# one leaf of the segment. Advertising the ARP entry as an IFL host route
# instead carries the segment with it, and every node resolves the /32 over both
# leaves. rfc9135SymmetricMode is the knob that picks IFL, despite the name:
# true renders "interface-less-routing", false leaves it off.
#
# The Router deliberately has NO nodeSelectors. Left blank it lands only where
# there is an IRB or routed interface - the leaves - plus wherever a
# RouterInterconnect pins it, which is the DCGWs. Naming the nodes explicitly
# (site + role=leaf, site + role=dcgw) looks tidier and does keep the spines
# clean, but forcing the router onto a DCGW brings the bridge domain and the
# anycast IRB along with it. The DCGW then answers ARP for the tenant subnet
# itself and resolves local host routes through its own bridge domain instead of
# the aliased IFL host routes, back to a single next hop. Blank keeps the DCGW's
# ip-vrf what the reference design has: a pure stitching VRF with no interfaces.
#
# A site-only selector is worse still - it matches the spines too, and they end
# up carrying a tenant ip-vrf, mac-vrf and IRB they have no use for.""")

# ------------------------------------------------------------- 60 interconnects
ic = []
for site in ("dc1", "dc2"):
    d = DC[site]
    gw_list = "\n".join(f"    - {g}" for g in d["gws"])
    for bd, wan_evi, wan_rt, dc_rd in (("l2dci", 1100, f"{GW_ASN}:100", 100),
                                       ("l2dci-b", 1110, f"{GW_ASN}:110", 110)):
        ic.append(f"""apiVersion: services.eda.nokia.com/v2
kind: BridgeDomainInterconnect
metadata:
  name: macvrf-{bd}-{site}-ic
  namespace: {NS}
spec:
  bridgeDomainRef:
    name: macvrf-{bd}-{site}
    namespace: {NS}
  nodes:
{gw_list}
  primaryBGPInstance:
    routeDistinguisher:
      administrator: {d['anycast']}
      assignedNumber: "{dc_rd}"
  interconnectBGPInstance:
    controlPlane: EVPN
    encapsulation: MPLS
    evi: {wan_evi}
    exportTarget: target:{wan_rt}
    importTarget: target:{wan_rt}
    routeDistinguisher:
      administrator: {d['anycast']}
      assignedNumber: "{wan_evi}"
    mpls:
      allowedTunnelTypes:
        - LDP
        - SR-ISIS""")
    ic.append(f"""apiVersion: services.eda.nokia.com/v2
kind: RouterInterconnect
metadata:
  name: ipvrf-l3dci-{site}-ic
  namespace: {NS}
spec:
  routerRef:
    name: ipvrf-l3dci-{site}
    namespace: {NS}
  nodes:
{gw_list}
  primaryBGPInstance:
    domainId: "{d['soo']}"
  interconnectBGPInstance:
    controlPlane: IPVPN
    encapsulation: MPLS
    domainId: "{WAN_DPATH}"
    exportTarget: target:{GW_ASN}:3000
    importTarget: target:{GW_ASN}:3000
    routeDistinguisher:
      type: Type 1
      assignedNumber: "13000"
    mpls:
      allowedTunnelTypes:
        - LDP
        - SR-ISIS""")

write("60-interconnects.yaml", ic,
"""# The DCI stitch itself - the two resources this whole design exists to exercise.
#
# Each adds a SECOND BGP instance to a service that already exists in the local
# fabric, and that second instance speaks MPLS to the WAN instead of VXLAN to the
# fabric. The gateway re-originates between the two, so VXLAN terminates at the
# DCGW and never crosses the DCI.
#
# BridgeDomainInterconnect (L2, 4 CRs = 2 bridge domains x 2 DCs)
#   The local side keeps its per-DC route target; the interconnect side uses a
#   target shared by both DCs (65000:100 for BD-A, 65000:110 for BD-B), which is
#   what actually joins them. The WAN EVI is offset by 1000 from the DC EVI purely
#   as a readable convention.
#
#   Both route distinguishers use the pair's ANYCAST loopback as the administrator
#   field, not the node's own system0. That is what makes a DCGW pair look like a
#   single PE to the far end: both gateways advertise the same RD, so failing over
#   between them is not a new route. It is also why primaryBGPInstance is required
#   whenever the gateways are redundant - without it the fabric-side instance falls
#   back to a per-node RD and the pair de-aliases.
#
# RouterInterconnect (L3, 2 CRs = 1 ip-vrf x 2 DCs)
#   Stitches the EVPN type-5 ip-vrf to IP-VPN over MPLS. Loop prevention is native
#   here: domainId is the WAN IP-VPN domain (65000:100, shared by both DCs) and
#   primaryBGPInstance.domainId the local DC's EVPN domain (65000:1 / 65000:2). A
#   route this gateway pushed into the DC, which its pair partner then pushed back
#   at the WAN, carries the WAN domain and is caught as a D-PATH loop on re-import.
#   No policy needed - contrast the L2 side, which needs the SOO community for
#   exactly the same job.
#
#   Its route distinguisher must be Type 1. RouterInterconnect has no
#   administrator field (BridgeDomainInterconnect does), so the administrator is
#   derived from the type: Type 1 takes it from the gateway's own system0 and
#   yields the per-node RD an ip-vrf wants, keeping the two gateways distinct
#   IP-VPN next hops. Only the L2 side aliases its pair behind one anycast RD.
#   Type 0 renders "None:13000" - EDA has no 2-byte AS to fill in - and the node
#   rejects it. Omitting type entirely is worse than it looks: the docs promise a
#   NOS-generated RD, but the IP-VPN instance has no EVI to derive one from, so no
#   RD is emitted at all and the VRF silently exports nothing.
#
# Neither kind names an exportPolicy or importPolicy, even though both accept
# them. Those fields attach the policy to the SERVICE (network-instance ...
# protocols bgp-vpn), and the WAN policies here are session-scoped: they stamp
# SOO on everything leaving for the WAN and match on address family, over a
# default-action of reject. At the service attachment point that rejects every
# route the VRF would have exported - l3vpn-ipv4 sits at 0/0/0 with the sessions
# happily established. The DefaultBGPGroup applies them at the BGP group instead,
# which is where the hand-built reference puts them.
#
# Both kinds resolve their MPLS next hop over LDP or SR-ISIS, whichever IS-IS
# currently prefers.""")

# ------------------------------------------------------------- 70 configlets
cfg = []
for es_name, (_, _, _, members) in ES.items():
    nodes = "\n".join(f"    - {n}" for n, _ in members)
    es_path = (".system.network-instance.protocols.evpn.ethernet-segments"
               ".bgp-instance{.id==1}.ethernet-segment{.name==\"%s\"}" % es_name)
    cfg.append(f"""apiVersion: config.eda.nokia.com/v1
kind: Configlet
metadata:
  name: ifl-host-ad-routes-{es_name}
  namespace: {NS}
spec:
  operatingSystem: srl
  priority: 50
  endpoints:
{nodes}
  configs:
    - operation: Update
      path: '{es_path}'
      config: '{{"advertise-ifl-host-ad-routes":{{}}}}'""")

ni_path = ('.network-instance{.name=="ipvrf-l3dci"}.protocols.bgp-evpn'
           '.bgp-instance{.id==1}')
for site, d in DC.items():
    nodes = "\n".join(f"    - {g}" for g in d["gws"])
    cfg.append(f"""apiVersion: config.eda.nokia.com/v1
kind: Configlet
metadata:
  name: ipvrf-mh-mode-{site}
  namespace: {NS}
spec:
  operatingSystem: srl
  priority: 50
  endpoints:
{nodes}
  configs:
    - operation: Update
      path: '{ni_path}'
      config: '{{"multi-homing-mode":"network"}}'""")

for site, d in DC.items():
    for gw in d["gws"]:
        grp_path = ('.network-instance{.name=="default"}.protocols.bgp'
                    '.group{.group-name=="bgpgroup-ebgp-%s"}' % site)
        chain = f'["dcgw-dc-pin-{gw}","ebgp-isl-export-policy-{site}"]'
        cfg.append(f"""apiVersion: config.eda.nokia.com/v1
kind: Configlet
metadata:
  name: dc-pin-chain-{gw}
  namespace: {NS}
spec:
  operatingSystem: srl
  priority: 50
  endpoints:
    - {gw}
  configs:
    - operation: Update
      path: '{grp_path}'
      config: '{{"export-policy":{chain}}}'""")

write("70-configlets.yaml", cfg,
"""# The one piece of this design that no CR can express.
#
# advertise-ifl-host-ad-routes tells an all-active ES pair to advertise host
# Auto-Discovery routes for the IFL ip-vrf, so a remote node resolving a /32
# tenant host route gets BOTH leaves of the segment as next hops. Without it the
# /32 is only advertised by whichever leaf happened to ARP the host, and every
# node in the fabric pins inter-subnet traffic for that host to that one leaf -
# the ES is all-active for bridged traffic and effectively single-active for
# routed traffic to a host route.
#
# There is no field for it anywhere: not on Interface (where the ESI and the
# all-active mode live), not on Router, not on RouterInterconnect. Setting it
# by hand on the node does not survive either - it is inside the subtree EDA
# owns for the ethernet segment, so the next reconcile deletes it again. A
# Configlet is the supported way to add config EDA has no schema for, and it
# keeps the value in the same declarative bundle as everything else rather
# than in a post-deploy script.
#
# Path is jspath, keyed the way the node keys the list, and the value is a
# presence container - "advertise-ifl-host-ad-routes {}" in the config tree,
# hence an empty JSON object rather than true.
#
# The second Configlet puts the DCGWs' fabric-side EVPN instance back to
# multi-homing-mode network, which is the NOS default. RouterInterconnect forces
# it to access, and on access a gateway will not alias a remote ethernet segment:
# it holds the host A-D routes from both leaves of the segment, marks them valid,
# and uses neither. The leaves alias fine, so the symptom is narrow and easy to
# miss - every /32 for a multi-homed host resolves over both leaves everywhere in
# the fabric EXCEPT on the two gateways, which pin it to whichever leaf ARPed the
# host. Traffic arriving from the far DC for that host then uses one leaf.
#
# There is no field for it: RouterInterconnect's primaryBGPInstance carries only
# domainId. Deleting the leaf out of band works and then gets reconciled away,
# same as the ES flag, so it belongs here.""")

print("\ndone")
