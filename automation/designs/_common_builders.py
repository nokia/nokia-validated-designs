"""
Shared builder helpers used by multiple design builders.

These helpers are passthrough transformations from raw dict input into
Pydantic intent models. They are design-agnostic — every design that
accepts the same input shape can reuse them.

Builders that vary across designs (e.g. bridge-domain default enrichment,
IRB dual-stack vs legacy field handling, design-specific configlet
generation) intentionally remain in the per-design modules.
"""

from __future__ import annotations

from automation.core.models import (
    BannerIntent,
    DefaultMtuIntent,
    EdgeInterfaceIntent,
    LacpConfig,
    LagIntent,
    LagMember,
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


def build_prefix_sets(raw: list[dict]) -> list[PrefixSetIntent]:
    """Build prefix-set intents from raw input."""
    return [
        PrefixSetIntent(
            name=ps["name"],
            prefixes=[
                PrefixEntry(
                    ip_prefix=p["ip_prefix"],
                    mask_length_range=p.get("mask_length_range", "exact"),
                )
                for p in ps.get("prefixes", [])
            ],
        )
        for ps in raw
    ]


def build_routing_policies(raw: list[dict]) -> list[RoutingPolicyIntent]:
    """Build routing-policy intents from raw input."""
    result: list[RoutingPolicyIntent] = []
    for rp in raw:
        statements: list[PolicyStatementIntent] = []
        for stmt in rp.get("statements", []):
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
        result.append(
            RoutingPolicyIntent(
                name=rp["name"],
                default_action=rp.get("default_action", "reject"),
                statements=statements,
            )
        )
    return result
