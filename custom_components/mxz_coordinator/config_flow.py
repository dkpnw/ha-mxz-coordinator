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
    CONF_RESTING_MODE_BIAS,
    CONF_ZONES,
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
    MAX_ZONES,
    MIN_ZONES,
    RESTING_BIAS_OPTIONS,
    ZONE_CLIMATE,
    ZONE_NAME,
    ZONE_SENSOR,
    ZONE_VANE_HORIZONTAL,
    ZONE_VANE_VERTICAL,
    unit_profile,
    zone_slug,
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


# Flow-local key for the multi-head picker on the first step.
_CONF_HEADS = "heads"


def _user_schema(
    notify_options: list[str],
    default_heads: list[str] | None = None,
    default_notify: str | None = None,
) -> vol.Schema:
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
            vol.Required(_CONF_HEADS, default=default_heads or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                description={"suggested_value": default_notify},
            ): notify_selector,
        }
    )


def _sensors_schema(count: int) -> vol.Schema:
    """One room-temperature picker per chosen head (sensor_1..sensor_N)."""
    return vol.Schema(
        {vol.Required(f"sensor_{i + 1}"): _SENSOR_SELECTOR for i in range(count)}
    )


def _vane_field_keys() -> set[str]:
    """Every possible per-zone vane override key (flow-input-only, never stored flat)."""
    keys: set[str] = set()
    for i in range(MAX_ZONES):
        slug = zone_slug(i)
        keys.add(f"{slug}_vane_vertical")
        keys.add(f"{slug}_vane_horizontal")
    return keys


def _num() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
    )


def _options_schema(
    current: dict[str, Any], celsius: bool, zones: list[dict[str, Any]]
) -> vol.Schema:
    # Unset temperature tunables fall back to the system-unit profile (clean
    # metric values on a °C system, the legacy °F values otherwise); an
    # already-saved value always wins. Non-temperature keys aren't in the
    # profile, so they fall through to their plain DEFAULT_* below.
    eff = {**unit_profile(celsius)["defaults"], **current}

    # Vane selects are auto-detected at setup; expose per-zone overrides
    # (suggested_value shows what's currently wired).
    vane_fields: dict[Any, Any] = {}
    for i, zone in enumerate(zones):
        slug = zone_slug(i)
        vane_fields[
            vol.Optional(
                f"{slug}_vane_vertical",
                description={"suggested_value": zone.get(ZONE_VANE_VERTICAL)},
            )
        ] = _VANE_SELECTOR
        vane_fields[
            vol.Optional(
                f"{slug}_vane_horizontal",
                description={"suggested_value": zone.get(ZONE_VANE_HORIZONTAL)},
            )
        ] = _VANE_SELECTOR

    return _tunables_schema(eff).extend(vane_fields)


