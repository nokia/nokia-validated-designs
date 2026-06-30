"""Generic extras merge utility.

Provides a reusable merge-by-name function for overlaying user-provided
``extras`` overrides onto design-generated intent lists.  Each design
builder can call ``merge_by_name()`` instead of maintaining per-type
boilerplate merge functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ExtrasSpec:
    """How to merge one ``extras`` resource list into a design-generated list.

    ``model_cls`` is the Pydantic intent model to instantiate for brand-new
    entries; ``pre_process`` is an optional per-raw-dict hook (e.g. to tag
    ``origin="extras"`` or coerce nested types).
    """

    model_cls: type[BaseModel]
    pre_process: Callable[[dict], dict] | None = None


def apply_extras(
    current: dict[str, list],
    extras: dict,
    table: dict[str, ExtrasSpec],
) -> dict[str, list]:
    """Merge each ``extras`` resource list into *current* per the *table*.

    For every ``resource -> ExtrasSpec`` entry, if ``extras[resource]`` is
    non-empty its overrides are merged by name into ``current[resource]`` via
    :func:`merge_by_name`. Resources absent from *extras* are left untouched.
    Returns a new dict; *current* is not mutated.

    Resources with bespoke semantics (e.g. configlets, which fully replace by
    name rather than overlay) are intentionally not table-driven and should be
    handled explicitly by the caller.
    """
    result = dict(current)
    for resource, spec in table.items():
        raw = extras.get(resource)
        if raw:
            result[resource] = merge_by_name(
                result[resource], raw, spec.model_cls, pre_process=spec.pre_process
            )
    return result


def merge_by_name(
    design: list[T],
    extras_raw: list[dict],
    model_cls: type[T],
    *,
    pre_process: Callable[[dict], dict] | None = None,
) -> list[T]:
    """Merge extras overrides into a design-generated list by name.

    For each entry in *extras_raw*:

    - If a design entry with the same ``name`` exists, overlay the extras
      fields using Pydantic's ``model_copy(update=...)``.
    - Otherwise, create a new instance of *model_cls* from the raw dict.

    Args:
        design: Design-generated intent list.
        extras_raw: Raw dicts from the ``extras`` section of the input.
        model_cls: Pydantic model class to instantiate for new entries.
        pre_process: Optional hook called on each raw dict before merging.
            Receives a mutable copy of the raw dict (with ``name`` already
            stripped for the update path) and should return the modified
            dict.  Useful for type conversions (e.g. ``IrbIpAddress``).

    Returns:
        Merged list preserving insertion order (design entries first,
        then new extras entries).
    """
    by_name: dict[str, T] = {item.name: item for item in design}  # type: ignore[attr-defined]

    for raw in extras_raw:
        name = raw["name"]
        update = {k: v for k, v in raw.items() if k != "name"}

        if pre_process is not None:
            update = pre_process(update)

        if name in by_name:
            by_name[name] = by_name[name].model_copy(update=update)
        else:
            by_name[name] = model_cls(name=name, **update)

    return list(by_name.values())
