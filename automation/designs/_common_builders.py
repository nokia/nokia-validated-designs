"""
Shared builder helpers used by multiple design builders.

These helpers are passthrough transformations from raw dict input into
Pydantic intent models. They are design-agnostic — every design that
accepts the same input shape can reuse them.

Builders that vary across designs (e.g. bridge-domain default enrichment,
IRB dual-stack vs legacy field handling, the *content* of design-generated
configlets) intentionally remain in the per-design modules.
"""

from __future__ import annotations

import ipaddress

from automation.core.extras import ExtrasSpec, apply_extras
from automation.core.models import (
    BannerIntent,
    BridgeDomainIntent,
    ConfigletConfigEntry,
    ConfigletIntent,
    Credentials,
    DefaultMtuIntent,
    EdaSettings,
    EdgeInterfaceIntent,
    IrbInterfaceIntent,
    IrbIpAddress,
    LacpConfig,
    LagIntent,
    LagMember,
    NodeIntent,
    PolicyAction,
    PolicyMatch,
    PolicyStatementIntent,
    PrefixEntry,
    PrefixSetIntent,
    RoutedInterfaceIntent,
    RouterIntent,
    RoutingPolicyIntent,
    StaticRouteIntent,
    VlanIntent,
)


# ---------------------------------------------------------------------------
# Node-generation scaffolding shared by constrained designs
# ---------------------------------------------------------------------------


def validate_unique_names(nodes: list[NodeIntent]) -> None:
    """Raise ValueError if any two nodes share the same name."""
    seen: dict[str, int] = {}
    for node in nodes:
        if node.name in seen:
            raise ValueError(
                f"Duplicate node name '{node.name}': name_template must include "
                f"{{i}} placeholder to produce unique names"
            )
        seen[node.name] = 1


def validate_mgmt_ips(nodes: list[NodeIntent]) -> None:
    """Raise ValueError if any two nodes share the same mgmt_ipv4."""
    seen: dict[str, str] = {}
    for node in nodes:
        if not node.mgmt_ipv4:
            continue
        ip = str(ipaddress.IPv4Address(node.mgmt_ipv4))
        if ip in seen:
            raise ValueError(
                f"Duplicate management IP {ip}: "
                f"assigned to both '{seen[ip]}' and '{node.name}'"
            )
        seen[ip] = node.name


def apply_node_overrides(nodes: list[NodeIntent], overrides: list[dict]) -> None:
    """Apply per-node overrides to auto-generated nodes (in-place).

    Raises ValueError if an override references a node name that was not
    auto-generated.
    """
    by_name = {n.name: n for n in nodes}
    for ovr in overrides:
        name = ovr["name"]
        if name not in by_name:
            raise ValueError(
                f"Node override references unknown node '{name}'. "
                f"Auto-generated nodes: {sorted(by_name)}"
            )
        node = by_name[name]
        if "platform" in ovr:
            node.platform = ovr["platform"]
        if "version" in ovr:
            node.version = ovr["version"]
        if "mgmt_ipv4" in ovr:
            node.mgmt_ipv4 = ovr["mgmt_ipv4"]
        if "labels" in ovr:
            node.labels = {**node.labels, **ovr["labels"]}


def increment_ip(base_ip: str, offset: int) -> str:
    """Increment an IPv4 address by an offset."""
    return str(ipaddress.IPv4Address(base_ip) + offset)


def build_edge_interfaces(raw: list[dict]) -> list[EdgeInterfaceIntent]:
    """Build edge interface intents from raw input."""
    return [
        EdgeInterfaceIntent(
            name=ei["name"],
            node=ei["node"],
            interface=ei["interface"],
            encap=ei.get("encap", "dot1q"),
            labels=ei.get("labels", {}),
        )
        for ei in raw
    ]


