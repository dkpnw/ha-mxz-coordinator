"""Constants for the MXZ Coordinator integration."""

from __future__ import annotations

DOMAIN = "mxz_coordinator"

# climate is last of the platforms it drives, so the number/switch siblings are
# registered first. button carries no state of its own and follows them all.
PLATFORMS: list[str] = ["number", "switch", "select", "sensor", "climate", "button"]

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

# Optional per-zone "airflow" sensor: the actual blower speed some firmwares
# expose (CN105/ESPHome heads publish a `stage` text_sensor decoded from the
# 0x09 power packet). Mirrored onto the tile so it shows REAL airflow while the
# fan is handed to the firmware's own `auto` (display only — see climate.py).
CONF_PRIMARY_STAGE = "primary_stage"
CONF_SECONDARY_STAGE = "secondary_stage"

# --- N-zone config (entry VERSION 2) ---
# entry.data["zones"] = ordered list of zone dicts; LIST ORDER = standoff priority
# (index 0 = highest, the "primary wins" generalization).
CONF_ZONES = "zones"
ZONE_NAME = "name"
ZONE_CLIMATE = "climate"
ZONE_SENSOR = "sensor"
ZONE_VANE_VERTICAL = "vane_vertical"
ZONE_VANE_HORIZONTAL = "vane_horizontal"
ZONE_STAGE_SENSOR = "stage_sensor"
# Optional per-sensor reporting cadence (room-sensor freshness). The three
# durations are in MINUTES (like coil_dry_minutes), positive and finite; each is
# per SENSOR, never inferred from another room or from recent history.
#   report_interval  the cadence the sensor's own documentation promises (P)
#   max_age          an explicit maximum age, overriding the derived 3P
#   startup_grace    an explicit startup grace, overriding the derived max_age
# A room with NEITHER report_interval NOR max_age has an unknown cadence: its
# age is shown and never enforced. There is no universal cutoff.
ZONE_REPORT_INTERVAL = "report_interval"
ZONE_MAX_AGE = "max_age"
ZONE_STARTUP_GRACE = "startup_grace"
# The sensor's EVIDENCE BASIS: what this source can prove about a reading being
# current. It is INDEPENDENT of the cadence above and defaults to `unknown` — a
# duration alone never buys enforcement, because it says how often the source
# claims to report, not whether its writes are reports.
#   unknown           (default) age is shown, never enforced
#   ha_state_write    the source contract guarantees that EVERY accepted write,
#                     including the first one after setup or reload, is a
#                     current acquisition and can never replay a restored or
#                     cached value. Then the HA write itself is the evidence.
#   sample_timestamp  the source publishes a trustworthy marker as an entity
#                     attribute: either a sample TIME
#                     (`sample_timestamp_attribute`) or a monotonically
#                     advancing sample SEQUENCE (`sample_sequence_attribute`).
#                     Exactly one is named, and only an ADVANCING marker is
#                     evidence — repeated cache writes carrying the same marker
#                     move no deadline and recover nothing.
# A user or integration can still misstate that external contract, and a
# generic monitor cannot detect the mistake (docs/MIGRATION.md says so).
ZONE_EVIDENCE_BASIS = "evidence_basis"
# Opt-in per room; absent = off, so every entry stored before this option
# existed is opted out.
# Off: a disabled room's head is held off every cycle, as it always has been.
# On: disabling sends off until the head is seen off, then the coordinator
# leaves that head alone until the room is enabled again (someone can run it
# from its own remote), and its observed heat/cool is a low-priority vote.
ZONE_HANDS_OFF = "hands_off"
# Per room, what a released hands-off head's heat/cool vote follows. Stored
# only as VOTE_BASIS_ACTION; the default (VOTE_BASIS_MODE) removes the key, so
# a room that never chose stores exactly what it stored before this existed.
# mode: the head votes the heat or cool it is set to, as it always has.
# action: that vote counts only while the head's own hvac_action says it is
# running that way (heating in heat, cooling in cool), and for
# ACTION_VOTE_HOLD after it stops. A head never seen reporting an
# hvac_action since MXZ started votes by its mode.
ZONE_HANDS_OFF_VOTE_BASIS = "hands_off_vote_basis"
VOTE_BASIS_MODE = "mode"
VOTE_BASIS_ACTION = "action"
VOTE_BASES = (VOTE_BASIS_MODE, VOTE_BASIS_ACTION)
# Seconds an action-basis vote still counts after its head stops running:
# rides out the short idle gaps an inverter shows near its setpoint, and a
# head's slow hvac_action reports. Fixed (Drew's call: a 10-minute hold).
ACTION_VOTE_HOLD = 600
# Seconds after a head's hvac_mode is seen to change (any change: MXZ's own
# off on release, a remote, anything) during which its report of running is
# not yet a run for an action-basis vote: it counts only if the head still
# reports running when this ends. On hardware (2026-10-28 18:30, after MXZ ran
# the Bedroom head in heat and parked it off) a remote's cool was answered by
# "cooling" 0.11 s after the head showed cool (0.29 s after the call), a stale
# report that lasted 5.3 s, until idle; its real cooling began 100 s later.
# Every real start seen after a mode change that day came 10.6 to 16.7 s after
# the call. 15 s is almost three times the stale report's 5.3 s, and holds a
# real start back by at most about 4 s.
ACTION_VOTE_SETTLE = 15
# Optional per-zone binary_sensor: "this zone is asserting a safety override
# right now" (e.g. a freeze/overheat floor). Unset = this zone's Demand never
# carries PRIORITY_SAFETY. Settable on any zone regardless of enable/hands_off
# -- a coordinated room's own demand or a hands-off room's vote can both be
# marked safety; see logic.arbitrate().
ZONE_SAFETY_PRIORITY_ENTITY = "safety_priority_entity"
ZONE_SAMPLE_TIMESTAMP_ATTR = "sample_timestamp_attribute"
ZONE_SAMPLE_SEQUENCE_ATTR = "sample_sequence_attribute"
# Optional per-room freeze floor / overheat ceiling, in the system unit, acting
# only while the room is disabled. Absent = none: every entry stored before
# these keys existed has no floor, and a blank field stores no key.
ZONE_FLOOR_LOW = "floor_low"
ZONE_FLOOR_HIGH = "floor_high"
# Optional per-room uneven drift: this room's own "Let it get cooler by" /
# "Let it get warmer by" (the global CONF_DRIFT_* sides, for one room). Absent =
# none: every entry stored before these keys existed is unchanged, and a blank
# field stores no key. Clamped on load into the profile's engage_bounds.
ZONE_DRIFT_COOLER_BY = "drift_cooler_by"
ZONE_DRIFT_WARMER_BY = "drift_warmer_by"
# Opt-in per room: separate Heat to and Cool to instead of one target. Stored
# only while on (True); turning it off removes the key, so a room that never
# opted in, or opted out again, stores exactly what it stored before.
ZONE_DUAL_SETPOINT = "dual_setpoint"
# Per room: how MXZ reaches the head (beta). Stored only as HEAD_TYPE_CLOUD,
# or as HEAD_TYPE_LOCAL where the head's platform suggested cloud and the user
# kept local (in setup, or by switching a cloud room back); no key reads local, so every entry stored before this option
# existed, and every CN105 room, stores exactly what it stored before.
# local: today's path, unchanged (ESPHome/CN105 heads, and any other head).
# cloud: a head behind a cloud integration that polls (MELCloud, Kumo Cloud).
# MXZ sends it the mode on its own call, then one setpoint pushed past the
# target by the drive offset, at most one write batch per write interval, and
# reads its reports for a confirm wait after each batch (see coordinator.py).
# An ESPHome head is always local, whatever is stored.
ZONE_HEAD_TYPE = "head_type"
HEAD_TYPE_LOCAL = "local"
HEAD_TYPE_CLOUD = "cloud"
HEAD_TYPES = (HEAD_TYPE_LOCAL, HEAD_TYPE_CLOUD)
# Entity platforms the setup flow suggests cloud for (the room page doesn't);
# the user confirms. melcloud is Home Assistant's own; kumo is dlarrick/hass-kumo;
# kumo_cloud is jjustinwilson/comfort_HA (manifest domains, read 2026-10-29).
CLOUD_PLATFORMS = ("melcloud", "kumo", "kumo_cloud")
# A cloud room's tunables, stored only while the room is cloud and only when
# set to something other than the default (blank or the default stores no key,
# so the default follows the system unit). The drive offset is in the system
# unit; the two times are seconds.
ZONE_DRIVE_OFFSET = "drive_offset"
ZONE_WRITE_INTERVAL = "write_interval"
ZONE_CONFIRM_WAIT = "confirm_wait"
# Setpoint drive offset: while a cloud room runs, its head gets target + offset
# in heat and target - offset in cool, so the head's own thermistor doesn't
# stop it early; the room sensor still decides when the room is done. From
# @mikepell007's mxz_coordinator_blueprint (MIT). 0 sends the bare target.
DEFAULT_DRIVE_OFFSET_F = 2.0
DEFAULT_DRIVE_OFFSET_C = 1.0
DRIVE_OFFSET_MAX_F = 6.0
DRIVE_OFFSET_MAX_C = 3.0
# At most one write batch per cloud head per this many seconds; a newer
# intent waiting out the interval replaces the older one.
DEFAULT_WRITE_INTERVAL = 60
WRITE_INTERVAL_BOUNDS = (30, 300)
# After a batch, a cloud head is pending this many seconds: MXZ re-sends
# nothing it already sent and reads a report of an older state as stale.
DEFAULT_CONFIRM_WAIT = 90
CONFIRM_WAIT_BOUNDS = (30, 600)
# A cloud head's fan boost moves one rung at most per this many seconds.
CLOUD_FAN_STEP_SECONDS = 300
# A cloud head unavailable this long is not controllable: no writes, its
# room's call doesn't count (unless Safety priority is on), and a repair issue
# says so. One that is available but sends no report at all (not even an
# unchanged one) is the same after CLOUD_SILENT_SECONDS: Home Assistant's own
# MELCloud integration polls every 15 minutes (since 2026.2; 2024.12 polled
# every 60 s), so a quiet MELCloud head is silent for 15 minutes at a time,
# and that limit is two missed polls and five minutes more.
CLOUD_STALE_SECONDS = 900
CLOUD_SILENT_SECONDS = 2100
# The setpoint step a cloud head that publishes none is rounded to, per
# system unit.
CLOUD_STEP_F = 1.0
CLOUD_STEP_C = 0.5
# hass.data key of the options flow's one-shot handoff to the reload it
# triggers (unless that save is applied in place, which drops it):
# {(entry_id, head): {"seed_range": True} | {"target": t}}.
DUAL_HANDOFF_KEY = "mxz_coordinator_dual_handoff"
# The values the room page accepts, per unit system: (low min, low max, high
# min, high max). A floor outside these is rejected, never clamped.
FLOOR_RANGE_F = (35.0, 65.0, 75.0, 100.0)
FLOOR_RANGE_C = (2.0, 18.0, 24.0, 38.0)
# The no-progress cap (decision 11): this long actually driving the head
# without one step of new best reading ends the run as `blind`.
FLOOR_STALL_SECONDS = 2 * 3600
# A stalled run re-arms one step past its worst reading in this much of the
# driving before the stall (P9), not since its last gain: a dip earlier in
# the window cannot push the re-arm line far past the stall.
FLOOR_REARM_SECONDS = 3600
# A hands-off room that yielded a run to a person re-arms after this long
# (or once the room reaches the clear line).
FLOOR_YIELD_SECONDS = 4 * 3600
# At most one floor notice per room in this long, whatever the kind (P5).
FLOOR_NOTICE_SECONDS = 3600
EVIDENCE_UNKNOWN = "unknown"
EVIDENCE_HA_WRITE = "ha_state_write"
EVIDENCE_SAMPLE_TIMESTAMP = "sample_timestamp"
# The bases a setup form may offer; anything else reads as `unknown`.
EVIDENCE_BASES = (EVIDENCE_UNKNOWN, EVIDENCE_HA_WRITE, EVIDENCE_SAMPLE_TIMESTAMP)
MIN_ZONES = 2
MAX_ZONES = 8

