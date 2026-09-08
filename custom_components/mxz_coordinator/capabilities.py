"""Read the Home Assistant capabilities advertised by coordinated heads."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import ATTR_FAN_MODES, ATTR_HVAC_MODES
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from .const import (
    FAN_AUTO,
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
)

_REQUIRED_MODES = frozenset((HVACMode.HEAT.value, HVACMode.COOL.value))
_IDLE_MODES = {
    IDLE_ACTION_FAN_ONLY: frozenset((HVACMode.FAN_ONLY.value,)),
    IDLE_ACTION_OFF: frozenset((HVACMode.OFF.value,)),
    IDLE_ACTION_OFF_AFTER_DRY: frozenset(
        (HVACMode.FAN_ONLY.value, HVACMode.OFF.value)
    ),
}


@dataclass(frozen=True)
class HeadCapabilities:
    """The service capabilities HA publishes in one climate entity state."""

    hvac_modes: frozenset[str] | None
    fan_modes: tuple[str, ...] | None


def head_capabilities(hass: HomeAssistant, entity_id: str) -> HeadCapabilities:
    """Read capabilities exactly as HA exposes them; unknown is not support."""
    state = hass.states.get(entity_id)
    if state is None:
        return HeadCapabilities(None, None)

    raw_hvac_modes = state.attributes.get(ATTR_HVAC_MODES)
    hvac_modes = (
        frozenset(str(mode) for mode in raw_hvac_modes)
        if isinstance(raw_hvac_modes, (list, tuple, set))
        else None
    )

    try:
        features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    except (TypeError, ValueError):
        features = 0
    raw_fan_modes = state.attributes.get(ATTR_FAN_MODES)
    fan_modes = None
    if features & ClimateEntityFeature.FAN_MODE and isinstance(
        raw_fan_modes, (list, tuple)
    ):
        cleaned = tuple(mode for mode in raw_fan_modes if isinstance(mode, str))
        if cleaned:
            fan_modes = cleaned
    return HeadCapabilities(hvac_modes, fan_modes)


def supported_idle_actions(
    hass: HomeAssistant, heads: Iterable[str]
) -> tuple[str, ...]:
    """Return parking policies supported by every selected head, in UI order."""
    capabilities = [head_capabilities(hass, head) for head in heads]
    if not capabilities or any(item.hvac_modes is None for item in capabilities):
        return ()
    return tuple(
        action
        for action, required in _IDLE_MODES.items()
        if all(
            item.hvac_modes is not None and required <= item.hvac_modes
            for item in capabilities
        )
    )


def head_mode_problem(
    hass: HomeAssistant,
    heads: Iterable[str],
    idle_action: str | None = None,
) -> tuple[str, dict[str, str]] | None:
    """Describe an unknown or unsupported required head mode for a flow error."""
    selected = tuple(heads)
    capabilities = {head: head_capabilities(hass, head) for head in selected}
    unknown = [head for head, item in capabilities.items() if item.hvac_modes is None]
    if unknown:
        return (
            "head_capabilities_unavailable",
            {"unsupported_heads": ", ".join(unknown)},
        )

    missing_required = [
        head
        for head, item in capabilities.items()
        if item.hvac_modes is None or not _REQUIRED_MODES <= item.hvac_modes
    ]
    if missing_required:
        return (
            "head_missing_heat_cool",
            {"unsupported_heads": ", ".join(missing_required)},
        )

    if idle_action is None:
        if supported_idle_actions(hass, selected):
            return None
        return (
            "head_missing_idle_modes",
            {"unsupported_heads": ", ".join(selected)},
        )

    required_idle = _IDLE_MODES.get(idle_action)
    missing_idle = [
        head
        for head, item in capabilities.items()
        if required_idle is None
        or item.hvac_modes is None
        or not required_idle <= item.hvac_modes
    ]
    if not missing_idle:
        return None
    alternatives = supported_idle_actions(hass, selected)
    return (
        "idle_action_unsupported",
        {
            "idle_action": idle_action,
            "unsupported_heads": ", ".join(missing_idle),
            "supported_idle_actions": ", ".join(alternatives) or "none",
        },
    )


def head_fan_modes(hass: HomeAssistant, entity_id: str) -> list[str] | None:
    """Return the exact settable fan options HA advertises, without renaming."""
    modes = head_capabilities(hass, entity_id).fan_modes
    return list(modes) if modes is not None else None


def head_has_fan_auto(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether the head exposes fan control with the token used for handback."""
    modes = head_capabilities(hass, entity_id).fan_modes
    return modes is not None and FAN_AUTO in modes
