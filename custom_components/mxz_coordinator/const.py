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
CONF_HEAT_LOCKOUT_FLOOR = "heat_lockout_floor"
CONF_COOL_LOCKOUT_CEILING = "cool_lockout_ceiling"
# Optional local-weather seasonal changeover (auto-drives the lockout switches).
CONF_CHANGEOVER_ENTITY = "changeover_entity"
CONF_CHANGEOVER_HEAT_ABOVE = "changeover_heat_above"
CONF_CHANGEOVER_COOL_BELOW = "changeover_cool_below"
# Optional delta-proportional "fan boost" (Tesla-style: bigger off-target -> faster fan).
CONF_FAN_BOOST_ENABLE = "fan_boost_enable"
CONF_FAN_BOOST_MAX = "fan_boost_max"

# --- Defaults (match packages/mxz_coordinator.yaml exactly) ---
DEFAULT_DEMAND_THRESHOLD = 3.0  # S — off-target °F before the SHARED MODE may flip
DEFAULT_ENGAGE_DEADBAND = 1.0  # D — off-target °F before a head actively runs
DEFAULT_MODE_HYSTERESIS = 600  # seconds minimum dwell before a heat<->cool flip
DEFAULT_ECO_COOL_MAX = 78.0  # away/eco cool extreme
DEFAULT_ECO_HEAT_MIN = 50.0  # away/eco heat extreme
DEFAULT_CLAMP_MIN = 59  # firmware min setpoint
DEFAULT_CLAMP_MAX = 88  # firmware max setpoint
DEFAULT_HEAT_LOCKOUT_FLOOR = 58.0  # heat-lockout safety floor: heat below this even if locked
DEFAULT_COOL_LOCKOUT_CEILING = 80.0  # cool-lockout safety ceiling: cool above this even if locked
DEFAULT_CHANGEOVER_HEAT_ABOVE = 68.0  # forecast daily high (°F) at/above -> heat-lockout on
DEFAULT_CHANGEOVER_COOL_BELOW = 50.0  # forecast daily high (°F) at/below -> cool-lockout on
CHANGEOVER_INTERVAL_MINUTES = 60  # how often to re-read the changeover weather signal

# --- Fan boost: drive the head's fan speed by how far the room is off-target ---
# The head's raw HA fan_modes list is UNSORTED. True airflow slowest->fastest is
#   quiet < low < medium < MIDDLE < high   (trap: "middle" is FASTER than "medium").
# "auto" is the firmware's own weak ramp we override. MAX = "high".
FAN_AUTO = "auto"
FAN_QUIET = "quiet"
FAN_LOW = "low"
FAN_MEDIUM = "medium"
FAN_MIDDLE = "middle"
FAN_HIGH = "high"
# Ascending true airflow speed; idx 0..4.
FAN_LADDER = (FAN_QUIET, FAN_LOW, FAN_MEDIUM, FAN_MIDDLE, FAN_HIGH)
# delta (°F off-target) >= up_at[i] to step UP into rung i+1
FAN_BOOST_UP_AT = (1.0, 2.0, 3.0, 4.0)
# up_at - 0.5 hysteresis; leave rung i once delta < down_at[i-1]
FAN_BOOST_DOWN_AT = (0.5, 1.5, 2.5, 3.5)
DEFAULT_FAN_BOOST_ENABLE = False
DEFAULT_FAN_BOOST_MAX = FAN_HIGH

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

