# Entity map — what to edit

The coordinator logic lives in HA `template:`, `script:`, and `automation:` blocks,
which **can't take runtime config** — so configuring it is a one-time find/replace of
a handful of entity IDs in [`packages/mxz_coordinator.yaml`](../packages/mxz_coordinator.yaml).
Replace each placeholder on the left with your real entity on the right.

## Required — replace these

| Placeholder in the package | What it is | Example of yours |
|---|---|---|
| `climate.head_primary` | Your **primary** indoor head (wins a mode standoff) | `climate.bedroom_minisplit` |
| `climate.head_secondary` | Your **secondary** indoor head | `climate.living_room_minisplit` |
| `sensor.room_primary_temperature` | Ambient temp sensor for the primary room | `sensor.bedroom_temperature` |
| `sensor.room_secondary_temperature` | Ambient temp sensor for the secondary room | `sensor.living_room_temperature` |
| `weather.home` | A weather entity exposing a **`daily`** forecast (drives the season pick) | `weather.forecast_home` |

> Tip: `climate.head_primary` / `climate.head_secondary` appear in the actuator
> script **and** the band-recovery automation. A single editor-wide find/replace per
> ID gets them all. There are exactly two heads, two temp sensors, and one weather
> entity to change.

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
- `input_select.hvac_season`, `input_select.hvac_shared_mode`
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
| season threshold | forecast daily high `>= 68 °F` → cooling | else heating |

If your heads' setpoint range differs, change the `59` / `88` and the `78/76` /
`61/59` eco bands in `script.mxz_coordinate` to match.
