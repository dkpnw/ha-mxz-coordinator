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
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLAMP_MAX,
    CONF_CLAMP_MIN,
    CONF_DEMAND_THRESHOLD,
    CONF_ECO_COOL_MAX,
    CONF_ECO_HEAT_MIN,
    CONF_ENGAGE_DEADBAND,
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_PRIMARY_VANE_HORIZONTAL,
    CONF_PRIMARY_VANE_VERTICAL,
    CONF_RESTING_MODE_BIAS,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    CONF_SECONDARY_VANE_HORIZONTAL,
    CONF_SECONDARY_VANE_VERTICAL,
    DEFAULT_CLAMP_MAX,
    DEFAULT_CLAMP_MIN,
    DEFAULT_DEMAND_THRESHOLD,
    DEFAULT_ECO_COOL_MAX,
    DEFAULT_ECO_HEAT_MIN,
    DEFAULT_ENGAGE_DEADBAND,
    DEFAULT_MODE_HYSTERESIS,
    DEFAULT_RESTING_MODE_BIAS,
    DOMAIN,
    RESTING_BIAS_OPTIONS,
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


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRIMARY_CLIMATE): _CLIMATE_SELECTOR,
            vol.Required(CONF_SECONDARY_CLIMATE): _CLIMATE_SELECTOR,
            vol.Required(CONF_PRIMARY_SENSOR): _SENSOR_SELECTOR,
            vol.Required(CONF_SECONDARY_SENSOR): _SENSOR_SELECTOR,
            vol.Optional(CONF_NOTIFY_SERVICE): selector.TextSelector(),
            vol.Optional(CONF_PRIMARY_VANE_VERTICAL): _VANE_SELECTOR,
            vol.Optional(CONF_PRIMARY_VANE_HORIZONTAL): _VANE_SELECTOR,
            vol.Optional(CONF_SECONDARY_VANE_VERTICAL): _VANE_SELECTOR,
            vol.Optional(CONF_SECONDARY_VANE_HORIZONTAL): _VANE_SELECTOR,
        }
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    def _num() -> selector.NumberSelector:
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                mode=selector.NumberSelectorMode.BOX, step="any"
            )
        )

    return vol.Schema(
        {
            vol.Optional(
                CONF_DEMAND_THRESHOLD,
                default=current.get(CONF_DEMAND_THRESHOLD, DEFAULT_DEMAND_THRESHOLD),
            ): _num(),
            vol.Optional(
                CONF_ENGAGE_DEADBAND,
                default=current.get(CONF_ENGAGE_DEADBAND, DEFAULT_ENGAGE_DEADBAND),
            ): _num(),
            vol.Optional(
                CONF_MODE_HYSTERESIS,
                default=current.get(CONF_MODE_HYSTERESIS, DEFAULT_MODE_HYSTERESIS),
            ): _num(),
            vol.Optional(
                CONF_ECO_COOL_MAX,
                default=current.get(CONF_ECO_COOL_MAX, DEFAULT_ECO_COOL_MAX),
            ): _num(),
            vol.Optional(
                CONF_ECO_HEAT_MIN,
                default=current.get(CONF_ECO_HEAT_MIN, DEFAULT_ECO_HEAT_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MIN,
                default=current.get(CONF_CLAMP_MIN, DEFAULT_CLAMP_MIN),
            ): _num(),
            vol.Optional(
                CONF_CLAMP_MAX,
                default=current.get(CONF_CLAMP_MAX, DEFAULT_CLAMP_MAX),
            ): _num(),
            vol.Optional(
                CONF_RESTING_MODE_BIAS,
                default=current.get(
                    CONF_RESTING_MODE_BIAS, DEFAULT_RESTING_MODE_BIAS
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(RESTING_BIAS_OPTIONS),
                    translation_key="resting_mode_bias",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class MXZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config (household entity IDs)."""

    VERSION = 1

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
                return self.async_create_entry(
                    title="MXZ Coordinator", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(), errors=errors
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
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current)
        )
