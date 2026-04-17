"""Generic extras merge utility.

Provides a reusable merge-by-name function for overlaying user-provided
``extras`` overrides onto design-generated intent lists.  Each design
builder can call ``merge_by_name()`` instead of maintaining per-type
boilerplate merge functions.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


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