def _tunables_schema(eff: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_DEMAND_THRESHOLD,
                default=eff.get(CONF_DEMAND_THRESHOLD, DEFAULT_DEMAND_THRESHOLD),
            ): _num(),
            vol.Optional(
                CONF_ENGAGE_DEADBAND,
                default=eff.get(CONF_ENGAGE_DEADBAND, DEFAULT_ENGAGE_DEADBAND),
            ): _num(),
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

    VERSION = 2

    def __init__(self) -> None:
        self._heads: list[str] = []
        self._notify: str | None = None
        self._zones: list[dict[str, Any]] = []

    def _head_name(self, entity_id: str) -> str:
        """Friendly display name for a head (used as the zone name)."""
        state = self.hass.states.get(entity_id)
        if state is not None and state.name:
            return str(state.name)
        return entity_id.split(".", 1)[-1].replace("_", " ").title()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: pick 2..MAX_ZONES heads (first = highest standoff priority)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            heads: list[str] = user_input.get(_CONF_HEADS) or []
            if len(set(heads)) != len(heads):
                errors["base"] = "duplicate_heads"
            elif len(heads) < MIN_ZONES:
                errors["base"] = "need_two_heads"
            elif len(heads) > MAX_ZONES:
                errors["base"] = "too_many_heads"
            else:
                await self.async_set_unique_id("|".join(heads))
                self._abort_if_unique_id_configured()
                self._heads = heads
                self._notify = user_input.get(CONF_NOTIFY_SERVICE) or None
                return await self.async_step_sensors()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(_notify_options(self.hass)),
            errors=errors,
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: one room temperature sensor per chosen head."""
        if user_input is not None:
            zones: list[dict[str, Any]] = []
            for i, head in enumerate(self._heads):
                # Auto-detect each head's vane selects from its own device so
                # the user never has to pick them (overridable via Configure).
                vanes = _detect_vanes(self.hass, head)
                zones.append(
                    {
                        ZONE_NAME: self._head_name(head),
                        ZONE_CLIMATE: head,
                        ZONE_SENSOR: user_input[f"sensor_{i + 1}"],
                        ZONE_VANE_VERTICAL: vanes.get("vertical"),
                        ZONE_VANE_HORIZONTAL: vanes.get("horizontal"),
                    }
                )
            self._zones = zones
            return await self.async_step_tuning()

        heads_list = "\n".join(
            f"{i + 1}. {self._head_name(h)}" for i, h in enumerate(self._heads)
        )
        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensors_schema(len(self._heads)),
            description_placeholders={"heads": heads_list},
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: every tunable, pre-filled with unit-appropriate defaults.

        Nothing here is required — Submit as-is accepts the defaults. The same
        values stay editable later via the integration's Configure dialog.
        """
        if user_input is not None:
            data: dict[str, Any] = {CONF_ZONES: self._zones, **user_input}
            if self._notify:
                data[CONF_NOTIFY_SERVICE] = self._notify
            # Tunables live in options (with the data mirror above), exactly as
            # an options-flow save would leave them.
            return self.async_create_entry(
                title="MXZ Coordinator", data=data, options=dict(user_input)
            )
        celsius = (
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        eff = unit_profile(celsius)["defaults"]
        return self.async_show_form(
            step_id="tuning", data_schema=_tunables_schema(eff)
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure the heads / sensors of an existing entry in place (#7).

        Avoids the delete-and-re-add cycle (which also resurrects stale restore
        state) when a zone's sensor or head was mis-assigned at setup.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            heads: list[str] = user_input.get(_CONF_HEADS) or []
            if len(set(heads)) != len(heads):
                errors["base"] = "duplicate_heads"
            elif len(heads) < MIN_ZONES:
                errors["base"] = "need_two_heads"
            elif len(heads) > MAX_ZONES:
                errors["base"] = "too_many_heads"
            else:
                uid = "|".join(heads)
                if any(
                    e.unique_id == uid and e.entry_id != entry.entry_id
                    for e in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    return self.async_abort(reason="already_configured")
                self._heads = heads
                self._notify = user_input.get(CONF_NOTIFY_SERVICE) or None
                return await self.async_step_reconfigure_sensors()

        current = entry.data.get(CONF_ZONES, [])
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(
                _notify_options(self.hass),
                default_heads=[z[ZONE_CLIMATE] for z in current],
                default_notify=entry.data.get(CONF_NOTIFY_SERVICE),
            ),
            errors=errors,
        )

    async def async_step_reconfigure_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-head sensors for reconfigure, prefilled from the existing zones."""
        entry = self._get_reconfigure_entry()
        old_by_climate = {
            z[ZONE_CLIMATE]: z for z in entry.data.get(CONF_ZONES, [])
        }
        if user_input is not None:
            zones: list[dict[str, Any]] = []
            for i, head in enumerate(self._heads):
                old = old_by_climate.get(head)
                if old is not None:
                    # Unchanged head: keep its name + vane wiring (incl. any
                    # user overrides), update only the sensor.
                    zone = dict(old)
                    zone[ZONE_SENSOR] = user_input[f"sensor_{i + 1}"]
                else:
                    vanes = _detect_vanes(self.hass, head)
                    zone = {
                        ZONE_NAME: self._head_name(head),
                        ZONE_CLIMATE: head,
                        ZONE_SENSOR: user_input[f"sensor_{i + 1}"],
                        ZONE_VANE_VERTICAL: vanes.get("vertical"),
                        ZONE_VANE_HORIZONTAL: vanes.get("horizontal"),
                    }
                zones.append(zone)
            # Notify is always written (None included): data_updates merges
            # and can't delete a key, so clearing the field in the form must
            # store an explicit None to actually turn the alerts off.
            data_updates: dict[str, Any] = {
                CONF_ZONES: zones,
                CONF_NOTIFY_SERVICE: self._notify,
            }
            return self.async_update_reload_and_abort(
                entry,
                data_updates=data_updates,
                unique_id="|".join(self._heads),
            )

        # Prefill each slot with the head's current sensor where known.
        schema_fields = {}
        for i, head in enumerate(self._heads):
            old = old_by_climate.get(head)
            schema_fields[
                vol.Required(
                    f"sensor_{i + 1}",
                    description={
                        "suggested_value": old.get(ZONE_SENSOR) if old else None
                    },
                )
            ] = _SENSOR_SELECTOR
        heads_list = "\n".join(
            f"{i + 1}. {self._head_name(h)}" for i, h in enumerate(self._heads)
        )
        return self.async_show_form(
            step_id="reconfigure_sensors",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={"heads": heads_list},
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
        zones = [dict(z) for z in self.config_entry.data.get(CONF_ZONES, [])]
        vane_keys = _vane_field_keys()
        if user_input is not None:
            # Per-zone vane overrides are flow-input-only: fold them into the
            # zones list in entry.data, never store them as flat keys (a stale
            # flat key would shadow the zones list in the coordinator's
            # {**data, **options} merge).
            for i, zone in enumerate(zones):
                slug = zone_slug(i)
                for suffix, zkey in (
                    ("vane_vertical", ZONE_VANE_VERTICAL),
                    ("vane_horizontal", ZONE_VANE_HORIZONTAL),
                ):
                    if (value := user_input.get(f"{slug}_{suffix}")) is not None:
                        zone[zkey] = value or None
            tunables = {
                k: v for k, v in user_input.items() if k not in vane_keys
            }
            # Resilience: MERGE onto the existing options (a partial/empty submit
            # must never wipe the rest) and refuse to persist an empty set. Also
            # MIRROR the tuned config into entry.data — the coordinator reads
            # {**data, **options}, so if anything clears options out-of-band the
            # config self-recovers from the data mirror instead of silently
            # reverting to defaults. Legacy flat vane keys are scrubbed from
            # both stores (the zones list is authoritative now).
            merged = {
                k: v
                for k, v in {**self.config_entry.options, **tunables}.items()
                if k not in vane_keys
            }
            if not merged and not zones:
                return self.async_abort(reason="empty_options")
            data = {
                k: v
                for k, v in {**self.config_entry.data, **merged}.items()
                if k not in vane_keys
            }
            if zones:
                data[CONF_ZONES] = zones
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data=merged)
        current = {**self.config_entry.data, **self.config_entry.options}
        celsius = (
            self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS
        )
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current, celsius, zones)
        )