# The maximum age derived from a reporting interval: a room goes stale at the
# THIRD due instant, so two missed due reports are tolerated and no more.
STALE_MISSED_REPORTS = 3

# Room-sensor health (per-zone plan key and `zones` view field).
#   healthy          fresh qualifying evidence, deadline still in the future
#   awaiting_report  provisionally eligible during the startup grace; the
#                    current value is NOT evidence this incarnation witnessed
#   stale            the configured maximum age was reached: no automatic demand
#   invalid          missing/unavailable/non-numeric/non-finite (M13's reader)
#   cadence_unknown  no cadence configured: age is shown, never enforced
# How far a room's head vote can be trusted right now (the plan's vote_status).
# live: the head is in sight. stale: out of sight (unavailable or gone), its
# last-known vote still counts. expired: out of sight for the whole TTL, so
# that vote no longer counts. unknown: not seen since MXZ started.
VOTE_LIVE = "live"
VOTE_STALE = "stale"
VOTE_EXPIRED = "expired"
VOTE_UNKNOWN = "unknown"

HEALTH_HEALTHY = "healthy"
HEALTH_AWAITING = "awaiting_report"
HEALTH_STALE = "stale"
HEALTH_INVALID = "invalid"
HEALTH_CADENCE_UNKNOWN = "cadence_unknown"
# The states that make a room ineligible for automatic demand. One continuous
# run of them is ONE unhealthy episode, however the subtype changes inside it.
HEALTH_UNHEALTHY = (HEALTH_STALE, HEALTH_INVALID)
# The states in which an unchanged report can still change something: an
# unhealthy room recovers on one, and a provisional room replaces its startup
# grace with that report's own evidence deadline. A healthy or unknown-cadence
# room learns nothing from one, so its writes never wake the coordinator.
HEALTH_REPORT_SENSITIVE = (HEALTH_STALE, HEALTH_INVALID, HEALTH_AWAITING)
# The eligible states a recovery can land in: an enforcement-capable room comes
# back to `healthy`, an unknown-basis one only to `cadence_unknown`.
HEALTH_ELIGIBLE = (HEALTH_HEALTHY, HEALTH_CADENCE_UNKNOWN)


