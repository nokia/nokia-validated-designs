© 2026 Nokia  
Licensed under the BSD 3-Clause License  
SPDX-License-Identifier: BSD-3-Clause  

# EVPN Datacenter Interconnect NVD with EDA

> **Pre-requisite requirements:** an existing EDA deployment (kind installation) with `SIMULATE=false` so external Containerlab nodes can be onboarded, a valid EDA license, and an SR Linux license for the six 7250 IXR-X3b nodes.

This is the EDA-managed twin of the hand-built `dci-without-eda` design. Two independent EVPN-VXLAN fabrics, each with four leaves, two spines and a redundant DCGW pair, are stitched together over an MPLS WAN. VXLAN terminates on the DCGWs and the services are re-originated onto the WAN as EVPN-MPLS (L2) and IP-VPN (L3), so no VXLAN tunnel ever crosses the DCI.

The point of the design is not the topology, it is the question of how much of a real DCI you can express as EDA custom resources. Everything here is declarative: 404 CRs across twenty files in `eda-manifests/`, applied through the transaction API. Where a CR could not express something the reference design needs, that is called out below rather than papered over.

| | |
|---|---|
| Fabrics | 2 (dc1, dc2), one EDA `Fabric` each, EBGP unnumbered underlay and EBGP overlay |
| Nodes | 8 leaves (7220 D5), 4 spines (7220 D5), 4 DCGWs (7250 IXR-X3b), 2 P routers (7250 IXR-X3b) |
| WAN | AS 65000 iBGP full mesh between the four DCGWs, IS-IS L2 with segment routing, LDP and SR-ISIS in parallel |
| L2 DCI | two stretched bridge domains, `BridgeDomainInterconnect` per DC, anycast RD per gateway pair |
| L3 DCI | one tenant ip-vrf, `RouterInterconnect` per DC, per-node RD, D-PATH loop prevention |
| Multihoming | four all-active ethernet segments, one per leaf pair |

## Deployment

EDA and the Containerlab topology run on the same host, each with its own bridge, so the Containerlab bridge has to be allowed to reach the kind bridge EDA sits on. The deploy script installs the rule itself, resolving the bridge name from the Docker network rather than assuming a subnet:

```
:~$ sudo iptables -I DOCKER-USER 2 -o br-<kind-bridge-id> \
       -m comment --comment "allow communications to kind bridge (EDA)" -j ACCEPT
```

Then:

```
:~$ ./deploy-dci-nvd.sh
```

The script deploys the Containerlab topology, onboards the eighteen nodes, and applies the manifests in dependency order. Pass `--skip-clab` if the topology is already running.

Two things about it are worth knowing before you read the log.

Nodes are onboarded **one at a time**, waiting for each to reach `Synced` before starting the next. A node's onboarding commit carries its entire intent — fabric, services, BGP — and runs under commit-confirmed. Batch several nodes on a loaded host and the slowest can miss its confirm window, at which point EDA fails the whole batch and doubles its retry backoff each time (77s, 2m, 4m, up to about half an hour). Sequential onboarding is slower to start and far more predictable. If nodes do get stuck in `NotOnBoarded`, recreating the `TopoNode` will not help — only `kubectl rollout restart deploy/eda-ce` resets the backoff timer.

The **apply order matters** and is not the numeric order of the files. Routing policies (`24-`) are applied before the WAN layer even though they read like a late concern, because the WAN BGP groups name them as import and export policies and an unresolved policy reference fails the entire transaction. Configlets (`70-`) go last, because they patch subtrees that the services must already own.

### Destroying

```
:~$ ./destroy-dci-nvd.sh [--keep-clab]
```

Deleting the `dci` namespace removes every CR of the design in one go, which is the whole benefit of keeping both fabrics in one namespace. The Containerlab topology goes too unless you pass `--keep-clab`.

## What the manifests map to

