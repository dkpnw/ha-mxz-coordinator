"""The base of the test heads: climate temperature names that work on every core.

HA 2026.11 renamed ClimateEntity's temperature members native_*
(native_temperature_unit, native_current_temperature, native_target_temperature
and the _low/_high pair). The old names still work until 2027.11, but HA logs a
deprecation for a class that provides one and for each call site that writes
one, and the test heads run inside commands MXZ sends, so those reports name
MXZ. HA 2024.12 to 2026.10 know only the old names.

So the test heads use only the native_ names. On a core that has them this
base adds nothing. On an older core it stores each native_ name under the old
one, the name that core reads, so a head behaves there exactly as it did when
it wrote the old names itself.
"""

from typing import Any

from homeassistant.components.climate import ClimateEntity

# Read from HA itself, not from the integration's own switch.
NATIVE = hasattr(ClimateEntity, "native_current_temperature")
NAMES = (
    "temperature_unit",
    "current_temperature",
    "target_temperature",
    "target_temperature_low",
    "target_temperature_high",
)


def _stored_as(old: str) -> property:
    """A native_ name read from and written to ``old``."""

    def write(self: Any, value: Any) -> None:
        setattr(self, old, value)

    return property(lambda self: getattr(self, old), write)


class HeadEntity(ClimateEntity):
    """A ClimateEntity for test heads that use only the native_ temperature names."""

    if not NATIVE:
        _attr_native_temperature_unit = _stored_as("_attr_temperature_unit")
        _attr_native_current_temperature = _stored_as("_attr_current_temperature")
        _attr_native_target_temperature = _stored_as("_attr_target_temperature")
        _attr_native_target_temperature_low = _stored_as("_attr_target_temperature_low")
        _attr_native_target_temperature_high = _stored_as("_attr_target_temperature_high")

        def __init_subclass__(cls, **kwargs: Any) -> None:
            """Move a head's native_ class attributes to the old names.

            This runs before the core's metaclass wraps the old names of the
            class being created, so it wraps the moved values as its own.
            """
            super().__init_subclass__(**kwargs)
            for name in NAMES:
                if (native := f"_attr_native_{name}") in vars(cls):
                    setattr(cls, f"_attr_{name}", vars(cls)[native])
                    delattr(cls, native)
