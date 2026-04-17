# NetBox Integration Design

Architecture for replacing YAML input files with NetBox as the source of
truth, triggering deployments from NetBox, and handling failure and drift.

---

## Integration Point

The current pipeline reads two flat YAML files and produces a
`FabricIntent`. The cleanest integration point is to replace the YAML
reader with a **NetBox builder** — a new module alongside
`three_stage_evpn_vxlan.py` and `unconstrained_3_stage.py` that queries
NetBox's REST API and produces the same `FabricIntent`. Everything
downstream (EDA generator, Ansible generator, clab generator, executor)
stays untouched.

```
                  YAML files                          NetBox API
                      |                                   |
                      v                                   v
            +------------------+              +------------------+
            |  Design Builder  |              |  NetBox Builder  |
            | (3-stage / uncon)|              | (queries NetBox) |
            +--------+---------+              +--------+---------+
                     |                                 |
                     +----------------+----------------+
                                      |
                                      v
                               FabricIntent
                                      |
                      +---------------+---------------+
                      |               |               |
                      v               v               v
                EDA Generator   Ansible Generator  Clab Generator
```

---

## Data Model Mapping

NetBox's model is device-centric and general-purpose; the current input
is fabric-centric and opinionated. The mapping uses NetBox native objects
where possible and custom fields or config contexts for fabric-specific
parameters.

### Topology (topology.yaml equivalent)

| Current Input | NetBox Object | Notes |
|---|---|---|
| `fabric_name` | **Site** (slug) | One Site per fabric |
| `design` | Custom field on Site | `nvd_design`: selection (`3-stage-evpn-vxlan`, `unconstrained-3-stage`) |
| `environment` | Custom field on Site | `nvd_environment`: selection (`containerlab`, `physical`) |
| `spine_asn` | Custom field on Site | Or derived from ASN range prefix |
| `leaf_asn_start` | Custom field on Site | Or derived from ASN range prefix |
| `system0_prefix` | **Prefix** (role=loopback) assigned to Site | |
| `mgmt_subnet` | **Prefix** (role=management) assigned to Site | |
| Spine/leaf nodes | **Devices** with Device Role `spine`/`leaf` at the Site | |
| Platform | **Device Type** on each Device | e.g., `7220 IXR-D3L` |
| SR Linux version | Custom field on Device | `srl_version`: text |
| Management IP | **IP Address** assigned to Device mgmt interface | |
| System0 IP | **IP Address** assigned to Device loopback interface | |
| Node labels | **Tags** on Device | Mapped to `eda.nokia.com/` label keys |
| ISL links | **Cables** between spine/leaf Interfaces | |
| Breakout config | Custom field on Interface | `breakout_mode`: text (e.g., `4x100G`) |

### Edge / Access

| Current Input | NetBox Object | Notes |
|---|---|---|
| Edge interfaces | **Interfaces** tagged `nvd-edge` | |
| Edge encap | Custom field on Interface | `encap`: selection (`dot1q`, `null`) |
| Edge labels | Tags on Interface | |
| LAG interfaces | **Interfaces** of type LAG + member assignment | |
| LAG parameters | Config context on LAG Interface | LACP system-id, min-links, mode, fallback |
| LAG labels | Tags on LAG Interface | |

### Services (services.yaml equivalent)

| Current Input | NetBox Object | Notes |
|---|---|---|
| Bridge domains | **VLAN Groups** at the Site | One VLAN Group per mac-vrf |
| Bridge domain VNI/EVI | Custom fields on VLAN Group | `vni`: integer, `evi`: integer |
| Bridge domain MAC settings | Config context on VLAN Group | mac_learning, mac_aging, mac_duplication |
| Routers (ip-vrfs) | **VRFs** | |
| Router VNI/EVI | Custom fields on VRF | `vni`: integer, `evi`: integer |
| Router node_selector | Tags on VRF | Mapped to label selectors |
| IRB interfaces | **Interfaces** of type Virtual on Devices, assigned to VRF | With IP Addresses |
| IRB parameters | Config context on Interface | proxy_arp, arp_timeout, ip_mtu, evpn settings |
| IRB bridge_domain ref | Custom field on Interface | Reference to VLAN Group name |
| VLANs | **VLANs** within a VLAN Group | |
| VLAN interface_selector | Tags on VLAN | Mapped to label selectors |
| Routed interfaces | **Interfaces** assigned to VRFs with IP Addresses | |
| Static routes | Config context on VRF | Prefixes, nexthop groups, node scoping |