Both fabrics live in **one** namespace. This is not a stylistic choice: `TopoLink` and `Interface` reference nodes by bare string with no namespace field, so the six cross-DC WAN links between the DCGWs can only be expressed if every node is in the same namespace.

| File | Kinds | Notes |
|---|---|---|
| `00`–`05` | `Namespace`, `Init`, `NodeUser`, `NodeProfile`, `TopoNode`, index pools | Custom `clab-srlinux-26.7.1` profile; `onBoarded` is always `false` in the manifest, EDA sets it itself |
| `10`–`12` | `Interface`, `TopoLink` | 80 interfaces, 4 LAGs, 49 links. WAN links are deliberately not `interSwitch`-typed so the `Fabric` link selectors skip them |
| `20` | `Fabric` | DCGWs join via `borderLeafs.borderLeafNodeSelector` |
| `24` | `PrefixSet`, `CommunitySet`, `Policy`, `PolicyDeployment` | Loop prevention, host-route scoping, gateway pinning |
| `30`–`34` | `LabelBlock`, `DefaultRouter`, `DefaultInterface`, `DefaultISISInstance`, `DefaultLDPRouter`, `DefaultBGPGroup`/`Peer` | The WAN underlay |
| `50`, `51` | `BridgeDomain`, `Router`, `IRBInterface`, `VLAN` | Per-DC overlay services |
| `60` | `BridgeDomainInterconnect`, `RouterInterconnect` | The DCI stitch |
| `70` | `Configlet` | The three things no CR could express |

`eda-manifests/generate.py` produces every file from one topology description. Each generated file carries a header comment explaining the non-obvious choices in it; those comments are the real documentation for this design and are worth reading before changing anything.

Selectors use comma-joined AND semantics within a list entry and OR semantics across entries, so a per-site leaf selector is `eda.nokia.com/role=leaf,eda.nokia.com/site=dc1` — two separate list entries would match every leaf and every dc1 node.

## Verdicts on the five design gaps

The five things the hand-built design does that CRs were not obviously able to do. Three turned out to be fully native. One needs a Configlet outright, one is native on its WAN-facing half but needs a Configlet for its DC-facing half, and testing turned up a sixth that needs one too — three Configlets in total.

**1. Anycast IMET originating-ip — native.** `BridgeDomainInterconnect.primaryBGPInstance.routeDistinguisher.administrator` set to the pair's anycast loopback renders `inclusive-mcast originating-ip 192.0.2.150` on both BGP instances, exactly as the reference does. This is also what makes a gateway pair look like one PE to the far end: both advertise the same RD, so failing over between them is not a new route.

**2. SOO loop prevention — native, via `Policy`.** `CommunitySet` plus a `Policy` referenced from the `DefaultBGPGroup` stamps `origin:65000:<dc>` on export and rejects the DC's own stamp on import. L3 does not need it — `RouterInterconnect` handles the equivalent natively with D-PATH domain IDs.

**3. Per-service gateway pinning — expressible, but the DC-facing half needs a Configlet.** The WAN-facing half is a `Policy` on the `DefaultBGPGroup`: BD-B's WAN route target gets local-preference 90, so BD-B settles on dcgw2/dcgw4 while BD-A rides dcgw1/dcgw3, with the de-preferred path staying as hot standby. The DC-facing half needs each gateway to prepend AS-path on the bridge domain its pair partner is primary for, which is per-node asymmetric — and `Fabric` only offers a fabric-wide `overlayProtocol.bgp.exportPolicies`, one value for every node. So the pinning policies are chained ahead of the fabric's own ISL export policy by a per-gateway Configlet, with `NextPolicy` so the fabric policy still makes the accept decision. Note a `Policy` on its own never reaches a node: EDA pushes it only when something it manages references it, and a Configlet reference does not count, hence the explicit `PolicyDeployment`.

**4. Host-route scoping — native, via `Policy`.** A `PrefixSet` of /32s out of the tenant supernet, rejected on WAN export, so only the /24 crosses. The `Policy` families enum is `IPv4`/`IPv6`/`EVPN`/`RTC` with no VPN-IPv4 member, so the match is on prefix alone rather than on family and prefix the way the hand-built config writes it.