def build_lags(raw: list[dict]) -> list[LagIntent]:
    """Build LAG intents from raw input."""
    lags: list[LagIntent] = []
    for lag in raw:
        lacp_cfg = lag.get("lacp", {})
        members = [
            LagMember(
                node=m["node"],
                interface=m["interface"],
                aggregate_id=m["aggregate_id"],
                lacp_port_priority=m.get("lacp_port_priority", 32768),
            )
            for m in lag.get("members", [])
        ]
        lags.append(
            LagIntent(
                name=lag["name"],
                type=lag.get("type", "lacp"),
                multihoming_mode=lag.get("mode", "all-active"),
                min_links=lag.get("min_links", 1),
                lacp=LacpConfig(
                    interval=lacp_cfg.get("interval", "fast"),
                    system_id_mac=lacp_cfg.get("system_id_mac", ""),
                    system_priority=lacp_cfg.get("system_priority", 32768),
                    admin_key=lacp_cfg.get("admin_key"),
                    fallback=lacp_cfg.get("fallback"),
                ),
                members=members,
                labels=lag.get("labels", {}),
                revertive=lag.get("revertive", False),
                preferred_active_node=lag.get("preferred_active_node", ""),
                standby_signaling=lag.get("standby_signaling", ""),
                reload_delay_timer=lag.get("reload_delay_timer", 100),
            )
        )
    return lags


def build_routers(raw: list[dict]) -> list[RouterIntent]:
    """Build router intents from raw input."""
    return [
        RouterIntent(
            name=r["name"],
            vni=r["vni"],
            evi=r["evi"],
            node_selector=r.get("node_selector", []),
            export_target=r.get("export_target"),
            import_target=r.get("import_target"),
        )
        for r in raw
    ]


def build_vlans(raw: list[dict]) -> list[VlanIntent]:
    """Build VLAN intents from raw input."""
    return [
        VlanIntent(
            name=v["name"],
            bridge_domain=v["bridge_domain"],
            vlan_id=str(v["vlan_id"]),
            interface_selector=v.get("interface_selector", []),
        )
        for v in raw
    ]


def build_routed_interfaces(raw: list[dict]) -> list[RoutedInterfaceIntent]:
    """Build routed interface intents from raw input."""
    return [
        RoutedInterfaceIntent(
            name=ri["name"],
            interface=ri["interface"],
            router=ri["router"],
            vlan_id=ri.get("vlan_id", "null"),
            ipv4_addresses=ri.get("ipv4_addresses", []),
            ipv6_addresses=ri.get("ipv6_addresses", []),
            ip_mtu=ri.get("ip_mtu", 1500),
            arp_timeout=ri.get("arp_timeout", 14400),
        )
        for ri in raw
    ]


def build_static_routes(raw: list[dict]) -> list[StaticRouteIntent]:
    """Build static route intents from raw input."""
    return [
        StaticRouteIntent(
            name=sr["name"],
            router=sr["router"],
            nodes=sr.get("nodes", []),
            prefixes=sr.get("prefixes", []),
            nexthop_group=sr.get("nexthop_group", {}),
        )
        for sr in raw
    ]


def build_default_mtus(raw: list[dict]) -> list[DefaultMtuIntent]:
    """Build default MTU intents from raw input."""
    return [
        DefaultMtuIntent(
            name=mtu["name"],
            interface_mtu=mtu.get("interface_mtu"),
            layer2_subif_mtu=mtu.get("layer2_subif_mtu"),
            layer3_mtu=mtu.get("layer3_mtu"),
            node_selector=mtu.get("node_selector", []),
            nodes=mtu.get("nodes", []),
        )
        for mtu in raw
    ]


def build_banners(raw: list[dict]) -> list[BannerIntent]:
    """Build banner intents from raw input."""
    return [
        BannerIntent(
            name=b["name"],
            login_banner=b.get("login_banner", ""),
            motd=b.get("motd", ""),
            node_selector=b.get("node_selector", []),
            nodes=b.get("nodes", []),
        )
        for b in raw
    ]


def build_prefix_entries(raw: list) -> list[PrefixEntry]:
    """Build prefix entries from raw input, passing through built models.

    Accepts already-built ``PrefixEntry`` instances so this can also service
    the ``merge_by_name`` overlay path, where a list may be partly converted.
    """
    entries: list[PrefixEntry] = []
    for p in raw:
        if isinstance(p, PrefixEntry):
            entries.append(p)
            continue
        entries.append(
            PrefixEntry(
                ip_prefix=p["ip_prefix"],
                mask_length_range=p.get("mask_length_range", "exact"),
            )
        )
    return entries


def build_prefix_sets(raw: list[dict]) -> list[PrefixSetIntent]:
    """Build prefix-set intents from raw input."""
    return [
        PrefixSetIntent(
            name=ps["name"],
            prefixes=build_prefix_entries(ps.get("prefixes", [])),
        )
        for ps in raw
    ]


