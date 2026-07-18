"""Config and options flow for MXZ Coordinator."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_CHANGEOVER_COOL_BELOW,
    CONF_CHANGEOVER_ENTITY,
    CONF_CHANGEOVER_HEAT_ABOVE,
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_COOL_LOCKOUT_CEILING,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_FAN_BOOST_ENABLE,
    CONF_FAN_BOOST_MAX,
    CONF_HEAT_LOCKOUT_FLOOR,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_STAGE,
    CONF_PRIMARY_VANE_HORIZONTAL,
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_RESTING_MODE_BIAS,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_STAGE,
    CONF_SECONDARY_VANE_HORIZONTAL,
    CONF_SECONDARY_VANE_VERTICAL,
    DEFAULT_CHANGEOVER_COOL_BELOW,
    DEFAULT_CHANGEOVER_HEAT_ABOVE,
    DEFAULT_CLAMP_MAX,
    DEFAULT_CLAMP_MIN,
    DEFAULT_COOL_LOCKOUT_CEILING,
    DEFAULT_DEMAND_THRESHOLD,
    DEFAULT_ECO_COOL_MAX,
    DEFAULT_ECO_HEAT_MIN,
    DEFAULT_ENGAGE_DEADBAND,
    DEFAULT_FAN_BOOST_ENABLE,
    DEFAULT_FAN_BOOST_MAX,
    DEFAULT_HEAT_LOCKOUT_FLOOR,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DOMAIN,
    FAN_LADDER,
    RESTING_BIAS_OPTIONS,
    unit_profile,
)

_CLIMATE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="climate")
)
_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)
_VANE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="select")
)
# The stage/airflow sensor is a sensor (ESPHome text_sensors register in the
# `sensor` domain), so a sensor picker covers it.
_STAGE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)


def _notify_options(hass: HomeAssistant) -> list[str]:
    """Available `notify.*` targets, for a friendly drift-alert dropdown."""
    return sorted(f"notify.{name}" for name in hass.services.async_services().get("notify", {}))


def _detect_vanes(hass: HomeAssistant, climate_id: str) -> dict[str, str]:
    """Best-effort vane `select` entities on the SAME device as ``climate_id``.

    The CN105/ESPHome head and its vertical/horizontal vane selects live on one
    device, so we can infer them from the chosen head instead of asking the user.
    Returns {"vertical": eid, "horizontal": eid} for whatever is found.
    """
    reg = er.async_get(hass)
    entry = reg.async_get(climate_id)
    if entry is None or entry.device_id is None:
        return {}
    found: dict[str, str] = {}
    for e in er.async_entries_for_device(reg, entry.device_id, include_disabled_entities=True):
        if e.domain != "select":
            continue
        text = f"{e.entity_id} {e.original_name or ''} {e.name or ''}".lower()
        if "vane" not in text and "swing" not in text:
            continue
        if "horizontal" in text:
            found.setdefault("horizontal", e.entity_id)
        elif "vertical" in text:
            found.setdefault("vertical", e.entity_id)
        else:
            found.setdefault("vertical", e.entity_id)  # lone unlabeled vane -> vertical
    return found


def _detect_stage(hass: HomeAssistant, climate_id: str) -> str | None:
    """Best-effort actual-airflow (`stage`) sensor on the head's OWN device.

    CN105/ESPHome heads publish the decoded blower speed as a `stage`
    text_sensor (registers in the `sensor` domain) on the same device as the
    climate entity, so we can infer it from the chosen head — exactly like
    ``_detect_vanes``. Conservative: require the literal word "stage" in the
    entity_id or name so we never mistake an unrelated sensor for airflow.
    Returns the entity_id, or None if nothing qualifies.
    """
    reg = er.async_get(hass)
    entry = reg.async_get(climate_id)
    if entry is None or entry.device_id is None:
        return None
    for e in er.async_entries_for_device(reg, entry.device_id, include_disabled_entities=True):
        if e.domain != "sensor":
            continue
        text = f"{e.entity_id} {e.original_name or ''} {e.name or ''}".lower()
        if "stage" in text:
            return e.entity_id
    return None


def _user_schema(notify_options: list[str]) -> vol.Schema:
    notify_selector: selector.Selector = (
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=notify_options,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        if notify_options
        else selector.TextSelector()
    )
    return vol.Schema(
        {
            vol.Required(CONF_PRIMARY_CLIMATE): _CLIMATE_SELECTOR,
            vol.Required(CONF_SECONDARY_CLIMATE): _CLIMATE_SELECTOR,
            vol.Required(CONF_PRIMARY_SENSOR): _SENSOR_SELECTOR,
            vol.Required(CONF_SECONDARY_SENSOR): _SENSOR_SELECTOR,
            vol.Optional(CONF_NOTIFY_SERVICE): notify_selector,
        }
    )


def _options_schema(current: dict[str, Any], celsius: bool) -> vol.Schema:
    """Tunables + the vane + airflow-sensor overrides (the Configure dialog)."""
    override_fields: dict[Any, Any] = {
        vol.Optional(
            key, description={"suggested_value": current.get(key)}
        ): _VANE_SELECTOR
        for key in (
            CONF_PRIMARY_VANE_VERTICAL,
            CONF_PRIMARY_VANE_HORIZONTAL,
            CONF_SECONDARY_VANE_VERTICAL,
            CONF_SECONDARY_VANE_HORIZONTAL,
        )
    }
    for key in (CONF_PRIMARY_STAGE, CONF_SECONDARY_STAGE):
        override_fields[
            vol.Optional(key, description={"suggested_value": current.get(key)})
        ] = _STAGE_SELECTOR
    return _tunables_schema(current, celsius).extend(override_fields)


def _num() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
    )


def _tunables_schema(current: dict[str, Any], celsius: bool) -> vol.Schema:
    # Unset temperature tunables fall back to the system-unit profile (clean
    # metric values on a °C system, the legacy °F values otherwise); an
    # already-saved value always wins. Non-temperature keys aren't in the
    # profile, so they fall through to their plain DEFAULT_* below.
    profile = unit_profile(celsius)
    eff = {**profile["defaults"], **current}
    engage_min, engage_max = profile["engage_bounds"]

    return vol.Schema(
        {
            vol.Optional(
                CONF_DEMAND_THRESHOLD,
                default=eff.get(CONF_DEMAND_THRESHOLD, DEFAULT_DEMAND_THRESHOLD),
            ): _num(),
            vol.Optional(
                CONF_ENGAGE_DEADBAND,
                default=eff.get(CONF_ENGAGE_DEADBAND, DEFAULT_ENGAGE_DEADBAND),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    min=engage_min,
                    max=engage_max,
                    step=engage_min / 2,
                )
            ),
            vol.Optional(
                CONF_MODE_HYSTERESIS,
                default=eff.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS),
            ): _num(),
            vol.Optional(
                CONF_ECO_COOL_MAX,
                default=eff.get(CONF_ECO_COOL_MAX, DEFAULT_ECO_COOL_MAX),
            ): _num(),
            vol.Optional(
                CONF_ECO_HEAT_MIN,
                default=eff.get(CONF_ECO_HEAT_MIN, DEFAULT_ECO_HEAT_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MIN,
                default=eff.get(CONF_CLAMP_MIN, DEFAULT_CLAMP_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MAX,
                default=eff.get(CONF_CLAMP_MAX, DEFAULT_CLAMP_MAX),
            ): _num(),
            vol.Optional(
                CONF_RESTING_MODE_BIAS,
                default=eff.get(
                    CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(RESTING_BIAS_OPTIONS),
                    translation_key="resting_mode_bias",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_HEAT_LOCKOUT_FLOOR,
                default=eff.get(
                    CONF_HEAT_LOCKOUT_FLOOR, DEFAULT_HEAT_LOCKOUT_FLOOR
                ),
            ): _num(),
            vol.Optional(
                CONF_COOL_LOCKOUT_CEILING,
                default=eff.get(
                    CONF_COOL_LOCKOUT_CEILING, DEFAULT_COOL_LOCKOUT_CEILING
                ),
            ): _num(),
            vol.Optional(
                CONF_CHANGEOVER_ENTITY,
                description={"suggested_value": eff.get(CONF_CHANGEOVER_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["weather", "sensor"])
            ),
            vol.Optional(
                CONF_CHANGEOVER_HEAT_ABOVE,
                default=eff.get(
                    CONF_CHANGEOVER_HEAT_ABOVE, DEFAULT_CHANGEOVER_HEAT_ABOVE
                ),
            ): _num(),
            vol.Optional(
                CONF_CHANGEOVER_COOL_BELOW,
                default=eff.get(
                    CONF_CHANGEOVER_COOL_BELOW, DEFAULT_CHANGEOVER_COOL_BELOW
                ),
            ): _num(),
            vol.Optional(
                CONF_FAN_BOOST_ENABLE,
                default=eff.get(
                    CONF_FAN_BOOST_ENABLE, DEFAULT_FAN_BOOST_ENABLE
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_FAN_BOOST_MAX,
                default=eff.get(CONF_FAN_BOOST_MAX, DEFAULT_FAN_BOOST_MAX),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(FAN_LADDER),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class MXZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config (household entity IDs)."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the two heads, two temp sensors, and an optional notify service."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_PRIMARY_CLIMATE] == user_input[CONF_SECONDARY_CLIMATE]:
                errors["base"] = "same_head"
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_PRIMARY_CLIMATE]}|"
                    f"{user_input[CONF_SECONDARY_CLIMATE]}"
                )
                self._abort_if_unique_id_configured()
                # Auto-detect each head's vane selects from its own device so the
                # user never has to pick them (overridable later via Configure).
                data = dict(user_input)
                for climate_key, vkey, hkey, skey in (
                    (CONF_PRIMARY_CLIMATE, CONF_PRIMARY_VANE_VERTICAL,
                     CONF_PRIMARY_VANE_HORIZONTAL, CONF_PRIMARY_STAGE),
                    (CONF_SECONDARY_CLIMATE, CONF_SECONDARY_VANE_VERTICAL,
                     CONF_SECONDARY_VANE_HORIZONTAL, CONF_SECONDARY_STAGE),
                ):
                    vanes = _detect_vanes(self.hass, user_input[climate_key])
                    if "vertical" in vanes:
                        data[vkey] = vanes["vertical"]
                    if "horizontal" in vanes:
                        data[hkey] = vanes["horizontal"]
                    if stage := _detect_stage(self.hass, user_input[climate_key]):
                        data[skey] = stage
                self._base_data = data
                return await self.async_step_tuning()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(_notify_options(self.hass)),
            errors=errors,
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: every tunable, pre-filled with unit-appropriate defaults.

        Nothing here is required — Submit as-is accepts the defaults. The same
        values stay editable later via the integration's Configure dialog.
        """
        if user_input is not None:
            # Tunables live in options (mirrored into data), exactly as an
            # options-flow save would leave them.
            return self.async_create_entry(
                title="MXZ Coordinator",
                data={**self._base_data, **user_input},
                options=dict(user_input),
            )
        celsius = (
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        return self.async_show_form(
            step_id="tuning", data_schema=_tunables_schema({}, celsius)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MXZOptionsFlow:
        return MXZOptionsFlow()


class MXZOptionsFlow(OptionsFlow):
    """Tune the constants that were hardcoded in the YAML package."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Resilience: MERGE onto the existing options (a partial/empty submit
            # must never wipe the rest) and refuse to persist an empty set. Also
            # MIRROR the tuned config into entry.data — the coordinator reads
            # {**data, **options}, so if anything clears options out-of-band the
            # config self-recovers from the data mirror instead of silently
            # reverting to defaults.
            merged = {**self.config_entry.options, **user_input}
            if not merged:
                return self.async_abort(reason="empty_options")
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **merged}
            )
            return self.async_create_entry(title="", data=merged)
        current = {**self.config_entry.data, **self.config_entry.options}
        celsius = (
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current, celsius)
        )
