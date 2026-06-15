# MXZ Coordinator

**A Home Assistant control layer for Mitsubishi MXZ multi-zone mini-splits (multiple
indoor heads on ONE outdoor unit) that fixes the "idle head starves the other / nothing
turns on" deadlock — and gives each room a single, Tesla-style comfort target.**

> Shared as-is. Issues and PRs welcome, but support is best-effort — this was built and
> validated on one real two-zone system (see [Caveats](#caveats)).

---

## The problem: don't run AUTO on an MXZ

If you have several indoor heads on a single Mitsubishi MXZ outdoor unit and you set them
to **AUTO** (heat/cool) in Home Assistant, you've probably seen this: one room sits a
degree from its setpoint while the *other* room — which is way off target — does **nothing**.
The system looks broken. It isn't.

**Mitsubishi's own manuals tell you not to do this.** The MSZ-SF indoor-unit manual says
AUTO mode is *"not recommended if this indoor unit is connected to a MXZ type outdoor unit…
the indoor unit becomes standby mode."* The mechanism:

- One outdoor unit = **one compressor + one reversing valve** = it can only do **one mode at
  a time** (the same-mode restriction is architectural). The MSZ-GE manual: *"cooling and
  heating cannot be done at the same time… the unit selected last goes into standby mode."*
- In AUTO, each head decides heat-vs-cool from **its own room**, not the aggregate. The
  **lowest-address head is the mode master** and forces the others to follow. An idle head
  sitting in its deadband holds the shared outdoor unit neutral and parks the demanding head
  in standby.

We reproduced exactly this on real hardware: with both heads in `heat_cool`, the primary
room at 70 °F against a 64 °F cooling target sat **idle / ~26 W for over an hour** while the
secondary room (1 °F from its band) was on. The instant the secondary head was turned **off**,
the primary engaged and ramped to ~460 W. Classic starvation.

## The fix: one explicit shared mode + per-room targets + `fan_only` idling

**Never run the heads in hardware AUTO.** Instead, a software coordinator keeps **both heads
in ONE explicit mode at any instant** — auto-choosing `cool` or `heat` from each room's
temperature vs. its target (a 3 °F delta flips the shared mode, with hysteresis), Tesla-style —
and drives each room to its **own single target**. (A weather forecast only seeds the *resting*
mode for when no room is calling; it does not drive the mode.) A head that's satisfied doesn't
switch to AUTO and stall the system — it idles in **`fan_only`**, closing its expansion valve
(LEV) while the other head keeps conditioning. This embraces the one-mode physical constraint
instead of fighting it.

This is confirmed by the outdoor unit's service manual (OCH573E): there's one independently
metered LEV per head, and the unit *"fully closes the LEV on the indoor unit which is in FAN,
COOL, STOP, or thermo-OFF."* A satisfied head in the same explicit mode closes its LEV and
idles — **no deadlock.**

### Live evidence (the test that greenlit this)

Both heads explicit `cool`; primary @ a low target (room → wants cool), secondary satisfied
and left **on**:

```
 20s  675W  primary cool/high | secondary idle (satisfied)
 60s  255W  primary cool/high | secondary idle
 80–220s 45W primary cool/high | secondary idle    ← compressor short-cycled off ~2.5 min
240–380s 101→609W primary cool/high | secondary cool  ← both served, sustained
```

A satisfied secondary head in explicit COOL **never blocked the primary** — it held
cool/high the whole time and ramped to ~600 W within minutes, versus **>1 h of starvation**
under AUTO in the same conditions.

---

## Architecture: decide → act → trigger

The coordinator is the **sole writer** of the heads. You set helper inputs (target / enable /
eco); the coordinator translates them into safe firmware commands. Three pieces, all in one
drop-in package ([`packages/mxz_coordinator.yaml`](packages/mxz_coordinator.yaml)):

1. **Decide — `sensor.mxz_plan`** (a `template` sensor, no side effects). Its state is the
   chosen shared mode (`cool`/`heat`). Two thresholds:
   - **demand** (`S = 3 °F`): how far a room must be off-target before the **shared mode** may
     flip. The **primary** room wins a standoff (one wants heat, the other cool); a **600 s
     hysteresis** stops flapping.
   - **engage** (`D = 1 °F`): how far off-target before a head **actively runs**. Within the
     deadband it idles in `fan_only`, so a satisfied room is **not** dragged along when the
     other room forces the mode.
   - Eco/away → wide `78 / 50 °F` protection extremes only (system sits off unless extreme).
2. **Act — `script.mxz_coordinate`** (the only thing that commands the heads). Reaches each
   room's target (`cool → high=target, low=target−2`; `heat → low=target, high=target+2`),
   **both edges clamped to `[59, 88] °F`**. Satisfied head or standoff-loser → `fan_only`;
   disabled or eco-satisfied → `off`. Idempotent (skips a head already correct); never
   `heat_cool`/`auto`; always sends both setpoint edges **with** the mode. Gated on the
   kill-switch `input_boolean.hvac_coordinator_enable`.
3. **Trigger — `automation.mxz_coordinator`** fires the actuator on a real decision change
   (mode flip, demand/engage change), a `/15 min` heartbeat, and the `mxz_recompute` event.
   Two self-heal automations cover drift (a head reverting to `heat_cool/auto/dry`, or a
   head going `off` while enabled) and a stale plan sensor after an HA restart.

---

## Install

1. Copy [`packages/mxz_coordinator.yaml`](packages/mxz_coordinator.yaml) to your HA config at
   `config/packages/mxz_coordinator.yaml`. Enable packages in `configuration.yaml` if you
   haven't:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Edit the handful of entity IDs at the top of the file to match your system — see
   [`docs/ENTITY-MAP.md`](docs/ENTITY-MAP.md) (two heads, two temp sensors, one weather
   entity, an optional notify service).
3. Reload YAML (Developer Tools → YAML, or restart). You'll get the helpers, `sensor.mxz_plan`,
   `script.mxz_coordinate`, and the automations.
4. Turn on `input_boolean.hvac_coordinator_enable`, set each room's
   `input_number.hvac_*_target`, and enable the rooms. Optionally adapt the example presets in
   [`examples/presets.yaml`](examples/presets.yaml) (day / night / away).

### HACS (custom repository)

A plain HA *package* isn't a first-class HACS category, so the primary install is the manual
copy above. If you'd rather track updates through HACS, you can add this repo as a **custom
repository** and pull the package file from releases — but you still drop the YAML into
`packages/` yourself. (A future config-flow integration would make this one-click; not in v1.)

---

## Gotchas (read these before you debug)

- **Per-zone power is shared, not per-head.** On a multi-zone outdoor unit, only the
  lowest-address head reports the real outdoor-unit draw. The other head's power/frequency
  read near-zero **even while it's actively being served.** Never declare a head "dead" from
  its own power sensor.
- **Anti-short-cycle timing.** The compressor has a ~3-minute minimum off-time in cooling, and
  after a **cool → heat** reversal it takes **~6 minutes** to engage (reversing-valve delay).
  `hvac_action` flips instantly; the actual draw lags. This is normal.
- **`fan_only` is correct, not a fault.** A satisfied head parked in `fan_only` is the design
  working — that's what keeps it from starving the other room.
- **Setpoint clamp `[59, 88] °F`.** A low setpoint below 59 made `climate.set_temperature`
  throw **HTTP 500** and abort on our heads — hence the clamp. Adjust to your firmware's range.
- **Minimum-capacity floor.** The compressor can't modulate below ~1/2.5 of nameplate; excess
  can bleed into a satisfied head as mild overshoot. Not a deadlock.

---

## Extending to N zones

v1 ships the validated **two-zone** arrangement (one primary, one secondary). The arbitration
generalizes cleanly: the **primary** zone picks the shared mode, and any zone that doesn't want
the shared direction idles in `fan_only`. To go to N zones you'd extend `sensor.mxz_plan` to
fold N per-room demands into `any_cool`/`any_heat`, keep a single primary tiebreak, and loop the
per-head apply block in `script.mxz_coordinate` over each head. Left as a documented exercise
rather than shipped, because it's unvalidated on hardware here.

---

## Caveats

- Built and validated on **one** real setup (MSZ indoor heads on a single MXZ outdoor unit).
  Other MXZ models/firmware may behave differently — especially the ~6-minute cool→heat
  reversal lag and the per-zone-power blindness.
- The coordinator alone works on **any** HA `climate` heads. The single-target
  *thermostat surface* (one number + Heat/Cool, exposed to HomeKit/Google) is a separate
  companion component — see **[ha-mitsubishi-climate-proxy](#related)** — and assumes
  dual-setpoint CN105 firmware. You don't need it to use the coordinator.
- Public release means issues will come in; scope is intentionally tight (2-zone package +
  this README).

---

## Related — the single-target thermostat surface

The coordinator drives any HA `climate` heads on its own. If you also want each head to appear as a
**single-target thermostat** (one number + Heat/Cool auto + vane) in HA/HomeKit/Google — instead of
the raw `cool`/`heat`/`fan_only` firmware tile — pair it with the **`coordinator_single_target`** option
of the [Mitsubishi Climate Proxy](https://github.com/echavet/MitsubishiCN105ESPHome) component, which
redirects the thermostat's writes to this coordinator's helpers. It defaults to the same helper names
this package uses (`hvac_<room>_target` / `hvac_<room>_enable` / `input_select.hvac_shared_mode` /
`mxz_recompute`). That surface assumes the dual-setpoint CN105 firmware; the coordinator itself does not.

## Credits & prior art

- [BarrettPalmer/Smart-HVAC-Automation-for-Home-Assistant-Mini-Splits](https://github.com/BarrettPalmer/Smart-HVAC-Automation-for-Home-Assistant-Mini-Splits)
- [bjrnptrsn/climate_group_helper](https://github.com/bjrnptrsn/climate_group_helper)
- [bartmachielsen/smart_climate](https://github.com/bartmachielsen/smart_climate)
- Mitsubishi service/installation manuals (MSZ-SF, MSZ-GE, MXZ-18NV, OCH573E) for the
  AUTO-on-MXZ behavior and LEV documentation.

## License

[MIT](LICENSE).

---

*Not affiliated with, endorsed by, or associated with Mitsubishi Electric Corporation.
"Mitsubishi Electric" and the three-diamond logo are trademarks of their respective owner,
used here for identification/compatibility only.*
