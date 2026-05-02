# NetBox as Source of Truth for NVD

Operator guide for using NetBox as the input for the NVD automation
engine. Covers installation on the NetBox host, one-shot schema setup,
seeding the `3-stage-evpn-vxlan` design, exporting / deploying via the
CLI, triggering from the NetBox UI, and tearing the instance back to a
clean slate between runs.

See [`automation/NETBOX_INTEGRATION.md`](NETBOX_INTEGRATION.md) for the
architecture rationale. This file is the hands-on walkthrough.

---

## How it fits together

```
               +-------------------+      +-----------------+
               |  YAML inputs      |      |  NetBox API     |
               | (validated-       |      | (Sites, Devices,|
               |  designs/*/inputs)|      |  Cables, VLAN,  |
               +---------+---------+      |  VRFs, custom   |
                         |                |  fields, tags)  |
                         |                +--------+--------+
                         |                         |
                         v                         v
               +---------+-------------------------+---------+
               |                FabricIntent                 |
               |          (design-agnostic contract)         |
               +-----+------------------+----------------+---+
                     |                  |                |
                     v                  v                v
               EDA Generator     Ansible Generator  Clab Generator
                     |
                     v
                  EdaClient
```

The NetBox builder is a drop-in replacement for the YAML builder. Every
generator, validator, and executor downstream is unchanged.

---

## Data model mapping — `FabricIntent` ↔ NetBox

The mapping is hybrid: whatever fits NetBox's model cleanly uses native
objects (Sites, Devices, Interfaces, Cables, VLAN Groups, VLANs, VRFs,
Prefixes); everything else is serialised into a single JSON custom
field `nvd_config` on the Site. This keeps the seed and the builder
symmetrical and guarantees round-trip fidelity.

All NVD-created objects carry the `nvd-managed` tag, and every NVD
custom field starts with `nvd_` — this is the contract that
`netbox_reset` uses. The authoritative list of field names lives in
[`automation/builders/netbox_schema.py`](builders/netbox_schema.py).

### Fabric identity — `Site` + Site custom fields

| `FabricIntent` field     | NetBox location                                               |
|--------------------------|---------------------------------------------------------------|
| `fabric_name`            | `Site.slug`                                                   |
| `design`                 | `Site.custom_fields.nvd_design` (select)                      |
| `environment`            | `Site.custom_fields.nvd_environment` (select)                 |
| `spine_asn`              | `Site.custom_fields.nvd_spine_asn`                            |
| `leaf_asn_start`         | `Site.custom_fields.nvd_leaf_asn_start`                       |
| `system0_prefix`         | `Site.custom_fields.nvd_system0_prefix` (also a `Prefix`)     |
| `mgmt_subnet`            | `Site.custom_fields.nvd_mgmt_subnet` (also a `Prefix`)        |
| `eda.namespace`          | `Site.custom_fields.nvd_eda_namespace` (fallback; see blob)   |
| `eda.node_profile`       | `Site.custom_fields.nvd_eda_node_profile` (fallback)          |

Lifecycle custom fields written by the Custom Script — purely operational,
not part of the intent contract: `nvd_deployment_state`,
`nvd_last_deploy_at`, `nvd_last_deploy_txid`, `nvd_last_deploy_phase`,
`nvd_last_deploy_error`, `nvd_last_deploy_summary`,
`nvd_last_deploy_duration`.

### `NodeIntent` ← `dcim.Device`

| `NodeIntent` field        | NetBox location                                                |
|---------------------------|----------------------------------------------------------------|
| `name`                    | `Device.name`                                                  |
| `role`                    | `Device.role.slug` (`leaf` / `spine` / `tor` / `collapsed-spine`) |
| `platform`                | `Device.device_type.model` (must match `PLATFORM_REGISTRY`)    |
| `version`                 | `Device.custom_fields.nvd_srl_version`                         |
| `asn`                     | `Device.custom_fields.nvd_asn`                                 |
| `mgmt_ipv4`               | `Device.primary_ip4.address` (CIDR → host/prefix split)        |
| `system0_ipv4`            | `Device.custom_fields.nvd_system0_ipv4`                        |
| `labels`                  | `Device.custom_fields.nvd_labels` (JSON dict of `key: value`)  |
| `uplink_interfaces`       | `Device.custom_fields.nvd_uplink_interfaces` (JSON list)       |

### `LinkIntent` ← `dcim.Cable`

Every NVD cable is between two `dcim.Interface`s on different devices.
The builder walks the cable's `a_terminations` / `b_terminations` to
recover both ends.

| `LinkIntent` field  | NetBox location                                           |
|---------------------|-----------------------------------------------------------|
| `name`              | Derived: `{local_node}-{remote_node}` (canonical ordering) |
| `local_node`        | `Cable.a_terminations[0].object.device.name`              |
| `local_interface`   | `Cable.a_terminations[0].object.name`                     |
| `remote_node`       | `Cable.b_terminations[0].object.name`                     |
| `remote_interface` | `Cable.b_terminations[0].object.name`                     |

Interfaces on both ends carry `nvd_role = isl` so the reset script and
the builder can filter them.

### `EdgeInterfaceIntent` ← tagged `dcim.Interface`

Filter: `Interface.tags contains nvd-edge` (equivalently `nvd_role =
edge`).