### Siteinfo / Overrides

| Current Input | NetBox Object | Notes |
|---|---|---|
| Configlets | **Config contexts** on Devices or Device Roles | JSON blobs with path/operation/config |
| Default MTU | Custom fields on Site or config context | interface_mtu, layer2_subif_mtu, layer3_mtu |
| Banners | Config context on Site | login_banner, motd |
| Credentials | NetBox Secrets plugin or config context | username, password |
| EDA settings | Custom fields on Site | `eda_node_profile`, `eda_namespace` |

### Extras

The `extras` mechanism (unconstrained additions that overlay the locked
design) maps naturally to NetBox: objects tagged `nvd-extras` are merged
on top of design-generated resources using the same `merge_by_name()`
logic. The NetBox builder tags resources with `origin="extras"` when they
carry the extras tag.

---

## The NetBox Builder

A new module registered in `fabric_builder.py`:

```
automation/builders/
    netbox.py          # NetBox API --> FabricIntent
```

The builder queries NetBox for all objects at a given Site and
constructs the `FabricIntent`:

```python
def build(site_slug: str, netbox_url: str, netbox_token: str) -> FabricIntent:
    nb = pynetbox.api(netbox_url, token=netbox_token)
    site = nb.dcim.sites.get(slug=site_slug)

    # Devices --> NodeIntent
    devices = nb.dcim.devices.filter(site_id=site.id, role=["leaf", "spine"])
    nodes = [_device_to_node(d) for d in devices]

    # Cables between spine/leaf interfaces --> LinkIntent
    links = _build_links_from_cables(nb, site)

    # Prefixes --> system0, mgmt
    system0_prefix = _get_prefix(nb, site, role="loopback")
    mgmt_subnet = _get_prefix(nb, site, role="management")

    # VRFs --> RouterIntent
    vrfs = nb.ipam.vrfs.filter(tenant=site.tenant_id)
    routers = [_vrf_to_router(v) for v in vrfs]

    # VLAN Groups --> BridgeDomainIntent
    vlan_groups = nb.ipam.vlan_groups.filter(site_id=site.id)
    bridge_domains = [_vlan_group_to_bd(vg) for vg in vlan_groups]

    # VLANs --> VlanIntent
    vlans = [_vlan_to_intent(v) for v in nb.ipam.vlans.filter(site_id=site.id)]

    # Tagged interfaces --> EdgeInterfaceIntent
    edge_interfaces = _build_edge_interfaces(nb, site)

    # LAG interfaces --> LagIntent
    lags = _build_lags(nb, site)

    # IRB interfaces --> IrbInterfaceIntent
    irb_interfaces = _build_irbs(nb, site)

    # Config contexts --> ConfigletIntent
    configlets = _build_configlets_from_contexts(nb, site)

    # ... remaining resources ...

    return FabricIntent(
        design=site.custom_fields["nvd_design"],
        fabric_name=site.slug,
        environment=site.custom_fields.get("nvd_environment", "containerlab"),
        spine_asn=site.custom_fields["spine_asn"],
        leaf_asn_start=site.custom_fields["leaf_asn_start"],
        system0_prefix=system0_prefix,
        mgmt_subnet=mgmt_subnet,
        nodes=nodes,
        links=links,
        bridge_domains=bridge_domains,
        routers=routers,
        irb_interfaces=irb_interfaces,
        vlans=vlans,
        edge_interfaces=edge_interfaces,
        lags=lags,
        routed_interfaces=routed_interfaces,
        static_routes=static_routes,
        configlets=configlets,
        default_mtus=default_mtus,
        banners=banners,
        credentials=credentials,
        eda=eda_settings,
    )
```

The builder is where all NetBox-specific conventions live (which custom
fields map to what, how tags encode interface roles, etc.). The output is
a standard `FabricIntent`, so all generators and executors work without
modification.

### Conventions

The builder relies on a set of naming and tagging conventions in NetBox:

- **Device Roles** named `leaf` and `spine` (matching the NVD role model)
- **Tags** prefixed with `nvd-` for automation-specific metadata
  (e.g., `nvd-edge` on edge interfaces, `nvd-extras` on overlay objects)
- **Custom fields** prefixed with `nvd_` or domain-specific names
  (e.g., `vni`, `evi`, `srl_version`)