def zone_slug(index: int) -> str:
    """Stable per-zone slug used in unique_ids and plan keys.

    Zones 0/1 keep the legacy "primary"/"secondary" slugs so existing 2-zone
    installs migrate with ZERO entity-registry churn; zones beyond get zone_N
    (1-based, so the third zone is ``zone_3``).
    """
    if index == 0:
        return "primary"
    if index == 1:
        return "secondary"
    return f"zone_{index + 1}"

# --- Options keys (tunable constants; were hardcoded in the YAML package) ---
CONF_DEMAND_THRESHOLD = "demand_threshold"
CONF_ENGAGE_DEADBAND = "engage_deadband"
# Optional uneven drift: how far below target before heat starts, and above
# target before cool starts. No default and no key when blank: blank means
# CONF_ENGAGE_DEADBAND, so an install that never sets them runs as before.
CONF_DRIFT_COOLER_BY = "drift_cooler_by"
CONF_DRIFT_WARMER_BY = "drift_warmer_by"
CONF_MODE_HYSTERESIS = "mode_hysteresis"
# Optional: how long, in minutes, a head MXZ has lost sight of (unavailable or
# gone) keeps its last-known vote. No key when blank: blank means
# DEFAULT_LOST_VOTE_TTL, so an install that never sets it stores nothing.
CONF_LOST_VOTE_TTL = "lost_vote_ttl"
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
# Minutes a lost head's last-known vote still counts (stale), then it expires.
# The default of @csingley's ha-climate-zone-sequencer (DEFAULT_LAST_KNOWN_TTL).
DEFAULT_LOST_VOTE_TTL = 30
LOST_VOTE_TTL_BOUNDS = (1, 240)  # minutes, the UI's and the load clamp's range
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
# ON by default since v2.10.0/v3.0.0 (opt-out via the Configure dialog) — it's simply a
# better experience than the firmware's weak "auto" ramp. An explicitly-saved
# False in an entry's options is always honored.
DEFAULT_FAN_BOOST_ENABLE = True
DEFAULT_FAN_BOOST_MAX = FAN_HIGH