| `EdgeInterfaceIntent` field | NetBox location                                    |
|-----------------------------|----------------------------------------------------|
| `name`                      | `"{device.name}-{interface.name}"` (builder-prefixed) |
| `node`                      | `Interface.device.name`                            |
| `interface`                 | `Interface.name`                                   |
| `encap`                     | Derived from native **802.1Q Mode** (`Interface.mode`): `access → null`, `tagged / tagged-all → dot1q`. `Interface.custom_fields.nvd_encap` is a legacy fallback (see "802.1Q Mode drives EDA encap" below). |
| `labels`                    | `Interface.custom_fields.nvd_labels` (JSON list of strings, see below) |
| `lag`                       | `Interface.lag.name` (if the edge is a LAG member) |

### `LagIntent` ← `dcim.Interface` (type `lag`) + members

One logical LAG in `FabricIntent` may span multiple devices. NetBox
models this as one LAG interface per device, linked by name. The
builder groups them back into a single `LagIntent` keyed on the
logical name.

| `LagIntent` field      | NetBox location                                                  |
|------------------------|------------------------------------------------------------------|
| `name`                 | `Interface.name` (e.g. `lag1`) — same across devices             |
| `aggregate_id`         | `Interface.custom_fields.nvd_aggregate_id`                       |
| `multihoming_mode`     | `Interface.custom_fields.nvd_lag_mode` (`all-active`/`port-active`) |
| `members[].node`       | Member `Interface.device.name`                                   |
| `members[].interface`  | Member `Interface.name` (members link via `lag` FK)              |
| `lacp.*`               | `Interface.custom_fields.nvd_lacp` (JSON with `LacpConfig` shape) |
| `min_links`            | `Interface.custom_fields.nvd_lag_min_links`                      |
| `reload_delay_timer`   | `Interface.custom_fields.nvd_lag_reload_delay`                   |
| `revertive`            | `Interface.custom_fields.nvd_lag_revertive`                      |
| `preferred_active`     | `Interface.custom_fields.nvd_lag_preferred_active`               |
| `standby_signaling`    | `Interface.custom_fields.nvd_lag_standby_signaling`              |
| `labels`               | `Interface.custom_fields.nvd_labels` (JSON list of strings, see below) |

#### Interface labels encoding

Interface labels in `FabricIntent` are a free-form `dict[str, str]`. In
NetBox they are split across three locations so each piece lands in
the most-native place:

1. **VLAN-membership labels** → native NetBox fields. Any label of the
   form `eda.nokia.com/tagged-v<X>` or `eda.nokia.com/untagged-v<X>`
   whose target VLAN exists in the intent is promoted to
   `Interface.tagged_vlans[*]` or `Interface.untagged_vlan` (with
   `Interface.mode` set to `tagged` / `access` accordingly).
   - **Tagged VLANs** are stored as `ipam.VLAN(name="tagged-v<X>",
     vid=<X>)` inside their bridge-domain VLAN Group, so the mapping is
     a direct name match (`tagged-v10` → VLAN named `tagged-v10`).
   - **Untagged VLANs** collapse into a single *reserved* VLAN per
     group named literally `"untagged"` (see "Untagged VLAN sentinel"
     below). The reader reconstructs the intent-level name
     (`untagged-v10`, `untagged-v40`, …) from the parent VLAN Group's
     suffix so the round-trip to `VlanIntent` stays lossless.

   NetBox's UI shows the VLANs as clickable, referentially-integral
   links on the Interface page.
2. **Fallback label list** → `Interface.custom_fields.nvd_labels`, a
   flat JSON **list of strings**. Holds every label that isn't a
   native VLAN attachment:
   - `{k: "enabled"}` collapses to the bare key `"k"`.
   - `{k: v}` with `v != "enabled"` becomes `"k=v"`.
   - `eda.nokia.com/role=X` is **elided** when `X` equals the
     interface's native role (`Interface.custom_fields.nvd_role`) —
     dropping the duplication between `nvd_role=edge` and the label
     `role=edge` on plain edge ports. Meaningful overrides — a routed
     sub-interface tagged `role=routed-s5`, a LAG whose functional
     role is `edge` while `nvd_role=lag` — are kept as `role=<value>`.
   - VLAN-membership labels whose target VLAN isn't in the intent
     (e.g. a pure edge-port selector like `tagged-v99` with no bridge
     domain) also land here as a fallback so nothing is lost.

Example (`leaf1/ethernet-1-3`, a plain edge carrying tagged-v10 natively
and a tagged-v99 selector tag with no backing VLAN):

- Native: `Interface.mode = "tagged"`, `Interface.tagged_vlans = ["tagged-v10"]`
- `nvd_labels = ["eda.nokia.com/tagged-v99"]`

Example (`leaf4/leaf4-leaf5-lag1`, a LAG whose functional role is edge,
carrying VLAN 30 untagged):

- Native: `Interface.mode = "access"`,
  `Interface.untagged_vlan = "untagged"` (the reserved sentinel inside
  VLAN Group `macvrf-v30`)
- `nvd_labels = ["eda.nokia.com/role=edge"]`

The builder reconstructs the original labels dict on read — reading
native VLAN FKs (turning the reserved `untagged` VLAN inside
`macvrf-v30` back into the label `eda.nokia.com/untagged-v30=enabled`),
decoding the fallback list, and reinjecting
`eda.nokia.com/role=<nvd_role>` when absent.

#### 802.1Q Mode drives EDA encap

The native **802.1Q Mode** field on `dcim.Interface` is the single
source of truth for a port's encapsulation. The reader maps it into
`EdgeInterfaceIntent.encap`, which the EDA generator (see
[`_cr_interface_edge` in `automation/generators/eda_generator.py`](generators/eda_generator.py))
reads verbatim into `InterfaceSpec.encap_type`.

