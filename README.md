# MXZ Coordinator

**Tesla-style, single-target control for Mitsubishi MXZ multi-zone mini-splits.** Several
indoor heads share one outdoor unit — this coordinates them so they stop fighting over the
shared compressor. Set **one comfort temperature per room**; it figures out the rest.

> Shared as-is. Issues and PRs welcome, but support is best-effort — this was built and
> validated on one real two-zone system (see [Caveats](#caveats)).

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mxz_coordinator)

**One-click HACS install:** click **Add to HACS** above → download → restart HA → click
**Add Integration** → pick your two heads and two temperature sensors. Vane controls are
detected automatically; everything else has sensible defaults. No YAML editing.

---

## Highlights

- 🎯 **One target per room, Tesla-style.** Set a single temperature and the coordinator
  decides whether the shared system heats or cools to reach it — no dual setpoints, no
  hardware AUTO, no mode-juggling.
- 🚫 **No more zone starvation.** It keeps both heads in one explicit shared mode, so an
  idle head can't hold the compressor neutral and starve the room that's actually calling
  (the classic MXZ-on-AUTO failure). A satisfied head idles in `fan_only` instead of
  parking the other room in standby.
- 🌀 **Dynamic fan speed.** Opt-in "fan boost" ramps the fan toward max when a room is far
  from target and eases it back as the room closes in — Tesla-style — instead of the
  firmware's weak `auto` ramp.
- 🌦️ **Local-weather seasonal changeover.** Point it at any `weather.*` entity (or an
  outdoor temp sensor) and it auto-locks out heating in summer / cooling in winter from
  your own forecast. No calendar, no cloud.
- 🌡️ **Works in °C and °F.** Adapts to your Home Assistant unit automatically, with 0.5°
  resolution and clean metric defaults on °C.
- 🍎 **Native thermostat tiles** for HomeKit / Google / Assist — a clean single-setpoint
  dial per room, with vane/swing control passed through.
- 🛟 **Self-healing.** If a head gets bumped off its coordinated mode (someone hits the
  wall unit, or it powers off while enabled), the coordinator puts it back — and can ping
  your phone so you know it happened.

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
and drives each room to its **own single target**. (When no room is calling, the shared mode
simply rests at whatever was **last called** — preserved across restarts. No weather/season input.
Optionally, a **resting-mode bias** can pin the idle mode to `cool` or `heat` so the system never
sits in the wrong mode for the season; a real opposite demand still flips it. Two **seasonal lockout**
switches make one direction *reluctant*: **heat-lockout** (`switch.*_heat_lockout`) holds off heating
so a below-target room idles in `fan_only` and lets passive solar warm it, and **cool-lockout**
(`switch.*_cool_lockout`) is the winter mirror (let a warm room drift down on its own). Cooling/heating
in the other direction is untouched, and configurable **safety floor/ceiling** still act at the
extremes. Drive them however you like — or point the integration at a **weather entity** (any
`weather.*`, e.g. a Tempest/WeatherFlow forecast, or an outdoor temperature `sensor`) and it runs a
**local-weather seasonal changeover** automatically: forecast daily high ≥ your warm threshold → heat
locked; ≤ your cold threshold → cool locked; in between → both free. No calendar, no cloud — your
local weather decides. Configure it under **Configure → options**. No weather entity? Add Home
Assistant's built-in **Met.no** integration (one click, uses your Home location — no ZIP needed) and
point the changeover at it.)
A head that's satisfied doesn't
switch to AUTO and stall the system — it idles in **`fan_only`**, closing its expansion valve
(LEV) while the other head keeps conditioning. This embraces the one-mode physical constraint
instead of fighting it.

Optionally, an opt-in **fan boost** (default off) drives each head's fan speed by how far the room
is off target — Tesla-style, big delta → max fan, easing down (with hysteresis) as it closes —
overriding the firmware's weak `auto` ramp (true airflow order `quiet < low < medium < middle < high`;
note `middle` is faster than `medium`). Configure it under **Configure → options** (enable + max speed).

All of the above live in the options entry. Saving them **merges** onto your existing settings (a
partial save never wipes the rest) and **mirrors** the config into the entry's data, so if something
ever clears the options out-of-band the coordinator keeps running from the mirror (and logs a warning)
rather than silently snapping back to defaults.

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
eco); the coordinator translates them into safe firmware commands. Three pieces — implemented in
Python by the HACS integration (`custom_components/mxz_coordinator/`) and mirrored 1:1 by the
legacy package ([`packages/mxz_coordinator.yaml`](packages/mxz_coordinator.yaml)):

