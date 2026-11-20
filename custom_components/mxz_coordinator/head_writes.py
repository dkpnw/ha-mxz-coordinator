"""MXZ's memory of its own mode and setpoint writes to one head, field by field. No Home Assistant.

Each field MXZ writes (``HEAD_WRITE_FIELDS``) has its own ``logic.HeadWriteMemory``. A write is
remembered just before it goes out (:func:`issue_steps`, :func:`remember_writes`), a call the head
refuses takes back what it counted (:func:`writes_refused`), a head's report passes the writes
before it (:func:`writes_caught_up`), and a person's change the adopt hold took replaces what the
head last showed (:func:`writes_pressed`). A report of a setpoint compares with a write on the head's own
grid (``grid.Grid.already_at``); the mode compares as it is.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Collection, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from .const import UNAVAILABLE_STATES
from .grid import Grid
from .logic import HeadWriteMemory, as_float

HEAD_WRITE_MODE = "hvac_mode"
HEAD_WRITE_FIELDS = (HEAD_WRITE_MODE, "temperature", "target_temp_low", "target_temp_high")


def field_shown(mode: str | None, attributes: Mapping[str, Any] | None, name: str) -> Any:
    """What a head showing ``mode`` and ``attributes`` shows for this field; None if nothing
    usable (no state, or unavailable)."""
    if mode is None or mode in UNAVAILABLE_STATES:
        return None
    if name == HEAD_WRITE_MODE:
        return mode
    return as_float((attributes or {}).get(name))


def same_as(name: str, grid: Grid) -> Callable[[Any, Any], bool]:
    """How a report of this field compares with a write of it."""
    return operator.eq if name == HEAD_WRITE_MODE else grid.already_at


def remember_writes(
    memories: MutableMapping[str, HeadWriteMemory],
    payload: Mapping[str, Any],
    *,
    mode: str | None,
    attributes: Mapping[str, Any] | None,
    grid: Grid,
    size: int,
) -> dict[str, Any]:
    """Keep each field a call's ``payload`` writes, while the head shows ``mode`` and
    ``attributes``; return the fields that counted a copy (``HeadWriteMemory.wrote``), which a
    refusal takes back."""
    counted: dict[str, Any] = {}
    for name in HEAD_WRITE_FIELDS:
        if name in payload:
            remembered = memories.setdefault(name, HeadWriteMemory(size))
            if remembered.wrote(
                payload[name], field_shown(mode, attributes, name), same_as(name, grid),
            ):
                counted[name] = payload[name]
    return counted


def writes_refused(memories: Mapping[str, HeadWriteMemory], counted: Mapping[str, Any]) -> None:
    """The head refused a call: the copies it counted (:func:`remember_writes`) will never
    show (``HeadWriteMemory.refused``)."""
    for name, value in counted.items():
        if name in memories:
            memories[name].refused(value)


@dataclass(frozen=True, slots=True)
class RememberWrites:
    """MXZ's write memory keeps each mode and setpoint field ``payload`` writes
    (:func:`remember_writes`), and what it counted, for a refusal."""

    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SendCall:
    """The call goes to the head. One the head refuses never shows: the copies
    :class:`RememberWrites` counted are taken back (:func:`writes_refused`)."""


def issue_steps(payload: Mapping[str, Any]) -> tuple[RememberWrites, SendCall]:
    """One call to a head with ``payload``, in order: its writes are remembered first, because
    the head's report can arrive while the call runs, then it is sent."""
    return (RememberWrites(payload), SendCall())


def writes_caught_up(
    memories: Mapping[str, HeadWriteMemory],
    *,
    old_mode: str | None,
    old_attributes: Mapping[str, Any] | None,
    mode: str | None,
    attributes: Mapping[str, Any] | None,
    grid: Grid,
) -> None:
    """A head's report: the writes before what it shows now are behind it for good
    (``HeadWriteMemory.caught_up``), for each field it shows; ``moved`` is whether the field
    changed since the report before."""
    for name, memory in memories.items():
        shown = field_shown(mode, attributes, name)
        if shown is not None:
            memory.caught_up(
                shown, same_as(name, grid),
                moved=shown != field_shown(old_mode, old_attributes, name),
            )


def writes_pressed(
    memories: Mapping[str, HeadWriteMemory],
    names: Collection[str],
    *,
    mode: str | None,
    attributes: Mapping[str, Any] | None,
    grid: Grid,
) -> None:
    """A person's change the adopt hold took (``planner.AdoptEnd.seen``): for each of these
    fields, what the head shows replaces what it last showed (``HeadWriteMemory.pressed``)."""
    for name in names:
        if name in memories:
            memories[name].pressed(field_shown(mode, attributes, name), same_as(name, grid))


def write_is_own(
    memories: Mapping[str, HeadWriteMemory], name: str, shown: Any, grid: Grid
) -> bool:
    """Whether MXZ's own writes of this field explain what the head shows."""
    memory = memories.get(name)
    return memory is not None and memory.own(shown, same_as(name, grid))