| 802.1Q Mode (NetBox UI) | `EdgeInterfaceIntent.encap` | EDA `InterfaceSpec.encap_type` |
|-------------------------|-----------------------------|--------------------------------|
| Access                  | `null`                      | `null` (no tag on the wire)    |
| Tagged                  | `dot1q`                     | `dot1q`                        |
| Tagged (All)            | `dot1q`                     | `dot1q`                        |
| *(unset)*               | `dot1q` (default)           | `dot1q`                        |

The writer sets `Interface.mode` on every nvd-managed edge interface
directly from the VLAN labels it finds (any `tagged-vN` label → Tagged;
only `untagged-vN` labels → Access), so what you see in the NetBox UI
always matches what will land in EDA.

The previous `nvd_encap` custom field is **soft-deprecated** and
reflected as such in its NetBox description. The writer no longer
populates it, and the reader consults it only as a fallback when
`Interface.mode` is unset — this keeps pre-migration NetBox data
readable for one release cycle. A follow-up change can drop the
field once every instance has been re-seeded.

##### SR Linux constraint: tagged parent + untagged routed sub-interface is illegal

On SR Linux, the `vlan.encap.untagged` construct (i.e. accepting
untagged frames on a sub-interface of a trunk) is supported **only
for bridged sub-interfaces**. Routed sub-interfaces do not have
that option, which leads to two hard rules:

- If a port's `Interface.mode = Tagged` (parent `vlan-tagging=true`),
  every routed sub-interface on that port MUST carry a dot1q VLAN
  (`RoutedInterfaceIntent.vlan_id` is a numeric string like `"100"`).
  An untagged routed sub-interface on a tagged parent will produce
  the EDA error *"vlan tagging true inconsistent with subinterface
  4097"* at commit time.
- If `Interface.mode = Access` (parent `vlan-tagging=false`), the
  only legal sub-interface is `vlan_id="null"`.

These rules apply regardless of whether the intent came from YAML
or NetBox. The `FabricIntent.validate_cross_references` validator
enforces them at intent-build time so a mismatch fails fast with a
clear error, instead of surfacing deep inside an EDA commit. The
corresponding unit tests live in
`tests/unit/test_routing_policy_model.py`
(`TestRoutedInterfaceEncapConsistency`).

If you edit an interface in the NetBox GUI that hosts a routed
sub-interface, **keep the Mode aligned with the routed VLAN**. In
particular: don't flip an interface to Tagged + attach a sentinel
while a `dcim.Interface` that has a NVD routed-interface parent
keeps its `vlan_id="null"` — the next deploy will be rejected.

### `BridgeDomainIntent` ← `ipam.VLANGroup`

One VLAN Group per mac-vrf, scoped to the Site.

| `BridgeDomainIntent` field | NetBox location                              |
|----------------------------|----------------------------------------------|
| `name`                     | `VLANGroup.name`                             |
| `type`                     | `VLANGroup.custom_fields.nvd_bd_type`        |
| `vni`                      | `VLANGroup.custom_fields.nvd_vni`            |
| `evi`                      | `VLANGroup.custom_fields.nvd_evi`            |
| `mac_learning`             | `VLANGroup.custom_fields.nvd_mac_learning`   |
| `mac_aging`                | `VLANGroup.custom_fields.nvd_mac_aging`      |
| `mac_duplication`          | `VLANGroup.custom_fields.nvd_mac_duplication` (JSON) |
| `origin`                   | `VLANGroup.custom_fields.nvd_origin`         |
| `export_target`            | `VLANGroup.custom_fields.nvd_export_target` (optional; `target:N:N`, overrides default `target:1:<evi>`) |
| `import_target`            | `VLANGroup.custom_fields.nvd_import_target` (optional; `target:N:N`, overrides default `target:1:<evi>`) |

### `VlanIntent` ← `ipam.VLAN`

| `VlanIntent` field     | NetBox location                                                 |
|------------------------|-----------------------------------------------------------------|
| `name`                 | `VLAN.name`                                                     |
| `vlan_id`              | `VLAN.custom_fields.nvd_vlan_id_str` (preferred; preserves `"untagged"`), else `VLAN.vid` |
| `bridge_domain`        | `VLAN.group.name` (cross-referenced back to the VLAN Group)     |
| `interface_selector`   | `VLAN.custom_fields.nvd_interface_selector` (JSON list)         |

#### Untagged VLAN sentinel

An `SR Linux` sub-interface can be explicitly **untagged** (no
802.1Q tag on the wire). NetBox's native `ipam.VLAN.vid` field has
no representation for "no tag" — it must be a valid 802.1Q id in
`1..4094`. To bridge that mismatch NVD reserves a **single sentinel
VLAN per VLAN Group**:

- One `ipam.VLAN` per VLAN Group, named literally `"untagged"` with
  `vid=4094`. The bridge-domain association comes from the VLAN's
  *group*, not from its name, so groups don't need matching
  `untagged-v<X>` per-BD rows anymore — one per group suffices.
- `ipam.VLAN.custom_fields.nvd_vlan_id_str = "untagged"` is the
  authoritative encap value. Readers (and `VlanIntent` round-trip)
  MUST consult this field and never `vid`.
- `ipam.VLAN.description` carries a plain-English note so the
  sentinel is self-documenting in the NetBox UI, e.g.

  ```
  Reserved untagged VLAN for bridge domain macvrf-v40.
  The displayed VID 4094 is a sentinel (NetBox requires vid in
  1..4094); this VLAN carries NO 802.1Q tag on the wire. See
  custom field 'nvd_vlan_id_str' for the authoritative value.
  ```