**5. `advertise-ifl-host-ad-routes` — Configlet.** No field for it on `Interface`, `Router` or `RouterInterconnect`, and setting it by hand does not survive: it sits inside the subtree EDA owns for the ethernet segment, so the next reconcile removes it. Without it a multi-homed host's /32 is not advertised at all — removing these Configlets on the running lab leaves the host in both ES leaves' ARP tables and the host route on nobody, so every remote node falls back to the anycast /24 and tromboning returns. A single-homed host on the same IRB keeps its /32 throughout, which is what places the behaviour on the ethernet segment rather than on interface-less routing generally. It follows from host routes here coming only from `arp evpn advertise dynamic` plus interface-less-routing, with `hostRoutePopulate` off: in IFL mode on an all-active ES the host route rides on the A-D route, so with no A-D route neither leaf advertises.

Two more things only showed up once traffic was tested — one a real gap, one a trap:

**`multi-homing-mode` on the gateway's fabric-side instance — Configlet.** `RouterInterconnect` forces the DC-side EVPN instance to `access`, and on `access` a gateway will not alias a remote ethernet segment. It holds the host A-D routes from both leaves, marks them valid, and uses neither. The leaves alias correctly, so the symptom is narrow: every node resolves a multi-homed host's /32 over both leaves except the two gateways. The Configlet puts it back to `network`, which is the NOS default. `primaryBGPInstance` carries only `domainId`, so there is no field for it.

**IRB placement is controlled by leaving `Router.nodeSelectors` blank.** This one is a trap rather than a gap. Left blank, the `Router` lands only where there is an IRB or routed interface — the leaves — plus wherever a `RouterInterconnect` pins it, which is the DCGWs, and the result matches the reference exactly: a pure stitching ip-vrf with no interfaces on the gateways. A site-only selector puts a tenant ip-vrf, mac-vrf and IRB on the spines, which have no use for any of it. Naming site and role explicitly keeps the spines clean but forces the router onto the gateways, and the bridge domain and anycast IRB come with it — the gateway then answers ARP for the tenant subnet itself and resolves local host routes through its own bridge domain instead of the aliased IFL host routes, back to a single next hop.

## Things that cost time and are not obvious

`DefaultBGPGroup` has `sendCommunityStandard` and `sendCommunityLarge` but nothing for extended communities. Setting both to `true` renders `send-community-type [ standard large ]` and **strips extended communities from the WAN sessions** — no route targets, no SOO. The sessions establish, prefix counters move, and nothing imports, because an EVPN route with no route target belongs to no service. Omit both fields and EDA renders `send-community-type [ extended ]`, which is what a DCI needs.

`RouterInterconnect`'s route distinguisher must be `Type 1`. There is no `administrator` field (unlike `BridgeDomainInterconnect`), so the administrator comes from the type: `Type 1` takes it from the gateway's own system0 and gives the per-node RD an ip-vrf wants. `Type 0` renders `None:13000` — EDA has no 2-byte AS to fill in — and the node rejects it. Omitting `type` is worse than it looks: the field documents a NOS-generated fallback, but the IP-VPN instance has no EVI to derive one from, so no RD is emitted at all and the VRF silently exports nothing.

Neither interconnect kind should name an `exportPolicy` or `importPolicy`, even though both accept them. Those fields attach the policy to the service (`network-instance ... protocols bgp-vpn`), and these WAN policies are session-scoped — they match on address family and stamp SOO over a default action of reject. At the service attachment point that rejects every route the VRF would have exported, and `l3vpn-ipv4` sits at `0/0/0` with the sessions happily established. The `DefaultBGPGroup` applies them at the BGP group, which is where the hand-built reference puts them.