# --- Unit profiles -----------------------------------------------------------
# The coordinator operates entirely in the Home Assistant SYSTEM temperature
# unit (room sensors and head setpoints already arrive in that unit). The
# decision math in logic.py is pure-numeric, so the only unit-dependent things
# are DEFAULT tunables, the setpoint band width, the eco edges, the target
# default, and the display resolution. A °F system is byte-for-byte unchanged
# (the °F profile reproduces every legacy DEFAULT_*); a °C system gets clean
# round metric values and 0.5° resolution instead of nonsensical °F numbers.
_UNIT_PROFILE_FAHRENHEIT = {
    "target_default": float(TARGET_DEFAULT),  # 70 °F
    "target_step": 1.0,
    "setpoint_band": 2.0,  # cool -> (t-2, t); heat -> (t, t+2)
    "eco_cool": (float(ECO_COOL_LOW), float(ECO_COOL_HIGH)),  # (76, 78)
    "eco_heat": (float(ECO_HEAT_LOW), float(ECO_HEAT_HIGH)),  # (59, 61)
    "fan_up_at": FAN_BOOST_UP_AT,  # (1, 2, 3, 4) °F off-target
    "fan_down_at": FAN_BOOST_DOWN_AT,  # (0.5, 1.5, 2.5, 3.5) °F hysteresis
    "defaults": {
        CONF_DEMAND_THRESHOLD: DEFAULT_DEMAND_THRESHOLD,
        CONF_ENGAGE_DEADBAND: DEFAULT_ENGAGE_DEADBAND,
        CONF_ECO_COOL_MAX: DEFAULT_ECO_COOL_MAX,
        CONF_ECO_HEAT_MIN: DEFAULT_ECO_HEAT_MIN,
        CONF_CLAMP_MIN: DEFAULT_CLAMP_MIN,
        CONF_CLAMP_MAX: DEFAULT_CLAMP_MAX,
        CONF_HEAT_LOCKOUT_FLOOR: DEFAULT_HEAT_LOCKOUT_FLOOR,
        CONF_COOL_LOCKOUT_CEILING: DEFAULT_COOL_LOCKOUT_CEILING,
        CONF_CHANGEOVER_HEAT_ABOVE: DEFAULT_CHANGEOVER_HEAT_ABOVE,
        CONF_CHANGEOVER_COOL_BELOW: DEFAULT_CHANGEOVER_COOL_BELOW,
    },
}
_UNIT_PROFILE_CELSIUS = {
    "target_default": 21.0,  # ~70 °F
    "target_step": 0.5,  # mini-splits accept 0.5 °C steps
    "setpoint_band": 1.0,  # cool -> (t-1, t); heat -> (t, t+1)
    "eco_cool": (24.0, 26.0),
    "eco_heat": (15.0, 16.0),
    "fan_up_at": (0.5, 1.0, 1.5, 2.0),  # °C off-target
    "fan_down_at": (0.25, 0.75, 1.25, 1.75),  # °C hysteresis
    "defaults": {
        CONF_DEMAND_THRESHOLD: 1.5,
        CONF_ENGAGE_DEADBAND: 0.5,
        CONF_ECO_COOL_MAX: 26.0,
        CONF_ECO_HEAT_MIN: 10.0,
        CONF_CLAMP_MIN: 15,
        CONF_CLAMP_MAX: 31,
        CONF_HEAT_LOCKOUT_FLOOR: 14.0,
        CONF_COOL_LOCKOUT_CEILING: 27.0,
        CONF_CHANGEOVER_HEAT_ABOVE: 20.0,
        CONF_CHANGEOVER_COOL_BELOW: 10.0,
    },
}


def unit_profile(celsius: bool) -> dict:
    """Return the tunable/resolution profile for the HA system temperature unit."""
    return _UNIT_PROFILE_CELSIUS if celsius else _UNIT_PROFILE_FAHRENHEIT

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
KEY_HEAT_LOCKOUT = "heat_lockout"
KEY_COOL_LOCKOUT = "cool_lockout"
KEY_SHARED_MODE = "shared_mode"
KEY_PLAN = "plan"
KEY_PRIMARY_THERMOSTAT = "primary_thermostat"
KEY_SECONDARY_THERMOSTAT = "secondary_thermostat"

UNAVAILABLE_STATES = ("unknown", "unavailable", "none", "")