- `ipam.VLAN.custom_fields.nvd_interface_selector` is left empty on
  the sentinel. Readers synthesise the selector back from the group
  name suffix, emitting
  `eda.nokia.com/untagged-v<X>=enabled` on read.

**Auto-create pre-pass.** `build_intent_from_netbox` runs a small
pre-pass (`_ensure_untagged_sentinels`) that iterates every
`nvd-managed` VLAN Group at the target site and creates the
`untagged` / `vid=4094` sentinel if it is missing. This means
humans can add a new VLAN Group in the NetBox GUI without also
remembering to add the sentinel — the next deploy will do it for
them, idempotently.

**Adding an edge sub-interface through the NetBox UI.** Both cases
only need `Interface.mode` + the VLAN fields:

- *Tagged* (e.g. a new `vid=98` on `macvrf-v10`): create an
  `ipam.VLAN` named `tagged-v98` (vid=98) inside group `macvrf-v10`,
  then on the target `dcim.Interface` set **802.1Q Mode = Tagged**
  and add the VLAN under **Tagged VLANs**.
- *Untagged* (no 802.1Q tag on the wire): on the target
  `dcim.Interface` set **802.1Q Mode = Access** and pick the
  group's reserved `untagged` VLAN under **Untagged VLAN**. No new
  VLAN row needed — the sentinel already exists (or the next
  deploy will create it).

If your design legitimately uses VID 4094 as a real customer VLAN
in a bridge domain that also has an untagged sibling, the two will
collide (NetBox enforces unique `vid` within a VLAN Group). The
fabric's design builders don't produce VID 4094 today, but if that
ever becomes a concern the right fix is to collapse to one
`ipam.VLAN` per bridge domain and express port encap exclusively
through `Interface.mode` / `tagged_vlans` / `untagged_vlan`.

### `RouterIntent` ← `ipam.VRF`

NetBox `ipam.VRF` objects are global (their uniqueness constraint is
`(name, rd)` — not `(site, name)`), so NVD scopes a VRF back to its
fabric through the `nvd_site` custom field. The writer always sets
`nvd_site = <site.slug>`; the reader filters on `nvd_managed` tag
**and** `nvd_site == <site.slug>`. Two fabrics can therefore reuse
`vrf1` without colliding — they are two separate NetBox rows that share
a name.

| `RouterIntent` field    | NetBox location                              |
|-------------------------|----------------------------------------------|
| `name`                  | `VRF.name`                                   |
| `vni`                   | `VRF.custom_fields.nvd_vni`                  |
| `evi`                   | `VRF.custom_fields.nvd_evi`                  |
| `node_selector`         | `VRF.custom_fields.nvd_node_selector` (JSON) |
| `export_target`         | `VRF.custom_fields.nvd_export_target` (optional; `target:N:N`, overrides default `target:1:<evi>`) |
| `import_target`         | `VRF.custom_fields.nvd_import_target` (optional; `target:N:N`, overrides default `target:1:<evi>`) |
| *(site scope)*          | `VRF.custom_fields.nvd_site` — slug of owning Site |

Static routes live on the VRF they belong to, not on the Site. Each
`StaticRouteIntent` is appended to `VRF.custom_fields.nvd_static_routes`
(JSON list) with the `router` key stripped — the owning VRF is implicit.

| `StaticRouteIntent` field | NetBox location                                         |
|---------------------------|---------------------------------------------------------|
| `name`                    | entry `name` in `VRF.custom_fields.nvd_static_routes`   |
| `router`                  | *implicit* — the owning `VRF.name`                      |
| `nodes`                   | entry `nodes`                                           |
| `prefixes`                | entry `prefixes`                                        |
| `nexthop_group`           | entry `nexthop_group`                                   |

### `IrbInterfaceIntent` ← `dcim.Interface` (type `virtual`)

An IRB is modelled as one `dcim.Interface` per device where its VRF is
deployed — so `irb-v10` attached to `vrf1` (selector `role=leaf`) on an
8-leaf fabric produces 8 rows, one per leaf. Each row is tagged
`nvd-managed` + `nvd-irb`, has `type=virtual`, and cross-references its
VRF through the native `Interface.vrf` foreign key. Bridge-domain
linkage lives in the shared `nvd_bridge_domain_ref` custom field. The
remaining IRB knobs (anycast_gw, proxy_*, arp_timeout, learn_unsolicited,
evpn_route_advertisement_type, host_route_populate, ip_addresses/ipv4,
origin) are serialised into `nvd_irb_config` (JSON) because they have no
native NetBox counterpart.

| `IrbInterfaceIntent` field     | NetBox location                                          |
|--------------------------------|----------------------------------------------------------|
| `name`                         | `Interface.name` (same on every device instance)         |
| `description`                  | `Interface.description`                                  |
| `router`                       | `Interface.vrf` → `ipam.VRF` (native FK)                 |
| `bridge_domain`                | `Interface.custom_fields.nvd_bridge_domain_ref`          |
| `ip_mtu`                       | `Interface.mtu`                                          |
| `anycast_gw`                   | `Interface.custom_fields.nvd_irb_config.anycast_gw`      |
| `proxy_arp` / `proxy_nd`       | `Interface.custom_fields.nvd_irb_config.proxy_*`         |
| `arp_timeout`                  | `Interface.custom_fields.nvd_irb_config.arp_timeout`     |
| `learn_unsolicited`            | `Interface.custom_fields.nvd_irb_config.learn_unsolicited` |
| `evpn_route_advertisement_type`| `Interface.custom_fields.nvd_irb_config.evpn_route_advertisement_type` |
| `host_route_populate`          | `Interface.custom_fields.nvd_irb_config.host_route_populate` |
| `origin`                       | `Interface.custom_fields.nvd_irb_config.origin`          |
| `ipv4` (legacy shorthand)      | `Interface.custom_fields.nvd_irb_config.ipv4` + `ipam.IPAddress` attached to the interface (role `anycast` when `anycast_gw=True`) |
| `ip_addresses` (dual-stack)    | `Interface.custom_fields.nvd_irb_config.ip_addresses` + one `ipam.IPAddress` per address attached to each leaf's IRB row |

