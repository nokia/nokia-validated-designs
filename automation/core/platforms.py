"""
Platform registry for Nokia SR Linux devices.

Defines port layouts, breakout capabilities, and role constraints
for all supported 7220 IXR platforms.
"""

from __future__ import annotations

from pydantic import BaseModel


class BreakoutMode(BaseModel):
    """A supported breakout configuration for a port group."""

    channels: int  # e.g. 4
    speed: str  # e.g. "100G"


class PortGroup(BaseModel):
    """A contiguous group of ports with the same speed on a platform."""

    count: int
    speed: str  # "25G", "100G", "400G", "800G"
    start_index: int  # 1-based
    end_index: int  # inclusive
    breakout_modes: list[BreakoutMode] = []

    def port_indices(self) -> list[int]:
        """Return all port indices in this group."""
        return list(range(self.start_index, self.end_index + 1))


class PlatformSpec(BaseModel):
    """Full specification of a Nokia SR Linux platform."""

    name: str
    port_groups: list[PortGroup]
    allowed_roles: list[str]  # ["leaf", "spine"] or ["spine"]

    @property
    def total_ports(self) -> int:
        return sum(pg.count for pg in self.port_groups)

    @property
    def max_port_index(self) -> int:
        return max(pg.end_index for pg in self.port_groups)

    def all_port_indices(self) -> list[int]:
        """Return all port indices, ordered ascending."""
        indices = []
        for pg in self.port_groups:
            indices.extend(pg.port_indices())
        return sorted(indices)

    def port_group_for_index(self, index: int) -> PortGroup | None:
        """Find which port group a given port index belongs to."""
        for pg in self.port_groups:
            if pg.start_index <= index <= pg.end_index:
                return pg
        return None

    def supports_breakout(self, port_index: int) -> list[BreakoutMode]:
        """Return available breakout modes for a given port index."""
        pg = self.port_group_for_index(port_index)
        if pg is None:
            return []
        return pg.breakout_modes

    def validate_role(self, role: str) -> bool:
        """Check if this platform is allowed for the given role."""
        return role in self.allowed_roles


# ---------------------------------------------------------------------------
# Platform Registry
# ---------------------------------------------------------------------------

PLATFORM_REGISTRY: dict[str, PlatformSpec] = {}


def _register(spec: PlatformSpec) -> None:
    PLATFORM_REGISTRY[spec.name] = spec


# 7220 IXR-D2L: 48x25G + 8x100G
_register(
    PlatformSpec(
        name="7220 IXR-D2L",
        port_groups=[
            PortGroup(count=48, speed="25G", start_index=1, end_index=48),
            PortGroup(count=8, speed="100G", start_index=49, end_index=56),
        ],
        allowed_roles=["leaf"],
    )
)

# 7220 IXR-D3L: 32x100G
_register(
    PlatformSpec(
        name="7220 IXR-D3L",
        port_groups=[
            PortGroup(
                count=32,
                speed="100G",
                start_index=1,
                end_index=32,
                breakout_modes=[
                    BreakoutMode(channels=2, speed="50G"),
                    BreakoutMode(channels=4, speed="25G"),
                ],
            ),
        ],
        allowed_roles=["leaf", "spine"],
    )
)

# 7220 IXR-D4: 28x100G + 8x400G
_register(
    PlatformSpec(
        name="7220 IXR-D4",
        port_groups=[
            PortGroup(count=28, speed="100G", start_index=1, end_index=28),
            PortGroup(
                count=8,
                speed="400G",
                start_index=29,
                end_index=36,
                breakout_modes=[BreakoutMode(channels=4, speed="100G")],
            ),
        ],
        allowed_roles=["leaf", "spine"],
    )
)

# 7220 IXR-D5: 32x400G
_register(
    PlatformSpec(
        name="7220 IXR-D5",
        port_groups=[
            PortGroup(
                count=32,
                speed="400G",
                start_index=1,
                end_index=32,
                breakout_modes=[BreakoutMode(channels=4, speed="100G")],
            ),
        ],
        allowed_roles=["leaf", "spine"],
    )
)

# 7220 IXR-H4-32D: 32x400G (spine only)
_register(
    PlatformSpec(
        name="7220 IXR-H4-32D",
        port_groups=[
            PortGroup(
                count=32,
                speed="400G",
                start_index=1,
                end_index=32,
                breakout_modes=[BreakoutMode(channels=4, speed="100G")],
            ),
        ],
        allowed_roles=["spine"],
    )
)

# 7220 IXR-H4: 64x400G (spine only)
_register(
    PlatformSpec(
        name="7220 IXR-H4",
        port_groups=[
            PortGroup(
                count=64,
                speed="400G",
                start_index=1,
                end_index=64,
                breakout_modes=[BreakoutMode(channels=4, speed="100G")],
            ),
        ],
        allowed_roles=["spine"],
    )
)

# 7220 IXR-H5-64D: 64x800G (spine only)
_register(
    PlatformSpec(
        name="7220 IXR-H5-64D",
        port_groups=[
            PortGroup(
                count=64,
                speed="800G",
                start_index=1,
                end_index=64,
                breakout_modes=[
                    BreakoutMode(channels=8, speed="100G"),
                    BreakoutMode(channels=4, speed="200G"),
                ],
            ),
        ],
        allowed_roles=["spine"],
    )
)

# 7220 IXR-H5-64O: 64x800G OSFP (spine only)
_register(
    PlatformSpec(
        name="7220 IXR-H5-64O",
        port_groups=[
            PortGroup(
                count=64,
                speed="800G",
                start_index=1,
                end_index=64,
                breakout_modes=[
                    BreakoutMode(channels=8, speed="100G"),
                    BreakoutMode(channels=4, speed="200G"),
                ],
            ),
        ],
        allowed_roles=["spine"],
    )
)


def get_platform(name: str) -> PlatformSpec:
    """Look up a platform by name. Raises KeyError if not found."""
    if name not in PLATFORM_REGISTRY:
        valid = ", ".join(sorted(PLATFORM_REGISTRY.keys()))
        raise KeyError(f"Unknown platform '{name}'. Valid platforms: {valid}")
    return PLATFORM_REGISTRY[name]


def expand_breakout(base_interface: str, channels: int) -> list[str]:
    """
    Expand a base interface name into breakout channel names.

    Example: expand_breakout("ethernet-1-32", 4)
             → ["ethernet-1-32-1", "ethernet-1-32-2", "ethernet-1-32-3", "ethernet-1-32-4"]
    """
    return [f"{base_interface}-{ch}" for ch in range(1, channels + 1)]


def interface_name(port_index: int) -> str:
    """Convert a 1-based port index to SR Linux interface name."""
    return f"ethernet-1-{port_index}"