def build_policy_statements(raw: list) -> list[PolicyStatementIntent]:
    """Build routing-policy statements from raw input.

    Accepts already-built ``PolicyStatementIntent`` instances so this can also
    service the ``merge_by_name`` overlay path.
    """
    statements: list[PolicyStatementIntent] = []
    for stmt in raw:
        if isinstance(stmt, PolicyStatementIntent):
            statements.append(stmt)
            continue
        m = stmt.get("match", {}) or {}
        a = stmt.get("action", {}) or {}
        statements.append(
            PolicyStatementIntent(
                name=str(stmt["name"]),
                match=PolicyMatch(
                    prefix_set=m.get("prefix_set"),
                    protocol=m.get("protocol"),
                    bgp_evpn_route_types=m.get("bgp_evpn_route_types"),
                ),
                action=PolicyAction(
                    result=a.get("result", "accept"),
                    set_local_preference=a.get("set_local_preference"),
                ),
            )
        )
    return statements


def build_routing_policies(raw: list[dict]) -> list[RoutingPolicyIntent]:
    """Build routing-policy intents from raw input."""
    return [
        RoutingPolicyIntent(
            name=rp["name"],
            default_action=rp.get("default_action", "reject"),
            statements=build_policy_statements(rp.get("statements", [])),
        )
        for rp in raw
    ]


# ---------------------------------------------------------------------------
# merge_by_name pre-process hooks
#
# ``merge_by_name`` overlays extras/user entries with Pydantic's
# ``model_copy(update=...)``, which does NOT validate or coerce nested values.
# Raw YAML dicts would therefore survive into the intent and blow up later in
# FabricIntent cross-reference validation, so nested lists must be converted
# up front.
# ---------------------------------------------------------------------------


def normalize_policy_update(fields: dict) -> dict:
    """Pre-process hook for merging user-supplied routing policies.

    Converts raw statement dicts into models, and forces ``internal=False`` so
    a user override of a design default (``internal=True``) becomes a regular
    user-declared policy that EDA emits.
    """
    fields["internal"] = False
    if fields.get("statements"):
        fields["statements"] = build_policy_statements(fields["statements"])
    return fields


def normalize_prefix_set_update(fields: dict) -> dict:
    """Pre-process hook for merging user-supplied prefix sets.

    Converts raw prefix dicts into models and forces ``internal=False`` for the
    same reason as :func:`normalize_policy_update`.
    """
    fields["internal"] = False
    if fields.get("prefixes"):
        fields["prefixes"] = build_prefix_entries(fields["prefixes"])
    return fields


def default_routing_policies(
    fabric_name: str, system0_prefix: str
) -> tuple[PrefixSetIntent, RoutingPolicyIntent, RoutingPolicyIntent]:
    """Return the eBGP ISL routing-policy artifacts a locked design requires.

    - ``prefixset-{fabric}`` matches the system0 loopback supernet on a /32 key.
    - ``ebgp-isl-export-policy-{fabric}`` accepts local/bgp/aggregate and the
      five EVPN route-types, setting local-preference to 100.
    - ``ebgp-isl-import-policy-{fabric}`` accepts bgp and the five EVPN
      route-types, setting local-preference to 100.

    Both policies default-reject. Users override any entry by name via
    ``topology.prefix_sets`` / ``services.routing_policies``.
    """
    prefix_set_name = f"prefixset-{fabric_name}"
    export_name = f"ebgp-isl-export-policy-{fabric_name}"
    import_name = f"ebgp-isl-import-policy-{fabric_name}"

    ps = PrefixSetIntent(
        name=prefix_set_name,
        prefixes=[PrefixEntry(ip_prefix=system0_prefix, mask_length_range="32..32")],
        internal=True,
    )

    accept = PolicyAction(result="accept", set_local_preference=100)
    evpn_route_types = (("25", 1), ("30", 2), ("35", 3), ("40", 4), ("45", 5))

    export_statements = [
        PolicyStatementIntent(
            name="10",
            match=PolicyMatch(prefix_set=prefix_set_name, protocol="local"),
            action=accept,
        ),
        PolicyStatementIntent(
            name="15", match=PolicyMatch(protocol="bgp"), action=accept
        ),
        PolicyStatementIntent(
            name="20", match=PolicyMatch(protocol="aggregate"), action=accept
        ),
    ]
    import_statements = [
        PolicyStatementIntent(
            name="10", match=PolicyMatch(protocol="bgp"), action=accept
        ),
    ]
    for statements in (export_statements, import_statements):
        statements.extend(
            PolicyStatementIntent(
                name=stmt_id,
                match=PolicyMatch(bgp_evpn_route_types=[rt]),
                action=accept,
            )
            for stmt_id, rt in evpn_route_types
        )

    return (
        ps,
        RoutingPolicyIntent(
            name=export_name,
            default_action="reject",
            statements=export_statements,
            internal=True,
        ),
        RoutingPolicyIntent(
            name=import_name,
            default_action="reject",
            statements=import_statements,
            internal=True,
        ),
    )