The blob carries the exact addressing shape (legacy `ipv4` string vs
dual-stack `ip_addresses` list) so round-trips stay lossless; the
attached `ipam.IPAddress` objects are a redundant, operator-friendly
view of the same data for NetBox UI consumers.

### `RoutedInterfaceIntent` ← `dcim.Interface` (type `virtual`, `parent`=edge)

A routed subinterface is a single `dcim.Interface` row sitting *on top
of* an existing edge port — think `ethernet-1-4.100` or an untagged
routed port. The native `Interface.parent` FK points at the edge, the
native `Interface.vrf` FK points at the router, and the native
`Interface.mtu` captures `ip_mtu`. Non-native settings (VLAN tag,
ARP timeout, IPv4/IPv6 address shapes with `primary` flags) live in the
`nvd_routed_config` JSON custom field.

| `RoutedInterfaceIntent` field | NetBox location                                          |
|-------------------------------|----------------------------------------------------------|
| `name`                        | `Interface.name`                                         |
| `interface`                   | `Interface.parent` → `dcim.Interface` (native FK)        |
| `router`                      | `Interface.vrf` → `ipam.VRF` (native FK)                 |
| `ip_mtu`                      | `Interface.mtu`                                          |
| `vlan_id`                     | `Interface.custom_fields.nvd_routed_config.vlan_id`      |
| `arp_timeout`                 | `Interface.custom_fields.nvd_routed_config.arp_timeout`  |
| `ipv4_addresses`              | `nvd_routed_config.ipv4_addresses` + one `ipam.IPAddress` per entry attached to the subinterface |
| `ipv6_addresses`              | `nvd_routed_config.ipv6_addresses` + one `ipam.IPAddress` per entry attached to the subinterface |

Each row is tagged `nvd-managed` + `nvd-routed` and has
`custom_fields.nvd_role = "routed"`.

### `Prefix` objects (native) — `system0_prefix` and `mgmt_subnet`

Two `ipam.Prefix` objects per site, both tagged `nvd-managed`, both
scoped to the Site.

| `Prefix.custom_fields.nvd_prefix_role` | Role        | `FabricIntent` field |
|----------------------------------------|-------------|----------------------|
| `loopback`                             | `loopback`  | `system0_prefix`     |
| `management`                           | `mgmt`      | `mgmt_subnet`        |

### Non-native bundle — `extras.ConfigContext` per site

Intent fields that don't map cleanly to native NetBox objects live in a
single per-site Config Context named `nvd-<site-slug>-config`, scoped
to the site (`sites=[<site>]`) and tagged `nvd-managed`. The writer
upserts this one row; the reader fetches every `nvd-managed` context
filtered by `site_id`, merges their `data` dicts in ascending `weight`
order, and slices the result into `FabricIntent` fields. Keys live in
[`netbox_schema.py`](builders/netbox_schema.py) as `CCTX_KEY_*`.

| `FabricIntent` field       | Config Context `data` key  |
|----------------------------|----------------------------|
| `configlets`               | `configlets`               |
| `default_mtus`             | `default_mtus`             |
| `banners`                  | `banners`                  |
| `prefix_sets`              | `prefix_sets`              |
| `routing_policies`         | `routing_policies`         |
| `fabric_export_policies`   | `fabric_export_policies`   |
| `fabric_import_policies`   | `fabric_import_policies`   |
| `eda` (full block)         | `eda`                      |
| `breakouts`                | `breakouts`                |

Why Config Contexts instead of the old Site JSON blob:

- First-class object in the NetBox UI with its own edit page, history,
  and API — much better than burying everything inside a Site custom
  field.
- Scope rules are native (`sites`, `roles`, `platforms`, `tags`, …),
  so splitting a fabric-wide bundle into role-scoped slices later is a
  schema-free change. The reader already merges whatever contexts are
  tagged `nvd-managed` for the site — additional contexts just plug in.
- The writer is still idempotent (upsert by name) and the reset script
  picks the contexts up via the `nvd-managed` tag.

### Remaining Site blob — `credentials` only

| `FabricIntent` field | `Site.custom_fields.nvd_config` JSON key |
|----------------------|------------------------------------------|
| `credentials`        | `credentials`                            |

Credentials stay on the Site custom field instead of moving into the
Config Context because Config Contexts don't have row-level ACLs —
they're readable by anyone who can see a device the context is
attached to. Keeping credentials on the Site CF lets operators gate
them through NetBox's object-level permissions or, eventually, migrate
them to NetBox Secrets.

### Supporting objects created by `netbox_setup`

One-off objects that don't correspond to any `FabricIntent` field but
are prerequisites for the mapping above:

- Tags: `nvd-managed`, `nvd-edge`, `nvd-extras`, `nvd-irb`, `nvd-routed`.
- Device Roles: `leaf`, `spine`, `tor`, `collapsed-spine`.
- Manufacturer: `Nokia`; Platform: `SR Linux` (slug `srlinux`).
- Device Types: `7220 IXR-D3L`, `D2L`, `D4`, `D5`, `7730 SVR-LH10/18/10C`.