- **Config contexts** for complex structured data that does not fit custom
  fields (LAG parameters, configlet payloads, IRB advanced settings)

These conventions should be documented and enforced via NetBox custom
validation rules or a setup script that creates the required custom
fields, tags, and device roles.

---

## Deployment Triggering

### Why NOT Webhooks

Webhooks are the wrong primitive for deployment triggering for three
reasons:

1. **Atomicity** — A single logical change (add a new VLAN service)
   touches 4-5 NetBox objects (VLAN Group, VLAN, IP addresses, interface
   assignments). Webhooks fire per-object, so you would need to batch
   them with a debounce timer. This is fragile — how long do you wait?
   What if the operator takes a break between saving the VLAN and the
   IRB?

2. **Ordering** — The automation needs all related objects present before
   validation. A webhook for a new IRB fires before the bridge domain it
   references is created. You would need a queue with dependency
   resolution — at which point you have reinvented the state machine
   described below.

3. **Blast radius** — An accidental bulk edit in NetBox (e.g., changing
   a tag on 50 interfaces) would fire 50 webhooks and potentially
   trigger 50 deployments. The explicit staging model ensures a human
   decides when to deploy.

Webhooks are useful for one thing in this architecture:
**notification**. After a deployment completes (or fails), a webhook
from the orchestrator back to NetBox can update the deployment state
custom fields. But the deployment trigger should always be explicit.

### Approach: Explicit Deploy with a State Machine

Model the deployment lifecycle as a state machine on the Site:

```
DRAFT --> STAGED --> DEPLOYING --> DEPLOYED
                        |
                        v
                      FAILED
```

**DRAFT** — Someone is editing NetBox. No deployment runs. This is the
normal state during day-to-day changes. Objects can be created, modified,
and deleted freely.

**STAGED** — The operator signals "I am done editing, this is ready to
deploy." At this point, a validation step runs: the NetBox builder
queries all related objects, constructs the `FabricIntent`, and runs
Pydantic cross-reference validation. If validation fails, the state
returns to DRAFT with an error logged to the Site's journal.

**DEPLOYING** — The automation engine picks up the staged intent and
runs the deployment (EDA apply or Ansible playbook). The state field
prevents concurrent deployments for the same site.

**DEPLOYED** — Success. The timestamp, transaction ID, and a summary of
what changed are recorded on the Site.

**FAILED** — The transaction failed. Error details are recorded. The
state machine does not automatically retry — the operator must
investigate, fix, and re-stage.

### Implementation Options

#### Option A: NetBox Custom Script (simplest)

A NetBox custom script that an operator runs from the UI:

```python
class DeployFabric(Script):
    class Meta:
        name = "Deploy NVD Fabric"
        description = "Build intent from NetBox and deploy to EDA or Ansible"

    site = ObjectVar(model=Site, description="Fabric site to deploy")
    mode = ChoiceVar(choices=[("eda", "EDA"), ("ansible", "Ansible")])
    dry_run = BooleanVar(default=True, description="Validate only, do not apply")
    phases = MultiChoiceVar(
        choices=[("topology", "Topology"), ("fabric", "Fabric"), ("services", "Services")],
        required=False,
        description="Deploy specific phases (default: all)",
    )

    def run(self, data, commit):
        site = data["site"]

        # Update state
        site.custom_fields["deployment_state"] = "deploying"
        site.save()

        try:
            # Build intent from NetBox
            intent = netbox_builder.build(
                site.slug,
                netbox_url=settings.NETBOX_URL,
                netbox_token=self.request.user.api_token,
            )
            self.log_info(f"Built intent: {len(intent.nodes)} nodes, "
                         f"{len(intent.bridge_domains)} bridge domains")

            if data["mode"] == "eda":
                resources = eda_generate(intent)
                self.log_info(f"Generated {len(resources)} EDA CRs")

                if not data["dry_run"]:
                    client = EdaClient(url=..., username=..., password=...)
                    result = client.apply(resources, phases=data.get("phases"))

                    if result.success:
                        site.custom_fields["deployment_state"] = "deployed"
                        site.custom_fields["last_deploy_txid"] = result.transaction_id
                        self.log_success(f"Deployed: {result.message}")
                    else:
                        site.custom_fields["deployment_state"] = "failed"
                        site.custom_fields["last_deploy_error"] = result.message[:200]
                        self.log_failure(f"Failed: {result.message}")
                else:
                    site.custom_fields["deployment_state"] = "draft"
                    self.log_info("Dry run — no changes applied")

            elif data["mode"] == "ansible":
                output = ansible_generate(intent)
                site.custom_fields["deployment_state"] = "deployed"
                self.log_success(f"Generated Ansible project: {output}")

        except Exception as e:
            site.custom_fields["deployment_state"] = "failed"
            site.custom_fields["last_deploy_error"] = str(e)[:200]
            self.log_failure(f"Error: {e}")

        site.custom_fields["last_deploy_at"] = datetime.now().isoformat()
        site.save()
```