# --- Airflow (stage) sensor -> settable fan token, for LIVE display only ------
# CN105/ESPHome heads publish the actual blower speed as a `stage` text_sensor
# (decoded from the 0x09 power packet — see echavet MitsubishiCN105ESPHome
# README ~line 902, "actual fan speed of the indoor unit"). It reports SEVEN
# granular stages, but the head only accepts FIVE settable speeds, so this map
# is approximate & monotonic (7-vs-5 mismatch: two stages collapse onto `quiet`,
# and DIFFUSE — the low-airflow wide-throw mode — maps to `low`, not a top rung).
# Values arrive uppercase from ESPHome; we normalize case/whitespace before
# lookup. Used ONLY to make the tile honest while the fan is in firmware `auto`;
# it never influences what the coordinator commands.
STAGE_TO_FAN = {
    "IDLE": FAN_QUIET,
    "LOW": FAN_QUIET,
    "GENTLE": FAN_LOW,
    "MEDIUM": FAN_MEDIUM,
    "MODERATE": FAN_MIDDLE,
    "HIGH": FAN_HIGH,
    "DIFFUSE": FAN_LOW,
}

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

# --- External inhibit / low-power standby (grid-down, load-shed, DR) ----------
# An optional watched entity (a grid-status binary_sensor, a load-shed switch, a
# "vacation" boolean, ...): while it reads ``inhibit_active_state`` the
# coordinator parks its heads in a low-power hold and self-restores on release.
# Orthogonal to the enable kill-switch, so there is NO prior state to snapshot.
# A dropout of the watched entity (unavailable/unknown/missing) reads as NOT
# held — fail toward normal coordination; a stuck sensor must never park the
# house.
CONF_INHIBIT_ENTITY = "inhibit_entity"
CONF_INHIBIT_ACTIVE_STATE = "inhibit_active_state"
CONF_INHIBIT_ACTION = "inhibit_action"
INHIBIT_ACTION_OFF = "off"  # zero draw, no protection
INHIBIT_ACTION_FAN_ONLY = "fan_only"  # circulate only
INHIBIT_ACTION_ECO = "eco"  # protection-only band: low draw AND freeze-safe
INHIBIT_ACTION_OPTIONS = (
    INHIBIT_ACTION_OFF,
    INHIBIT_ACTION_FAN_ONLY,
    INHIBIT_ACTION_ECO,
)
DEFAULT_INHIBIT_ACTION = INHIBIT_ACTION_ECO  # freeze-safe beats lowest-draw
DEFAULT_INHIBIT_ACTIVE_STATE = "on"  # grid sensors are often inverted -> "off"