---

## Prerequisites

- NetBox 4.x reachable over HTTPS. Self-signed certs are fine — pass
  `--no-netbox-verify` (the default when `--netbox-verify` is not set).
- An API token with write permission on DCIM, IPAM, and the Extras
  (custom field / script) subsystems. NetBox 4.x "v2" tokens (the ones
  prefixed `nbt_`) are supported transparently.
- Network reachability from wherever you run the CLI to the NetBox host
  and, if you are deploying, to the EDA controller.
- The NVD repo itself checked out on the box that will execute
  deployments. When running from NetBox UI this is typically the NetBox
  host at `/opt/nvd`.

Export credentials once per shell so every command below can pick them
up automatically:

```bash
export NETBOX_URL=https://srv9002
export NETBOX_TOKEN=...              # nbt_... for NetBox 4.x
export EDA_URL=https://srv0201:9443
export EDA_USER=admin
export EDA_PASSWORD=admin
```

---

## Install the `[netbox]` extra

The NetBox builder depends on `pynetbox>=7.3`, which is declared as an
optional extra so pure-YAML users are not forced to pull it in.

```bash
# with uv (recommended — matches the committed lockfile)
uv sync --extra netbox

# or with pip
pip install -e '.[netbox]'
```

After this, `python -m automation.builders.netbox_setup --help`,
`... .netbox_seed_dc1_3stage --help`, and `... .netbox_reset --help`
will all run.

---

## Step 1 — Create the NVD schema in NetBox (one-shot)

`netbox_setup` is idempotent. Run it once per NetBox instance (or after
upgrading NVD) to create:

- Tags: `nvd-managed`, `nvd-edge`, `nvd-extras`, `nvd-isl`, `nvd-lag`,
  `nvd-irb`, `nvd-loopback`, `nvd-management`, `nvd-routed`.
- Custom fields, all prefixed `nvd_` so they can be cleanly removed
  again later. Sites, Devices, Interfaces, VLAN Groups, VLANs, VRFs,
  and Prefixes all gain the fields they need.
- Device Role objects (`leaf`, `spine`).
- Manufacturer `Nokia`, Platform `SR Linux`, and the device types used
  by the `3-stage-evpn-vxlan` reference design (e.g. `7220 IXR-D3L`).

```bash
python -m automation.builders.netbox_setup
```

Re-running the command is safe — it reconciles existing objects with
the desired schema rather than duplicating them. The one field NetBox
treats as immutable (`type` on a custom field) is intentionally never
patched.

---

## Step 2 — Seed the 3-stage reference design

`netbox_seed_dc1_3stage` loads the existing YAML inputs from
`validated-designs/3-stage-evpn-vxlan/inputs/`, runs them through the
real 3-stage design builder to produce a fully expanded `FabricIntent`,
and then writes that intent to NetBox via `netbox_writer.write`.

```bash
python -m automation.builders.netbox_seed_dc1_3stage
```

What gets created in NetBox:

- A Site `dc1` carrying the identity custom fields (`nvd_design`,
  `nvd_environment`, `spine_asn`, `leaf_asn_start`, …) and a single
  `nvd_config` JSON blob that holds the intent fields that do not map
  cleanly to native NetBox objects (routing policies, configlets,
  credentials, breakouts, EDA settings, …).
- Prefixes for loopbacks and the management subnet, site-scoped and
  role-tagged.
- Devices for every leaf and spine with their manufacturer / platform /
  device type / role / tags / ASN.
- Physical interfaces on every device, tagged `nvd-isl`, `nvd-edge`,
  `nvd-lag` or `nvd-routed` as appropriate, with cable-level
  terminations modelled as NetBox Cables.
- VLAN Groups and VLANs for every bridge domain; VRFs for every router
  (scoped back to the site through the `nvd_site` custom field).
- Virtual `dcim.Interface` rows (type `virtual`, tagged `nvd-irb`) for
  every IRB on every leaf where its VRF is deployed, with IP addresses
  attached via `ipam.IPAddress` (role `anycast` for EVPN-VXLAN IRBs).
- Virtual `dcim.Interface` rows (type `virtual`, tagged `nvd-routed`)
  for every routed subinterface — one per `RoutedInterfaceIntent` — with
  `parent` pointing at the underlying edge port and `vrf` pointing at
  the router.

The writer is idempotent: re-running it updates in place rather than
duplicating. Partially-populated prefixes (a crash mid-seed) are also
handled — a global lookup falls back in when a site-scoped lookup
misses.

---

## Step 3 — Build intent back out, or export it

The primary public entry point is the normal automation CLI. It now
accepts `--source netbox` alongside the existing `--design`:

```bash
# Construct FabricIntent from NetBox and stop (validation + pretty print)
python -m automation.deploy \
    --source netbox --site dc1 \
    --generate-only

# Same, but round-trip back to the canonical YAML format for inspection
python -m automation.deploy \
    --source netbox --site dc1 \
    --export-yaml /tmp/dc1-from-netbox/

# Compare the NetBox-derived YAML against the canonical fixtures
diff -u validated-designs/3-stage-evpn-vxlan/inputs/topology.yaml \
        /tmp/dc1-from-netbox/topology.yaml
diff -u validated-designs/3-stage-evpn-vxlan/inputs/services.yaml \
        /tmp/dc1-from-netbox/services.yaml
```

