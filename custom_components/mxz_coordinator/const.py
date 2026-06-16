"""Constants for the MXZ Coordinator integration."""

from __future__ import annotations

DOMAIN = "mxz_coordinator"

# climate is last so the number/switch siblings it drives are registered first.
PLATFORMS: list[str] = ["number", "switch", "select", "sensor", "climate"]

# --- Config-entry keys (collected in the config flow; household-specific) ---
CONF_PRIMARY_CLIMATE = "primary_climate"
CONF_SECONDARY_CLIMATE = "secondary_climate"
CONF_PRIMARY_SENSOR = "primary_sensor"
CONF_SECONDARY_SENSOR = "secondary_sensor"
CONF_NOTIFY_SERVICE = "notify_service"

# Optional vane `select` entities, mirrored onto the native thermostats so the
# single-target tile keeps vane control (replaces the echavet proxy's vane mode).
CONF_PRIMARY_VANE_VERTICAL = "primary_vane_vertical"
CONF_PRIMARY_VANE_HORIZONTAL = "primary_vane_horizontal"
CONF_SECONDARY_VANE_VERTICAL = "secondary_vane_vertical"
CONF_SECONDARY_VANE_HORIZONTAL = "secondary_vane_horizontal"

# --- Options keys (tunable constants; were hardcoded in the YAML package) ---
CONF_DEMAND_THRESHOLD = "demand_threshold"
CONF_ENGAGE_DEADBAND = "engage_deadband"
CONF_MODE_HYSTERESIS = "mode_hysteresis"
CONF_ECO_COOL_MAX = "eco_cool_max"
CONF_ECO_HEAT_MIN = "eco_heat_min"
CONF_CLAMP_MIN = "clamp_min"
CONF_CLAMP_MAX = "clamp_max"
CONF_RESTING_MODE_BIAS = "resting_mode_bias"

# --- Defaults (match packages/mxz_coordinator.yaml exactly) ---
DEFAULT_DEMAND_THRESHOLD = 3.0  # S — off-target °F before the SHARED MODE may flip
DEFAULT_ENGAGE_DEADBAND = 1.0  # D — off-target °F before a head actively runs
DEFAULT_MODE_HYSTERESIS = 600  # seconds minimum dwell before a heat<->cool flip
DEFAULT_ECO_COOL_MAX = 78.0  # away/eco cool extreme
DEFAULT_ECO_HEAT_MIN = 50.0  # away/eco heat extreme
DEFAULT_CLAMP_MIN = 59  # firmware min setpoint
DEFAULT_CLAMP_MAX = 88  # firmware max setpoint

# Resting-mode bias: which shared mode to settle on when NO room is calling.
#   "last" (default) -> hold whatever was last called (original behavior).
#   "cool"/"heat"    -> always rest in that mode, so the system won't sit idle in
#                       the wrong mode for the season. A genuine opposite demand
#                       (room past its demand threshold) still flips the mode; the
#                       bias only sets the *neutral* resting mode, it never blocks a call.
RESTING_BIAS_LAST = "last"
RESTING_BIAS_COOL = "cool"
RESTING_BIAS_HEAT = "heat"
RESTING_BIAS_OPTIONS = (RESTING_BIAS_LAST, RESTING_BIAS_COOL, RESTING_BIAS_HEAT)
DEFAULT_RESTING_MODE_BIAS = RESTING_BIAS_LAST

# Target setpoint number bounds (input_number hvac_*_target in the YAML)
TARGET_MIN = 55
TARGET_MAX = 85
TARGET_STEP = 1
TARGET_DEFAULT = 70

# Eco setpoint edges when a head runs in eco (from the actuator script)
ECO_COOL_HIGH = 78
ECO_COOL_LOW = 76
ECO_HEAT_HIGH = 61
ECO_HEAT_LOW = 59

# Heartbeat / drift re-assert interval (time_pattern "/15" in the YAML)
HEARTBEAT_MINUTES = 15

# Self-heal debounce windows (YAML `for:` durations)
BAND_DRIFT_DELAY = 20  # head in heat_cool/auto/dry this long -> re-apply
OFF_WHILE_ENABLED_DELAY = 30  # head off while enabled this long -> re-apply
STARTUP_RECOVER_DELAY = 40  # after HA start, wait this long then recompute

# Modes a head must never sit in (drift); the coordinator owns these transitions.
BANNED_MODES = ("heat_cool", "auto", "dry")

# Recompute trigger event (kept for echavet mitsubishi_climate_proxy interop)
EVENT_RECOMPUTE = "mxz_recompute"
SERVICE_RECOMPUTE = "recompute"

# Plan "state" / engage sentinel values
MODE_COOL = "cool"
MODE_HEAT = "heat"
MODE_FAN_ONLY = "fan_only"
MODE_OFF = "off"
DEMAND_NEUTRAL = "neutral"
ENGAGE_SATISFIED = "satisfied"

# Helper entity keys (used for unique_id suffixes and translation keys)
KEY_PRIMARY_TARGET = "primary_target"
KEY_SECONDARY_TARGET = "secondary_target"
KEY_PRIMARY_ENABLE = "primary_enable"
KEY_SECONDARY_ENABLE = "secondary_enable"
KEY_COORDINATOR_ENABLE = "coordinator_enable"
KEY_ECO_IDLE = "eco_idle"
KEY_SHARED_MODE = "shared_mode"
KEY_PLAN = "plan"
KEY_PRIMARY_THERMOSTAT = "primary_thermostat"
KEY_SECONDARY_THERMOSTAT = "secondary_thermostat"

UNAVAILABLE_STATES = ("unknown", "unavailable", "none", "")