This gives operators a button in the NetBox UI, a dry-run option, and a
log trail — all within NetBox's existing interface.

#### Option B: External Orchestrator Polling for Staged State

A lightweight service (cron job, Kubernetes CronJob, or CI pipeline)
that periodically checks for sites in STAGED state:

```python
def reconcile():
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    sites = nb.dcim.sites.filter(cf_deployment_state="staged")

    for site in sites:
        site.custom_fields["deployment_state"] = "deploying"
        site.save()

        try:
            intent = netbox_builder.build(site.slug, NETBOX_URL, NETBOX_TOKEN)
            resources = eda_generate(intent)
            client = EdaClient(url=EDA_URL, username=EDA_USER, password=EDA_PASS)
            result = client.apply(resources)

            if result.success:
                site.custom_fields["deployment_state"] = "deployed"
                site.custom_fields["last_deploy_txid"] = result.transaction_id
            else:
                site.custom_fields["deployment_state"] = "failed"
                site.custom_fields["last_deploy_error"] = result.message[:200]

        except Exception as e:
            site.custom_fields["deployment_state"] = "failed"
            site.custom_fields["last_deploy_error"] = str(e)[:200]

        site.custom_fields["last_deploy_at"] = datetime.now().isoformat()
        site.save()
```

The staging trigger can be a simple NetBox custom field toggle, a custom
script that just sets the state to STAGED, or an API call from a CI
system.

#### Option C: GitOps Bridge

NetBox changes are exported to YAML (via the NetBox builder running in
export mode), committed to a git repo, and a CI pipeline runs the
deployment. This adds git as an audit trail and allows PR-based review
of changes before deployment.

```
NetBox --> NetBox Builder (export) --> YAML commit --> PR --> merge --> CI deploy
```

The NetBox-to-YAML export can run as a NetBox custom script or a
scheduled job. The resulting YAML files are identical to the current
`topology.yaml` and `services.yaml` format, so the existing pipeline
runs unmodified.

---

## Handling Failed Transactions and Drift

### Failure Scenario 1: Failed Deployment

The EDA transaction failed. NetBox says X, the network has the old state.

**Detection:** The deployment state machine records FAILED with the
transaction ID and error. The operator can see exactly what failed in
the Site's journal and custom fields.

**Resolution:** Fix the root cause (e.g., a node is unreachable, a
resource conflict in EDA) and re-deploy. The engine is idempotent — EDA
uses `replace` operations, Ansible uses `replace` subtree semantics.
Re-running the same intent converges to the desired state.

**Do NOT** automatically roll back NetBox to match the network. The
operator made deliberate changes in NetBox — the intent is correct, the
network just has not caught up yet.

### Failure Scenario 2: Partial Success

EDA's phased deployment succeeded on topology but failed on services.
The network is in an intermediate state.

**Detection:** The executor returns per-phase results. The orchestrator
records which phases succeeded:

```
last_deploy_phase: topology       # services failed
last_deploy_error: "IRB 'irb-v70' references unknown bridge_domain 'macvrf-v70'"
```

**Resolution:** Re-deploy with `--phase services` (or whichever phase
failed). The topology phase is skipped (TopoNodes already exist and are
synced), and the services phase retries.

### Failure Scenario 3: Out-of-Band Changes (Drift)

A network engineer made changes via CLI or EDA UI that are not reflected
in NetBox.

**Detection:** Use the `--diff` mode as a periodic reconciliation check.
The NetBox builder produces a `FabricIntent`, the EDA generator produces
CRs, and `compute_diff()` compares against live EDA state:

```python
def check_drift(site_slug: str):
    intent = netbox_builder.build(site_slug, NETBOX_URL, NETBOX_TOKEN)
    resources = eda_generate(intent)
    client = EdaClient(url=EDA_URL, username=EDA_USER, password=EDA_PASS)
    current = client.get_managed_resources(namespace=intent.eda.namespace)
    plan = client.compute_diff(resources, current)

    drift = {
        "missing_from_network": len(plan.creates),
        "different_from_intent": len(plan.updates),
        "not_in_intent": len(plan.deletes),
    }

    if plan.creates:
        # Resources in NetBox intent but not in EDA
        for cr in plan.creates:
            log.warning("MISSING: %s/%s", cr["kind"], cr["metadata"]["name"])

    if plan.deletes:
        # Resources in EDA but not in NetBox intent (out-of-band additions)
        for entry in plan.deletes:
            log.warning("EXTRA: %s/%s", entry["kind"], entry["name"])

    return drift
```

This can run on a schedule (e.g., hourly) and:
- Post drift reports to the Site journal in NetBox
- Update the `drift_detected` custom field
- Send alerts via NetBox's notification system

Drift detection and drift correction are separate decisions. The check
reports the discrepancy; a human decides whether to re-deploy (enforce
NetBox as truth) or update NetBox (accept the out-of-band change).

### State Tracking Custom Fields

Add these custom fields to the Site model in NetBox:

| Custom Field | Type | Purpose |
|---|---|---|
| `nvd_design` | Selection | Design identifier |
| `nvd_environment` | Selection | `containerlab` / `physical` |
| `spine_asn` | Integer | BGP AS for spine nodes |
| `leaf_asn_start` | Integer | Starting BGP AS for leaf nodes |
| `deployment_state` | Selection | `draft` / `staged` / `deploying` / `deployed` / `failed` |
| `deployment_mode` | Selection | `eda` / `ansible` |
| `last_deploy_at` | DateTime | Timestamp of last deployment attempt |
| `last_deploy_txid` | Text | EDA transaction ID or Ansible run ID |
| `last_deploy_error` | Text | Error message if failed |
| `last_deploy_phase` | Text | Last successfully completed phase |
| `last_drift_check` | DateTime | Last reconciliation check timestamp |
| `drift_detected` | Boolean | Whether drift was found |
| `eda_namespace` | Text | EDA namespace (default: `eda`) |
| `eda_node_profile` | Text | EDA NodeProfile name |

### Reconciliation Flow

```
                    +-------------------+
                    |  Periodic check   |
                    |  (cron / manual)  |
                    +---------+---------+
                              |
                    +---------v---------+
                    | NetBox Builder    |
                    | build FabricIntent|
                    +---------+---------+
                              |
                    +---------v---------+
                    | EDA Generator     |
                    | produce CRs       |
                    +---------+---------+
                              |
                    +---------v---------+
                    | EDA API           |
                    | get_managed_      |
                    | resources()       |
                    +---------+---------+
                              |
                    +---------v---------+
                    | compute_diff()    |
                    +---------+---------+
                              |
                  +-----------+-----------+
                  |                       |
            No drift                 Drift found
                  |                       |
            Update Site              Update Site
            last_drift_check         drift_detected=true
            drift_detected=false     Log details to journal
                                     Alert operator
```

---

## CLI Extension

Add a `--source netbox` option to `deploy.py`:

```bash
# Deploy from NetBox
python -m automation.deploy \
  --source netbox \
  --netbox-url https://netbox.example.com \
  --netbox-token $NETBOX_TOKEN \
  --site dc1 \
  --mode eda \
  --eda-url https://eda.example.com

# Diff NetBox intent against live EDA
python -m automation.deploy \
  --source netbox \
  --netbox-url https://netbox.example.com \
  --netbox-token $NETBOX_TOKEN \
  --site dc1 \
  --mode eda --diff \
  --eda-url https://eda.example.com

# Export NetBox intent to YAML (for inspection or GitOps)
python -m automation.deploy \
  --source netbox \
  --netbox-url https://netbox.example.com \
  --netbox-token $NETBOX_TOKEN \
  --site dc1 \
  --export-yaml validated-designs/dc1/inputs/

# Deploy from YAML (current behavior, unchanged)
python -m automation.deploy \
  --design validated-designs/3-stage-evpn-vxlan \
  --mode eda
```

The `--source` flag selects the input method. When omitted, the existing
`--design` path is used, preserving full backward compatibility.