# ---------------------------------------------------------------------------
# Configlets
# ---------------------------------------------------------------------------


def build_configlets(raw: list[dict], *, origin: str = "") -> list[ConfigletIntent]:
    """Build configlet intents from raw input (explicit or extras-supplied)."""
    return [
        ConfigletIntent(
            name=c["name"],
            endpoint_selector=c.get("endpoint_selector", []),
            endpoints=c.get("endpoints", []),
            operating_system=c.get("operating_system", "srl"),
            priority=c.get("priority", 100),
            origin=origin,
            configs=[
                ConfigletConfigEntry(
                    path=cfg["path"],
                    operation=cfg.get("operation", "Update"),
                    config=cfg["config"],
                )
                for cfg in c.get("configs", [])
            ],
        )
        for c in raw
    ]


def merge_extras_configlets(
    design: list[ConfigletIntent], extras_raw: list[dict]
) -> list[ConfigletIntent]:
    """Merge user-provided extras configlets with design-generated ones.

    Configlets replace by name rather than overlaying field-by-field — a raw
    config patch is only meaningful as a whole — which is why they don't go
    through the generic ``apply_extras`` table.
    """
    by_name = {c.name: c for c in design}
    for cfglet in build_configlets(extras_raw, origin="extras"):
        by_name[cfglet.name] = cfglet
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Service extras
# ---------------------------------------------------------------------------


def _tag_extras_origin(fields: dict) -> dict:
    fields["origin"] = "extras"
    return fields


def service_extras_table(default_ip_mtu: int) -> dict[str, ExtrasSpec]:
    """Return the ``resource -> ExtrasSpec`` table for service extras.

    Adding a service resource to the extras contract means one entry here
    rather than a merge function per resource per design.
    """

    def irb_pre_process(fields: dict) -> dict:
        if "ip_addresses" in fields:
            fields["ip_addresses"] = [
                a if isinstance(a, IrbIpAddress) else IrbIpAddress(**a)
                for a in fields["ip_addresses"]
            ]
        fields.setdefault("ip_mtu", default_ip_mtu)
        fields["origin"] = "extras"
        return fields

    return {
        "bridge_domains": ExtrasSpec(BridgeDomainIntent, _tag_extras_origin),
        "irb_interfaces": ExtrasSpec(IrbInterfaceIntent, irb_pre_process),
        "routers": ExtrasSpec(RouterIntent),
        "vlans": ExtrasSpec(VlanIntent),
        "routed_interfaces": ExtrasSpec(RoutedInterfaceIntent),
        "static_routes": ExtrasSpec(StaticRouteIntent),
    }


def apply_service_extras(
    current: dict[str, list], extras: dict, *, default_ip_mtu: int
) -> dict[str, list]:
    """Overlay ``services.extras`` onto design-generated service lists.

    Keys of *current* must match the table in :func:`service_extras_table`.
    """
    return apply_extras(current, extras, service_extras_table(default_ip_mtu))


# ---------------------------------------------------------------------------
# Small shared derivations
# ---------------------------------------------------------------------------


def derive_default_ip_mtu(
    default_mtus: list[DefaultMtuIntent], fallback: int = 1500
) -> int:
    """Return the first declared layer-3 MTU, used as the default IRB ip_mtu."""
    for mtu in default_mtus:
        if mtu.layer3_mtu is not None:
            return mtu.layer3_mtu
    return fallback


def build_credentials(topology: dict) -> Credentials:
    """Build device credentials, falling back to the model defaults."""
    creds_cfg = topology.get("credentials", {})
    return Credentials(**creds_cfg) if creds_cfg else Credentials()


def build_eda_settings(topology: dict) -> EdaSettings:
    """Build EDA settings from the optional ``eda`` topology block."""
    eda_cfg = topology.get("eda", {})
    return EdaSettings(
        node_profile=eda_cfg.get("node_profile", ""),
        namespace=eda_cfg.get("namespace", "eda"),
    )