The diff should be empty except for ordering-only differences that the
builder normalises away anyway (links and lags come back sorted).

A dedicated unit test (`tests/unit/test_netbox_builder.py`) runs the
same round-trip against an in-memory NetBox fake on every `pytest`
invocation, so regressions here fail CI.

---

## Step 4 — Deploy (CLI)

Once the intent round-trips correctly, deploy from NetBox the same way
you would from YAML — just with a different `--source`:

```bash
# EDA deploy (default mode)
python -m automation.deploy \
    --source netbox --site dc1 \
    --mode eda

# Dry-run against live EDA (no apply)
python -m automation.deploy \
    --source netbox --site dc1 \
    --mode eda --dry-run

# See exactly what would change
python -m automation.deploy \
    --source netbox --site dc1 \
    --mode eda --diff

# Phase-scoped apply (topology only, services only, etc.)
python -m automation.deploy \
    --source netbox --site dc1 \
    --mode eda --phase topology

# Generate an Ansible project on disk instead
python -m automation.deploy \
    --source netbox --site dc1 \
    --mode ansible --generate-only
```

The engine prints a machine-readable `[NVD-DEPLOY-SUMMARY] { ... }`
JSON line at the end of every run. The NetBox Custom Script (next
step) keys off that line to decide success/failure.

---

## Step 5 — Trigger deploys from the NetBox UI

`netbox_custom_scripts/deploy_nvd_fabric.py` is a NetBox Custom Script
that wraps the CLI above. Operators select a Site from a drop-down,
pick `eda` or `ansible`, toggle dry-run / phases / prune, and click
**Run Script**.

Under the hood the script:

1. Marks `nvd_deployment_state = deploying` and `nvd_last_deploy_at =
   now` on the Site.
2. Shells out via `subprocess.Popen` to `python -m automation.deploy
   --source netbox --site <slug> ...` running under the NVD venv at
   `NVD_VENV` (default `/opt/nvd/.venv`) with the NVD repo at
   `NVD_REPO` (default `/opt/nvd`).
3. Streams every stdout / stderr line back to the Custom Script output
   pane in real time.
4. Parses the `[NVD-DEPLOY-SUMMARY]` JSON to update the Site custom
   fields (`nvd_deployment_state`, `nvd_last_deploy_txid`,
   `nvd_last_deploy_error`, `nvd_last_deploy_phase`) and writes a
   Journal Entry with the full summary.

### Install recipe (NetBox 4.5)

NetBox 4.5 tracks every custom script as a `ScriptModule` row in
Postgres. Dropping a `.py` file into `SCRIPTS_ROOT` does **not**
register it — you have to create the matching `ScriptModule` entry as
well. There is no `manage.py syncscripts` command any more (removed
in 4.x); the registration is done via the Django shell, the UI's
**+ Add** button under Customization → Scripts, or a `POST` to
`/api/extras/script-modules/`.

First, find where `SCRIPTS_ROOT` actually points. On stock NetBox
installations `/opt/netbox` is a version-suffixed symlink, so both of
these paths refer to the same directory:

```bash
readlink -f /opt/netbox
#   → /opt/netbox-4.5.6
sudo -u netbox /opt/netbox/venv/bin/python \
    /opt/netbox/netbox/manage.py shell -c \
    'from django.conf import settings; print(settings.SCRIPTS_ROOT)'
#   → /opt/netbox-4.5.6/netbox/scripts
```

Then install and register the script:

```bash
# 1. Install the file under SCRIPTS_ROOT, readable by the netbox user
sudo install -m 0644 -o netbox -g netbox \
    netbox_custom_scripts/deploy_nvd_fabric.py \
    /opt/netbox/netbox/scripts/

# 2. Register a ScriptModule entry pointing at it (once per new file).
#    This is what the "+ Add" button under Customization → Scripts does.
sudo -u netbox /opt/netbox/venv/bin/python \
    /opt/netbox/netbox/manage.py shell <<'PY'
from extras.models import ScriptModule
from core.choices import ManagedFileRootPathChoices
sm, created = ScriptModule.objects.get_or_create(
    file_root=ManagedFileRootPathChoices.SCRIPTS,
    file_path="deploy_nvd_fabric.py",
)
sm.sync_classes()   # imports the module, creates Script rows
print("ScriptModule:", sm, "created=", created,
      "scripts=", list(sm.scripts.values_list("name", flat=True)))
PY

# 3. Restart the RQ worker so it picks up the module and env vars.
#    (The web process auto-reloads the Python module on next request.)
sudo systemctl restart netbox-rq
```

After this, "Deploy NVD Fabric" appears under
**Customization → Scripts** in the UI. Sanity check from outside:

```bash
curl -sk -H "Authorization: Bearer $NETBOX_TOKEN" \
    https://srv9002/api/extras/scripts/ | jq '.results[].display'
# → "DeployFabric (deploy_nvd_fabric)"
```

If the script still doesn't show up, the most likely causes are:

- File not readable by the `netbox` user
  (`sudo -u netbox test -r /opt/netbox/netbox/scripts/deploy_nvd_fabric.py`).
- Python import error — run
  `sudo -u netbox /opt/netbox/venv/bin/python -c 'import sys;
  sys.path.insert(0, "/opt/netbox/netbox/scripts"); import
  deploy_nvd_fabric'` to see the traceback. NetBox catches the
  `ImportError` silently and just omits the script from the UI;
  `ScriptModule.error` holds the exception after `sync_classes()`.

The `netbox-rq` worker needs the NVD environment variables so that
the shell-out from the script can find the venv and reach EDA /
NetBox; the cleanest way is a systemd drop-in:

