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
| Same room surfaces | `command_status`, `command_error`, `command_deferred`, `command_ownership_retired` | Delivery progress, error and ownership; a handler return does not prove physical application. |
| Same room surfaces | `head_state_updated_at` | The head's HA `last_updated`, including attribute changes; `not yet recorded` if the head is missing. |
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
Volatile attributes can produce `state_changed` events even when a thermostat's
visible mode/temperature is unchanged, especially on head attribute-only updates.
When recorded, these entities can add history/storage and trigger bare state
automations more often. Actual recorder rows, bytes and hourly growth have not been
measured. See [Migration](MIGRATION.md#added-diagnostics-and-state-events) for the
tradeoff and [README diagnostics](../README.md#diagnose-a-room-that-is-waiting) for use.

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
| engage deadband `D` | `1.0 °F` | how far off-target before a head **actively runs** (else `fan_only`) |
| mode hysteresis | `600 s` | minimum dwell before a heat↔cool flip |
| eco extremes | `cool > 78 / heat < 50 °F` | away/eco protection band |
| firmware clamp | `[59, 88] °F` | your heads' min/max setpoint (a low `< 59` made `climate.set_temperature` throw **HTTP 500** on our units) |

If your heads' setpoint range differs, change the `59` / `88` and the `78/76` /
`61/59` eco bands in `script.mxz_coordinate` to match.