1. **Decide — the plan** (`sensor.*_plan`; the package's `sensor.mxz_plan` `template` sensor). No
   side effects; its state is the chosen shared mode (`cool`/`heat`). Two thresholds:
   - **demand** (`S = 3 °F`): how far a room must be off-target before the **shared mode** may
     flip. The **primary** room wins a standoff (one wants heat, the other cool); a **600 s
     hysteresis** stops flapping.
   - **engage** (`D = 1 °F`): how far off-target before a head **actively runs**. Within the
     deadband it idles in `fan_only`, so a satisfied room is **not** dragged along when the
     other room forces the mode.
   - Eco/away → wide `78 / 50 °F` protection extremes only (system sits off unless extreme).
2. **Act — the actuator** (the integration's coordinator; the package's `script.mxz_coordinate`) —
   the only thing that commands the heads. Reaches each room's target (`cool → high=target,
   low=target−2`; `heat → low=target, high=target+2`), **both edges clamped to `[59, 88] °F`**.
   Satisfied head or standoff-loser → `fan_only`; disabled or eco-satisfied → `off`. Idempotent
   (skips a head already correct); never `heat_cool`/`auto`; always sends both setpoint edges
   **with** the mode. Gated on the kill-switch (`switch.*_coordinator_enable`; the package's
   `input_boolean.hvac_coordinator_enable`).
3. **Trigger** fires the actuator on a real decision change (mode flip, demand/engage change), a
   `/15 min` heartbeat, and the `mxz_recompute` event. Two self-heal paths cover drift (a head
   reverting to `heat_cool/auto/dry`, or going `off` while enabled) and a recompute after HA restart.

> The thresholds (3 °F demand, 1 °F engage, 600 s hysteresis, `[59,88]` clamp) are the integration's
> **option defaults** (editable via its *Configure* dialog) and are marked inline in the legacy package.

### Units (°C and °F)

The integration works in **your Home Assistant temperature unit** — no configuration needed. On a
metric (°C) system it adopts clean metric defaults (1.5° demand, 0.5° engage, `[15, 31] °C` clamp,
20/10 °C changeover, 21 °C target default) and **0.5° setpoint resolution**; on a °F system it uses the
values shown throughout this README (the °F examples above are just that — examples). Room sensors and
head setpoints are read/written in the system unit, so nothing is hard-coded to Fahrenheit. *(New in
v2.8.0 — earlier versions assumed °F.)*

---

## Install

### HACS (recommended — one click)

1. Click **[Add to HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration)**
   (the badge at the top), then **Download** in the dialog. If the badge doesn't open,
   add `https://github.com/dkpnw/ha-mxz-coordinator` as a HACS **custom repository**
   with category **Integration**, then download "MXZ Coordinator".
2. **Restart Home Assistant.**
3. Click **[Add Integration](https://my.home-assistant.io/redirect/config_flow_start/?domain=mxz_coordinator)**
   (or **Settings → Devices & Services → Add Integration → MXZ Coordinator**).
4. In the config form, pick your **primary** and **secondary** heads (the primary wins a
   mode standoff) and your two **room temperature sensors**. Optionally choose a **notify
   target** for drift alerts (a dropdown of your `notify.*` services). Each head's
   **vane selects are auto-detected** from its device — no need to pick them (you can
   override them later under **Configure**).
5. The integration creates the helpers (`number.*_target`, `switch.*_enable`,
   `switch.*_coordinator_enable`, `switch.*_eco_idle`, `select.*_shared_mode`) and
   `sensor.*_plan`. Turn on **Coordinator enable**, set each room's target, and enable
   the rooms. Tune the thresholds anytime via the integration's **Configure** dialog.

Adapt the example presets in [`examples/presets.yaml`](examples/presets.yaml) (day /
night / away) to the new entity IDs if you want scheduled comfort changes.

### Legacy / manual (YAML package)

Prefer not to use HACS? The original drop-in package still ships in
[`packages/mxz_coordinator.yaml`](packages/mxz_coordinator.yaml): copy it to
`config/packages/`, enable `homeassistant: packages: !include_dir_named packages`, edit
the entity IDs per [`docs/ENTITY-MAP.md`](docs/ENTITY-MAP.md), and reload YAML. This path
uses `input_*` helpers instead of the integration's entities and is **not** one-click.

> **Already on the YAML package?** See [`docs/MIGRATION.md`](docs/MIGRATION.md) for the
> entity-ID mapping and upgrade steps (it's a breaking change — remove the package so the
> two don't fight over the heads).

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
the shared direction idles in `fan_only`. To go to N zones you'd extend the plan computation to
fold N per-room demands into `any_cool`/`any_heat`, keep a single primary tiebreak, and loop the
per-head apply block over each head (`coordinator.py` in the integration, or `script.mxz_coordinate`
in the package). Left as a documented exercise rather than shipped, because it's unvalidated on
hardware here.

---

## Caveats

- Built and validated on **one** real setup (MSZ indoor heads on a single MXZ outdoor unit).
  Other MXZ models/firmware may behave differently — especially the ~6-minute cool→heat
  reversal lag and the per-zone-power blindness.
- The coordinator alone works on **any** HA `climate` heads. The single-target
  *thermostat surface* (one number + Heat/Cool, exposed to HomeKit/Google) ships natively as
  two `climate.*` entities — see **[Related](#related--the-single-target-thermostat-surface)**.
  It's optional; you don't need it to use the coordinator.
- Public release means issues will come in; scope is intentionally tight (2-zone integration +
  legacy package).

---

## Related — the single-target thermostat surface

Each room is exposed as a **native single-target thermostat** (`climate.*_primary_thermostat` /
`climate.*_secondary_thermostat`, **v2.1.0+**): one number + Heat/Cool auto, rendered as a clean
single-setpoint tile (never a dual threshold) that binds directly to HA/HomeKit/Google — instead of
the raw `cool`/`heat`/`fan_only` firmware tile. It's a thin facade over the room's `number.*_target`
and `switch.*_enable` entities; the coordinator stays the sole writer to your real heads. Expose only
these `climate.*` thermostats (not the raw heads) to avoid two fighting tiles per room. The tile's
**fan** passes through to the head, its setpoint slider is bounded to the firmware operating band
(`clamp_min`–`clamp_max`, default 59–88 °F / 15–31 °C), and optional vane `select` entities, if
configured, appear as swing modes.

> **Older setups / the legacy YAML package** got this surface from the
> [Mitsubishi Climate Proxy](https://github.com/echavet/MitsubishiCN105ESPHome)'s
> `coordinator_single_target` option, which redirects a thermostat's writes to the package's
> `input_*` helpers and fires `mxz_recompute`. That still works with the **YAML package**, but the
> HACS integration no longer needs the proxy — its native thermostats own the surface. (The
> `mxz_recompute` event is still honored, so existing proxy/automation nudges keep working.) The
> proxy's tile assumes dual-setpoint CN105 firmware; the coordinator itself does not.

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