```ini
# /etc/systemd/system/netbox-rq.service.d/nvd.conf
[Service]
Environment=NVD_VENV=/opt/nvd/.venv
Environment=NVD_REPO=/opt/nvd
Environment=EDA_URL=https://srv0201:9443
Environment=EDA_USER=admin
Environment=EDA_PASSWORD=admin
Environment=NETBOX_URL=https://srv9002
Environment=NETBOX_TOKEN=nbt_...
```

The NVD repo and its venv just need to be readable by the `netbox`
user. We recommend:

```bash
sudo chown -R wds:netbox /opt/nvd
sudo chmod -R g+rX,o-rwx /opt/nvd
```

### State fields populated by the UI trigger

| Custom field              | Meaning                                           |
|---------------------------|---------------------------------------------------|
| `nvd_deployment_state`    | `draft` → `deploying` → `deployed` or `failed`    |
| `nvd_last_deploy_at`      | ISO-8601 timestamp of last attempt                |
| `nvd_last_deploy_txid`    | EDA transaction ID or Ansible run reference       |
| `nvd_last_deploy_error`   | Truncated error from the engine on failure        |
| `nvd_last_deploy_phase`   | Last phase the engine successfully completed      |

Every deploy also gets a Journal Entry on the Site with the full
summary and the tail of stdout / stderr, so you have history without
spelunking logs on the NetBox host.

---

## Step 6 — Reset between runs

`netbox_reset` selectively removes only content NVD created. It
identifies objects by the `nvd-managed` tag and custom fields by the
`nvd_` prefix — no unrelated NetBox content is touched.

```bash
# Preview what would be deleted (seeded data only)
python -m automation.builders.netbox_reset --scope data --dry-run

# Actually remove seeded data but keep schema (custom fields / roles /
# device types) — this is what you want between demo runs
python -m automation.builders.netbox_reset --scope data --yes

# Full reset, including schema — undoes netbox_setup as well
python -m automation.builders.netbox_reset --scope all --yes

# Scope the cleanup to a single site only (data scope only)
python -m automation.builders.netbox_reset \
    --scope data --site dc1 --yes
```

Cascade order: cables → interfaces → IP addresses → devices →
prefixes → VLANs → VLAN groups → VRFs → sites. `--scope all` extends
this to custom fields, choice sets, tags, device roles, platforms,
manufacturer, and device types.

If you ever need a full database rollback (e.g. NetBox has become
inconsistent after a partial delete), the nuclear option is a Postgres
snapshot before `netbox_setup` and a restore afterwards:

```bash
sudo -u postgres pg_dump netbox > /var/backups/netbox-pre-nvd.sql
# ... experiments ...
sudo -u postgres dropdb netbox
sudo -u postgres createdb netbox
sudo -u postgres psql netbox < /var/backups/netbox-pre-nvd.sql
sudo systemctl restart netbox netbox-rq
```

This is documented for completeness — in day-to-day use `netbox_reset
--scope all` is enough.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `RequestError: Invalid v1 token` | Using an `nbt_` token with an older `pynetbox` | `uv sync --extra netbox` to pick up `pynetbox>=7.3` (native v2 support) |
| `Duplicate prefix found in global table` | A previous seed crashed mid-run and left a global prefix | Re-run the seed — the writer now falls back to a global lookup and updates in place |
| `Changing the type of custom fields is not supported` | Two fields with the same name across object types declared with different types | Make sure `netbox_schema.py` uses globally unique names (`nvd_role` for interfaces, `nvd_prefix_role` for prefixes, etc.) |
| Cross-reference validation error after reading from NetBox | Edge interface names or LAG names mismatch YAML | The builder prefixes `EdgeInterfaceIntent.name` with the device name and deduplicates LAGs by their logical name; if this fails on real data the site is missing tags or cables |
| Custom Script doesn't appear in the UI after dropping the file | NetBox 4.5 requires a `ScriptModule` DB entry (there is no `syncscripts` manage command any more) | Create it via the **+ Add** button in Customization → Scripts, or via `ScriptModule.objects.get_or_create(...)` + `sync_classes()` from the Django shell — see the install recipe above |
| Custom Script runs but engine never starts | `netbox-rq` worker cannot reach the venv / repo | Check the systemd drop-in, `systemctl status netbox-rq`, and that `/opt/nvd` is group-readable by `netbox` |
| Journal entry reports "HTTP 401" when updating Site | pynetbox sets auth per-request but direct `requests` calls on the session need the header too | `netbox_client.make_client` now sets `Authorization` on `nb.http_session.headers` explicitly — pull the latest code |

---

## Reference

- Builder module: `automation/builders/netbox.py` (read path,
  NetBox → `FabricIntent`).
- Writer module: `automation/builders/netbox_writer.py` (write path,
  `FabricIntent` → NetBox).
- Shared schema constants: `automation/builders/netbox_schema.py`.
- Thin client wrapper (auth, TLS): `automation/builders/netbox_client.py`.
- Setup / seed / reset one-shots: `automation/builders/netbox_setup.py`,
  `netbox_seed_dc1_3stage.py`, `netbox_reset.py`.
- Custom Script for the UI trigger:
  `netbox_custom_scripts/deploy_nvd_fabric.py`.
- Dispatcher entry: `build_intent_from_netbox()` in
  `automation/core/fabric_builder.py`.
- CLI entry: `python -m automation.deploy --source netbox ...`
  (`automation/deploy.py`).
- Round-trip tests: `tests/unit/test_netbox_builder.py`.
