# Entity map — what to edit (legacy YAML package)

> **This page is for the legacy manual install** (the `packages/` YAML file). The
> recommended install is now the **HACS integration**, which configures these entities
> through a UI form — no find/replace. See the README's "Install" section, and
> [`MIGRATION.md`](MIGRATION.md) for the integration's entity IDs. Keep reading only if
> you are using the YAML package directly.

The coordinator logic lives in HA `template:`, `script:`, and `automation:` blocks,
which **can't take runtime config** — so configuring it is a one-time find/replace of
a handful of entity IDs in [`packages/mxz_coordinator.yaml`](../packages/mxz_coordinator.yaml).
Replace each placeholder on the left with your real entity on the right.

## Integration diagnostic surfaces (3.4.0)

This section describes the HACS integration's added diagnostics; it is separate from
the legacy YAML mappings below. These existing entities carry the observations:

| Entity surface | Added fields | Meaning |
| --- | --- | --- |
| Room `climate.*_thermostat` and `sensor.*_plan` → `zones[]` | `command_attempted_at`, `command_returned_at`, `command_failed_at`, `command_retired_at` | HA software observation times; missing times are `not yet recorded`. |
| Same room surfaces | `command_status`, `command_error`, `command_deferred`, `command_ownership_retired` | Delivery progress, error, and ownership; a handler return does not prove physical application. |
| `sensor.*_plan` → `zones[]` only | `head_state_updated_at` | The head's HA `last_updated` as of the plan's last update; `not yet recorded` if the head is missing. Not on the room thermostat, so head attribute-only updates add no thermostat events. |
| Same room surfaces | `command_timestamp_basis`, `plan_target_basis` | Explicit software-time and computed-intent boundaries. |
| Same room surfaces | `vane_retirement_cleanup`, `control_reasons` | Retired vane cleanup observations and current reasons with next steps. |
| Room `switch.*_fan_auto` | `last_fan_command`, `prior_fan_command` | Command/adopted-observation baselines for echo tolerance; absent memory is `null`. These are not a delivery journal. |
| Same Fan auto switch | `fan_control_reason`, `fan_on_pending` | Missing-current-speed explanation (`null` otherwise); `fan_on_pending: true` appears only while an explicit ON awaits a usable speed report. Capability/restore reasons are on the room surfaces above. |

The plan's `zones[]` also exposes `sensor_health` and `sensor_age`; invalid `temp`
is `null`. A fresh install or newly added room has no saved fan record and shows no
fan-hold reason. A record that exists but is stale or malformed adds a distinct restore
reason. No fan write is sent before
the head reports a usable current speed.