# --- Idle action: how a non-running head parks ---------------------------------
# A satisfied room (or a standoff loser parked by a shared-mode mismatch) parks
# by this action. fan_only keeps air moving; on a wet coil after cooling, that
# airflow carries the coil smell into the room — "off" stops the fan and lets
# the head close its vanes instead. NOTE: "off" is NOT a tighter refrigerant seal; MXZ outdoor
# units bleed refrigerant through idle heads either way (oil return, and the
# SW5-7/8 dip options) — the only thing this changes is airflow.
# "off_after_dry" idles in fan_only for a coil-dry period after ACTIVE COOLING
# (heat leaves no wet coil), then powers off — so the coil isn't sealed wet.
# New entries default to off_after_dry. Entries saved before that default
# (which never stored a choice) migrate to an explicit fan_only, so an upgrade
# does not change how an existing install idles (see async_migrate_entry).
CONF_IDLE_ACTION = "idle_action"
IDLE_ACTION_FAN_ONLY = "fan_only"  # circulate (the original default)
IDLE_ACTION_OFF = "off"  # fan stops, vanes close
IDLE_ACTION_OFF_AFTER_DRY = "off_after_dry"  # dry the coil first, then off
IDLE_ACTION_OPTIONS = (
    IDLE_ACTION_FAN_ONLY,
    IDLE_ACTION_OFF,
    IDLE_ACTION_OFF_AFTER_DRY,
)
DEFAULT_IDLE_ACTION = IDLE_ACTION_OFF_AFTER_DRY
CONF_COIL_DRY_MINUTES = "coil_dry_minutes"
DEFAULT_COIL_DRY_MINUTES = 10

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
    "engage_bounds": (0.5, 5.0),  # UI + load clamp for the re-engage drift
    "dual_min_gap": 2.0,  # a dual-setpoint room's least Cool to - Heat to
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
    "engage_bounds": (0.25, 2.5),  # UI + load clamp for the re-engage drift
    "dual_min_gap": 1.0,  # a dual-setpoint room's least Cool to - Heat to
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