A DCGW's WAN iBGP sessions and its fabric eBGP sessions share one network-instance and one EVPN address family, so **by default every EVPN route the DCGW learns from its spines is re-advertised straight to the far DC** — original RD, original route target, original VXLAN next hop — and the far DC installs leaf VTEP addresses it has no tunnel to. The hand-built config catches this with internal tags: the VXLAN-side instance stamps `tag-1` and the WAN export rejects `tag-1`. Internal tags are not on any service CR, so the export policy discriminates on route target instead, which separates the two sides just as cleanly: DC instances carry `target:<evi>:<evi>`, interconnect instances `target:65000:*`. The policy accepts exactly the three WAN targets over a default action of reject, rather than accepting everything.

A multi-homed LAG is **one** `Interface` CR with a member on each leaf, not one CR per leaf. The `members` list is how EDA knows which ports form the segment. Split per leaf, the aggregates and their VLAN subinterfaces still render correctly and the config looks right, but no ethernet segment appears under `system network-instance protocols evpn` and the multihoming block is silently ignored.

`IRBInterface.evpnRouteAdvertisementType.rfc9135SymmetricMode` must be `true` to get interface-less (RFC 9014) host routes, despite the name: `true` renders `interface-less-routing`. And `hostRoutePopulate` must stay off. The two look interchangeable but are not — host-route population creates the /32 locally on the leaf that ARPed the host and exports it as a type-5 with no ESI, which no other node can alias.

**EDA does not reconcile config that is deleted out of band.** This matters as soon as you run the failure tests, which disable transports by editing the nodes directly. Once a node's `Synced` state and EDA's intent agree, deleting the pushed config on the device leaves EDA convinced everything is fine — and re-applying the manifest is a no-op, because nothing in the intent changed. Restarting `eda-ce` does not help, nor does toggling `TopoNode.onBoarded`, and manually re-adding the missing lines does not survive: EDA's next commit to that node strips anything not in its intent. The only reliable recovery is to delete the owning CRs and recreate them, which for SR-ISIS meant clearing the `DefaultISISInstance`, its deployments and its interfaces together, since a leftover per-interface node-SID makes the clean re-apply fail validation against the parent blocks that are no longer on the node. Node SIDs are reallocated from the pool on the way back, which is one of the reasons the tests read them off the nodes.

EDA manages the service label blocks itself (`eda-default-networkinstance`, `eda-default-system-evpn`, `eda-default-evpn-imet`) and binds `system mpls services` to those, not to any `LabelBlock` you define. The `LabelBlock` CRs here still matter for LDP, SR-ISIS and the SRGB.

## Validation

`tests/` is the suite from the without-EDA design, repointed at this topology. Two changes were needed for an EDA-managed fabric: gNMI is on port 57410 rather than the Containerlab default of 57400, and system0 addresses and IS-IS node SIDs are read back off the nodes instead of hardcoded, because EDA allocates both from pools and the values change between deployments.

```
:~/dci-with-eda/tests$ python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
:~/dci-with-eda/tests$ .venv/bin/python -m pytest -v -m connectivity
```

All 43 tests pass against EDA 26.8.2 / SR Linux 26.7.1. The markers are `connectivity` (25 — L2 and L3 DCI reachability, control plane, WAN transport, host-route scoping), `ecmp` (2 — SR-ISIS and LDP transport diversity, per-service gateway pinning) and `disruptive`/`convergence` (16 — link, gateway and transport failures measured as mid-stream loss across hashed flows). Omit `-m` to run everything; the failure tests take about thirteen minutes because each one restores the fabric and waits for it to reconverge before the next starts.

The failure tests edit the nodes directly, which is worth remembering given that EDA will not reconcile such changes back (see above). Their teardown is written accordingly: it restores exactly the lines EDA renders and nothing more, and it reads pool-allocated values such as the IS-IS node SID into cache *before* deleting them, since the delete removes the only copy.

### Revision history

| EDA Version | SRL version | Date tested |
|-------------|-------------|-------------|
| 26.8.2 | 26.7.1 | September 2026 |