Delivery observations publish immediately, including outside refresh completion.
Delivery progress, fan state, and reason changes can produce `state_changed` events
even when a thermostat's visible mode/temperature is unchanged. A head attribute-only
update the thermostat does not display adds none.
When recorded, these entities can add history/storage and trigger bare state
automations more often. Actual recorder rows, bytes, and hourly growth have not been
measured. See [Migration](MIGRATION.md#added-diagnostics-and-state-events) for the
tradeoff and [README diagnostics](../README.md#diagnose-a-room-that-is-waiting) for use.

## Integration start points

Each room's start points, read-only, in the system temperature unit. Both surfaces read
the same computed plan.

| Entity surface | Fields | Meaning |
| --- | --- | --- |
| Room `climate.*_thermostat` and `sensor.*_plan` → `zones[]` | `heat_start`, `cool_start` | The room's head starts heating below `heat_start`, or cooling above `cool_start`, while the outdoor unit is in that mode. `null` for a disabled room. |
| Same room surfaces | `heat_demand_below`, `cool_demand_above` | Below or above these, the room asks the outdoor unit to switch to heating or cooling. `null` for a disabled room. |
| Same room surfaces | `start_points_mode`, `start_points_lockouts` | `comfort` or `eco` (eco uses Away low limit and Away high limit), and the heat or cool lockouts that move a side out to Heat anyway below or Cool anyway above. `null` and empty for a disabled room. |
| Same room surfaces | `drift_source`, `drift_heat_from`, `drift_cool_from` | `room` (either side comes from the room) or `global`, and per side `room_cooler_by` or `room_warmer_by` (the room's own side), `room` (the room's drift number), `cooler_by` or `warmer_by` (the global side), or `allowed_drift`. |
| Room `number.*_drift` | `drift_heat`, `drift_cool` | The drift in use below and above target, the same as the plan row's. Read-only; the number's value and its `override` flag are unchanged. |

These are extra attributes only. A single-target room's thermostat has no
`target_temp_low` or `target_temp_high` and keeps its single target.

## Integration dual setpoint (opt-in per room)

Only a room switched on in Configure → Rooms → (room) → **Dual setpoint** has these.
Every other room keeps exactly the entities and attributes above.

| Entity surface | Fields | Meaning |
| --- | --- | --- |
| Room `number.*_heat_to` (unique ID ends `_target_low`) | value | Heat to: heat runs start below it by the heat drift and end at it. Same limits and step as the target number. Restores across a restart. Added and removed without a reload when only the option changes; removed from the entity registry when the room is switched back to one target. |
| Room `number.*_cool_to` (unique ID ends `_target_high`) | value | Cool to: cool runs start above it by the cool drift and end at it. At least 2 °F (1 °C) above heat to. |
| Room `number.*_target` | value | The middle of the range (can be a half step). Writing it moves the whole range, keeping its width. |
| Room `climate.*_thermostat` | `target_temp_low`, `target_temp_high`, `supported_features` | Heat to and cool to, with the range feature in place of the single target (no `temperature`). Modes stay `off` and `heat_cool`. A save that only turns the option on or off switches the same entity in place: it stays available, and `supported_features` changes in one state write (for example 393 to 394, and back), which HomeKit's bridge rebuilds its accessory on. |
| `sensor.*_plan` → `zones[]` | `target_low`, `target_high` | Heat to and cool to; `target` is their midpoint. A single-target row has neither key. |
| Same room surfaces | `heat_start`, `cool_start`, `heat_demand_below`, `cool_demand_above` | Worked out from heat to (heat side) and cool to (cool side). |

## Integration lost-head vote

Every room's row in the plan, whatever its settings. A head is out of sight while it
reads `unavailable` or is missing from Home Assistant. Any other state, `unknown`
included, is in sight, and a head that only reports nothing new stays in sight.

| Entity surface | Fields | Meaning |
| --- | --- | --- |
| `sensor.*_plan` → `zones[]` | `vote_status` | `live`: the head is in sight. `stale`: out of sight, and the heat or cool it last showed still counts (a room run from its own remote), as does an enabled room's demand and run. `expired`: out of sight for **Keep a lost head's vote for** (30 minutes by default) or more, so neither counts any more. The one exception is an enabled room whose Safety priority sensor is `on`: its demand and run keep counting. A room run from its own remote lets go even with its sensor `on`, and a freeze floor or overheat ceiling is never dropped by this. `unknown`: not seen since MXZ started (or reloaded), so there is no last vote to keep; an enabled room's demand counts for the same time limit from that start, then the row reads `expired`. |
| Same row | `lost_since` | When MXZ lost sight of the head (for an `unknown` head, when MXZ started), as an ISO time. `null` while the head is in sight. Display only: the time limit runs on a clock a wall-clock change can't move. |

An enabled room's `demand` and `engage` keep showing its own call; `vote_status` says
whether that still counts. A room run from its own remote shows the vote that counts in
`hands_off_vote`: its last heat or cool while `stale`, and `null` once `expired`. These
two fields are not on the room thermostat. Nothing about them is saved: after a restart
or reload every head starts `unknown` until it is seen.

## Required — replace these

| Placeholder in the package | What it is | Example of yours |
|---|---|---|
| `climate.head_primary` | Your **primary** indoor head (wins a mode standoff) | `climate.bedroom_minisplit` |
| `climate.head_secondary` | Your **secondary** indoor head | `climate.living_room_minisplit` |
| `sensor.room_primary_temperature` | Ambient temp sensor for the primary room | `sensor.bedroom_temperature` |
| `sensor.room_secondary_temperature` | Ambient temp sensor for the secondary room | `sensor.living_room_temperature` |

> Tip: `climate.head_primary` / `climate.head_secondary` appear in the actuator
> script **and** the band-recovery automation. A single editor-wide find/replace per
> ID gets them all. There are exactly two heads and two temp sensors to change.

## Optional

| Placeholder | What it is | If you don't want it |
|---|---|---|
| `notify.your_phone` | Notify service for drift/recovery alerts (one reference, in `mxz_band_recovery`) | Delete that `- action: notify.your_phone` step |

## Kept as-is (the package's own namespace — no need to edit)

These are internal helpers created by the package. You only touch them if you want
to rename the namespace (then also update the proxy's `helper_prefix` / `room_key`):

- `input_number.hvac_primary_target`, `input_number.hvac_secondary_target`
- `input_boolean.hvac_primary_enable`, `input_boolean.hvac_secondary_enable`
- `input_boolean.hvac_coordinator_enable` (kill-switch), `input_boolean.hvac_eco_idle`
- `input_select.hvac_shared_mode`
- `input_datetime.hvac_last_mode_change`
- `sensor.mxz_plan` (decision sensor), `script.mxz_coordinate` (actuator)
- event `mxz_recompute`

## Tunable constants (optional)

All have sane defaults and are commented inline in the package header and next to
where they appear:

| Constant | Default | Meaning |
|---|---|---|
| demand threshold `S` | `3.0 °F` | how far off-target before the **shared mode** may flip |
| engage deadband `D` | `1.0 °F` | how far off-target before a head **actively runs** (else it parks by the idle action) |
| mode hysteresis | `600 s` | minimum dwell before a heat↔cool flip |
| eco extremes | `cool > 78 / heat < 50 °F` | away/eco protection band |
| firmware clamp | `[59, 88] °F` | your heads' min/max setpoint (a low `< 59` made `climate.set_temperature` throw **HTTP 500** on our units) |

If your heads' setpoint range differs, change the `59` / `88` and the `78/76` /
`61/59` eco bands in `script.mxz_coordinate` to match.