# Vane "kick": a vane change while a head is OFF can't physically move the
# louvre (and the firmware forgets the position on power-up), so the
# coordinator briefly runs the head in fan_only to apply it, then hands the
# head back to the plan (off again in eco/away).
VANE_KICK_SPINUP = 8  # s after fan_only before commanding the vane
VANE_KICK_APPLY = 20  # s for the louvre to travel before switching back off
# Cap on how long unload/reload waits for a cancelled kick to unwind. A kick
# awaits another integration's climate/select service, and a handler that
# defers cancellation must not hang Home Assistant's config-entry unload.
VANE_KICK_RETIRE_TIMEOUT = 5  # s

# Heartbeat / drift re-assert interval (time_pattern "/15" in the YAML)
HEARTBEAT_MINUTES = 15

# Self-heal debounce windows (YAML `for:` durations)
BAND_DRIFT_DELAY = 20  # head in heat_cool/auto/dry this long -> re-apply
OFF_WHILE_ENABLED_DELAY = 30  # head off while enabled this long -> re-apply
STARTUP_RECOVER_DELAY = 40  # after HA start, wait this long then recompute

# A seed with no fan-hold restore data reads a fan_only idle token as our own
# only on an entry at least this old (s) whose coordinator was restored ON; a
# younger entry may be a first-ever compute that must honor a pre-install pick.
IDLE_SEED_MIN_ENTRY_AGE = 600

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

# Every per-zone entity suffix the platforms register, so one room's unique_ids
# are f"{entry_id}_{zone_slug(index)}_{suffix}". Two callers need this list to be
# complete: the setup prune (a missing suffix removes and recreates that record
# on EVERY setup, spending the room's custom name, area and disabled flag on HA
# 2024.12) and the reorder move (a missing suffix leaves that one setting behind
# on the old priority slot). tests/test_registry_lifecycle.py and
# tests/test_room_rename_reorder.py pin it against what the platforms register.
ZONE_ENTITY_SUFFIXES = (
    "target", "drift", "drift_follow_global", "enable", "thermostat", "fan_auto",
)
# The per-zone suffixes only a dual-setpoint room registers (its Heat to and
# Cool to numbers). Kept apart so a room that isn't opted in provides, and
# keeps, exactly the set above: the setup prune adds these to a room's valid
# set only while it's opted in (so turning it off removes them), and the
# reorder move carries them with every room that has them.
DUAL_ZONE_ENTITY_SUFFIXES = ("target_low", "target_high")

# Global helper entity keys (used for unique_id suffixes and translation keys).
# Per-zone entities build theirs from zone_slug() instead, and translate through
# the generic zone_* keys with the zone's name substituted in.
KEY_COORDINATOR_ENABLE = "coordinator_enable"
KEY_ECO_IDLE = "eco_idle"
KEY_HEAT_LOCKOUT = "heat_lockout"
KEY_COOL_LOCKOUT = "cool_lockout"
KEY_SHARED_MODE = "shared_mode"
KEY_PLAN = "plan"

UNAVAILABLE_STATES = ("unknown", "unavailable", "none", "")
