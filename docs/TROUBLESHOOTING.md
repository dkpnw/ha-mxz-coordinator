# Troubleshooting and FAQ

- [Diagnose a room that is waiting](#diagnose-a-room-that-is-waiting)
- [FAQ](#faq)

## Diagnose a room that is waiting

Open `sensor.*_plan` on the device page. Start with the room's entry in `zones`, then
compare it with the raw head and room sensor. Top-level primary/secondary attributes refer
to priority slots, not permanent room identities.

On Home Assistant 2026.10, the plan's attributes are under the sensor's ⋮ menu →
**Details**.

<details>
<summary>Screenshot: the plan's Details</summary>

<img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-plan-vote-stale.png" width="420" alt="The Plan sensor's Details: state heat, the Friendly name and Icon lines, the top-level attributes from Idle action to Standoff, then Studio's zones row with its demand, engage, temperature, target, drift, start points, sensor health, a control reason that climate.studio_head is unavailable, vote_status stale, and lost_since.">

Studio's row while its head is unavailable; see
[When a head goes unavailable](FEATURES.md#when-a-head-goes-unavailable).

</details>

| Observation | Meaning and next step |
| --- | --- |
| `standoff`, demand/engage, mode dwell | Another room's priority or dwell may be holding the direction. Check targets and priority before changing a raw head. |
| room `drift_heat`, `drift_cool` | How far below (heat) and above (cool) target the room may get before a run starts: per side, the room's own Let it get cooler by or Let it get warmer by, else the room's drift number once set, else the global Let it get cooler by or Let it get warmer by, else Allowed drift. The room's drift number still shows one value, with these two as its attributes. See [Uneven drift](FEATURES.md#uneven-drift). |
| room `heat_start`, `cool_start`, `heat_demand_below`, `cool_demand_above` | Where the room starts a run (heats below `heat_start`, cools above `cool_start`) and where it asks the whole system to switch (below `heat_demand_below`, above `cool_demand_above`), as MXZ uses them now. `start_points_mode` is `comfort` or `eco`, `start_points_lockouts` lists a heat or cool lockout in use, and `drift_source`, `drift_heat_from`, and `drift_cool_from` say where the drift came from. For a disabled room, the four edges and `start_points_mode` are `null`, and `start_points_lockouts` is `[]`. The edges are shown rounded to six decimal places; MXZ compares the unrounded values. Also on the room's thermostat. See [Start points](FEATURES.md#start-points). |
| `mode_by_hands_off`, room `hands_off_vote` | True when a disabled room run from its own remote set the shared mode, with no enabled room calling or still running to its target, or the mode went back after its vote ended; that change starts no mode-change wait. Always false with no room opted in. See [Run one room from its own remote](FEATURES.md#run-one-room-from-its-own-remote). |
| room `vote_status`, `lost_since` | `stale` or `expired` means MXZ has lost sight of the room's head (`unavailable` or missing) since `lost_since`. While `stale` its last vote still counts; once `expired` it no longer holds the shared mode. `unknown` means not seen since MXZ started. Check the head's integration and network. See [When a head goes unavailable](FEATURES.md#when-a-head-goes-unavailable). |
| `mode_by_safety`, room `safety_priority` | True while a room marked Safety priority is asserting and its want is the shared mode, or a tripped freeze floor or overheat ceiling's is. Taking the mode from an enabled room that is calling or still running to its target starts the mode-change wait like any real reversal; deciding while no enabled room is calling or running does not. Always false with no sensor wired and no floor tripped. See [Safety priority](FEATURES.md#safety-priority). |
| `mode_by_floor`, room `floor_state`, `floor_reason`, `floor_want`, `floor_low`, `floor_high` | `mode_by_floor` is true while a tripped freeze floor or overheat ceiling's direction is the shared mode. Each room's `floor_state` is `none`, `enabled`, `armed`, `heat`/`cool` (running), `held` (waiting, or the head rejecting the command), `blind` (cannot protect: no reading, head unavailable, stalled), or `yielded`, and `floor_reason` says why. Always false and `none` with no floor set. See [Freeze floor and overheat ceiling](FEATURES.md#freeze-floor-and-overheat-ceiling). |
| `inhibited`, room enable, lockouts, eco | A configured gate may be parking the room. Check the controlling switch/input. |
| `sensor_health`, `sensor_age`, `sensors_ok` | Check validity and the freshness contract. Age of an HA write can differ from age of a sample. |
| `fan_hold`, `control_reasons` | Check ownership, capability, restore, and settings warnings with their suggested next step. |
| `command_status: pending`, `command_deferred` | A service handler has not returned. New intent waits for that head; independent rooms can continue. |
| `command_status: rejected` | Read `command_error` and repair the head integration. A later ordinary update may retry. |
| `command_status: returned` | The HA handler returned. This does not prove the hardware applied the command. |
| `cancelled; outcome unknown`, retired ownership | External work may still finish. Check the head's report before relying on its outcome. |

**What these readings can't tell you**

- Command timestamps and `head_state_updated_at` are HA software observations. They do
  not show physical receipt, compressor operation, or energy use. A missing date reads
  `not yet recorded`.
- `head_state_updated_at` is only on the plan's `zones`, as of the plan's last update; the
  head entity shows its own live time.
- A per-head power sensor alone may not report outdoor-unit draw. Check your equipment
  before interpreting an idle report as a fault or an energy measurement.

**Fan settings warnings.** Absent fan settings use defaults; the plan names that absence
without guessing why. Invalid stored fan values also receive a diagnostic. Review
Configure to set supported values.

The [Entity map](ENTITY-MAP.md#integration-diagnostic-surfaces-340) lists every added
diagnostic field. [Limits of control](HOW-IT-WORKS.md#limits-of-control) covers failures,
Home Assistant going down, and recorder load.

## FAQ

Short answers to things people have asked about or tripped over. Each one links to the
section with the detail.

- [I set it up and nothing happens](#faq-nothing-happens)
- [My head looks off and its vanes are closed, but the coordinator says it is on / the room is enabled](#faq-parked)
- [The fan is running but the room is not heating or cooling](#faq-fan-not-conditioning)
- [A room needs heating or cooling but its head waits](#faq-waiting)
- [The room reached its target, drifted away, and the head did not start again](#faq-no-restart)
- [The setpoint on the head is not the room's target](#faq-setpoint)
- [The fan is stuck on one speed](#faq-fan-stuck)
- [I changed a head with its remote or app and it changed back](#faq-changed-back)
- [I want to run one room from its own remote](#faq-own-remote)
- [One room needs to win every time, not just by priority order](#faq-win-every-time)
- [Keep a disabled room from freezing (or overheating)](#faq-freeze)
- [A head dropped off the network and the other room is stuck waiting](#faq-head-dropped)

<a id="faq-nothing-happens"></a>

<details>
<summary><b>I set it up and nothing happens</b></summary>

A new entry starts with **Coordinator enable** and every room's enable switch off, so it
commands nothing. Turn on Coordinator enable, then each room (its enable switch, or set
its thermostat to `heat_cool`).

</details>

<a id="faq-parked"></a>

<details>
<summary><b>My head looks off and its vanes are closed, but the coordinator says it is on / the room is enabled</b></summary>

An enabled room's head is parked for other reasons too, so rule those out first on
`sensor.*_plan`:

- **Sensor health.** A room whose `sensor_health` is `stale` or `invalid` stops asking for
  heat or cool, and its head parks by the idle action.
- **Standby hold.** While `inhibited` is true, an `off` standby hold parks every head off.
- **Waiting.** A room that needs the other mode parks while it waits; see
  [A room needs heating or cooling but its head waits](#faq-waiting).
- **Command status.** A room `command_status` of `pending` or `rejected` means MXZ's last
  command to that head has not returned, or failed; read `command_error`.

If none of these applies, it is a satisfied room parked by its idle action. That is not a
fault, and the room has not been dropped.

- With **Off after drying** (the default for new setups) or **Off**, a room that reaches
  its target asks its head to turn off, so the fan stops and the head closes its vanes.
  After cooling, **Off after drying** first runs the fan for the coil drying time
  (default 10 min). After heating it asks for `off` straight away.
- The coordinator stays in control. The room thermostat stays `heat_cool` and its action
  reads *Idle*. On `sensor.*_plan` the room's `engage` reads `satisfied`, and the
  top-level `idle_action` shows the setting.
- When the room drifts past its re-engage band (default 1 °F / 0.5 °C), the coordinator
  wakes the head in the shared mode. A room that needs the other mode has to win it
  first; see [A room needs heating or cooling but its head waits](#faq-waiting) below.
- A head the plan parked off is not treated as drift, so the coordinator leaves it alone.
  A head someone turns off while the plan wants it running is turned back on after about
  30 seconds.
- A room waiting for the other mode parks the same way. While eco/away is on, a satisfied
  room parks off at once, with no drying run.

Want air moving instead? Choose **Fan only** in Configure → Fan and idle. See
[How a satisfied head idles](FEATURES.md#how-a-satisfied-head-idles).

</details>

<a id="faq-fan-not-conditioning"></a>

<details>
<summary><b>The fan is running but the room is not heating or cooling</b></summary>

Check the head's own mode first.

- **The head reads `fan_only`.** MXZ is idling it:
  - **Off after drying**: after cooling, the fan runs for the coil drying time (default
    10 min) to dry the coil, then the head is asked to turn off.
  - **Fan only**: a satisfied or waiting room keeps its fan moving until it needs
    conditioning again. Installs that never chose an idle action before Off after drying
    became the default keep Fan only.
  - **Standby hold** with *During a hold* set to Fan only parks every head this way while
    the hold signal is active.
  - **A vane change** on a head parked off runs it briefly in `fan_only` so the louvre
    moves, then parks it again.
- **The head reads `cool` or `heat`.** MXZ has asked for conditioning; the outdoor unit
  decides when the compressor runs, and short pauses are normal (a ~2.5 min
  anti-short-cycle pause in [the logged example](HOW-IT-WORKS.md#the-problem-in-detail)).
  If the head never took the requested mode, read `command_status` in
  [Diagnose](#diagnose-a-room-that-is-waiting).

</details>

<a id="faq-waiting"></a>

<details>
<summary><b>A room needs heating or cooling but its head waits</b></summary>

One outdoor unit runs one direction at a time, so a room that needs the other direction
waits. Its thermostat reads *Idle* and its head parks by the idle action. What decides
when it gets its turn:

| Plan sensor shows | Why the room waits |
| --- | --- |
| `standoff: true` | Rooms want opposite modes; the higher-priority room wins. Change the order in [Reconfigure](SETUP.md#reconfigure-heads-rooms-and-sensors). |
| room `demand: neutral` | A room votes to flip the shared mode only once it is past the mode switch threshold (default 3 °F / 1.5 °C), or its own drift if that is wider. Closer than that, it waits. |
| `mode_change_allowed: false` | The last flip was less than the minimum time between mode switches ago (default 600 s); see `seconds_since_mode_change`. |
| `inhibited`, lockouts, eco, `sensor_health` | A standby hold, a seasonal lockout, away limits, or an invalid or stale room sensor is keeping the room out. |

[Diagnose a room that is waiting](#diagnose-a-room-that-is-waiting) has the full list.
Mitsubishi's own standby lamp means something else: a head set, by its remote or stock
AUTO, to the mode the system is not running. Under MXZ a waiting head is parked by
command instead.

</details>

<a id="faq-no-restart"></a>

<details>
<summary><b>The room reached its target, drifted away, and the head did not start again</b></summary>

Rooms run to target, then coast until they drift past the re-engage band (default 1 °F /
0.5 °C). Each room's drift number widens or tightens that band; **Follow global drift**
hands it back to the global value. See [Everyday control](FEATURES.md#everyday-control).

</details>

<a id="faq-setpoint"></a>

<details>
<summary><b>The setpoint on the head is not the room's target</b></summary>

Usually expected:

- In a room with one target, **range heads** (`target_temp_low`/`high`) get a band:
  target − 2 °F to the target when cooling, the target to target + 2 °F when heating (a
  1 °C band on a °C system). **Single-setpoint heads**, common on MXZ indoor units, get the
  target itself.
- A [dual-setpoint](FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint)
  room uses its Heat to edge in place of the target while heating, and its Cool to edge
  while cooling. A range head gets the same band built from that edge, and a
  single-setpoint head gets the edge itself.
- Setpoints are rounded to 1 °F or 0.5 °C and kept within Configure's Lowest/Highest
  setpoint and the head's own limits.
- A head that only holds 0.5 °C steps (ESPHome CN105) in a °F system shows some whole-°F
  setpoints 0.5 °F off, such as 64.5 for 64. Since 3.4.1 MXZ compares on the head's own
  steps and does not re-send. See
  [Setpoints on °C-native heads](MIGRATION.md#setpoints-on-c-native-heads-in-a-f-system-341).
- While eco/away is on, heads get fixed eco setpoints instead of the room target.
- A parked head (`fan_only` or `off`) is sent a mode only, so it keeps its last setpoint.

The head also reads its own thermistor, which can differ from your room sensor; see
[Best practice: give the firmware your room sensor too](HOW-IT-WORKS.md#best-practice-give-the-firmware-your-room-sensor-too).

</details>

<a id="faq-fan-stuck"></a>

<details>
<summary><b>The fan is stuck on one speed</b></summary>

Picking a speed makes it a hold, and a hold has no timeout. Changing targets or drift does
not release it. Turn **Fan auto** on, or set the head's fan to `auto`. The room's
`fan_hold` on the plan sensor shows whether a hold is set. If Fan auto is unavailable, the
head may not advertise an exact `auto` fan mode. See
[Who drives the fan](FEATURES.md#who-drives-the-fan).

</details>

<a id="faq-changed-back"></a>

<details>
<summary><b>I changed a head with its remote or app and it changed back</b></summary>

While the coordinator is enabled it is the head's writer. A head switched to `auto`,
`heat_cool`, or `dry` is put back after about 20 seconds; a head turned off while the plan
wants it running, after about 30 seconds. If you set up drift alerts, you get one. Other
changes, such as a setpoint, are corrected on a later update. A fan speed you pick is kept
as a hold. To run the heads by hand, turn **Coordinator enable** off; MXZ then leaves them
alone. To hand just one room to its remote, see the next answer.

</details>

<a id="faq-own-remote"></a>

<details>
<summary><b>I want to run one room from its own remote</b></summary>

Turn on **Let its own remote run it while disabled** for that room in Configure → Rooms,
then disable the room (its switch, or set its tile to `off`). MXZ turns the head off once,
retrying until the head reports `off`. Then MXZ leaves it alone until you enable the room
again, across restarts too. With the setting off (the default), a disabled room's head is
held off, and MXZ turns it back off whenever someone turns it on. See
[Run one room from its own remote](FEATURES.md#run-one-room-from-its-own-remote).

</details>

<a id="faq-win-every-time"></a>

<details>
<summary><b>One room needs to win every time, not just by priority order</b></summary>

Wire a `binary_sensor` to that room's **Safety priority** field in Configure → Rooms.
While it reads exactly `on`, that room's want for heat or cool outranks every other
room's, including an enabled room that is calling or still running to its target, which
an ordinary vote from a room run on its own remote never overrides. Only a tripped
freeze floor ranks higher, and a tripped overheat ceiling is settled against it by room
order. It only raises a want the room already has; it never starts heat or cool by
itself, so it is not freeze protection. Reserve it for a real floor or ceiling
condition, not everyday comfort: it is an override, not a priority bump. See [Safety
priority](FEATURES.md#safety-priority).

</details>

<a id="faq-freeze"></a>

<details>
<summary><b>Keep a disabled room from freezing (or overheating)</b></summary>

Fill in that room's **Freeze floor** (and, if you want, **Overheat ceiling**) in
Configure → Rooms → (room). Both are empty by default. While the room is disabled, MXZ
heats it when its sensor falls below the floor (cools it above the ceiling), runs it
3 °F (1.5 °C) back inside, then turns the head off again. A freeze floor wins over every
other room's want in any room order, a Safety priority room's cool included; an overheat
ceiling against a Safety priority room goes by room order. It does nothing on a lost or
stale sensor, gives up after 2 hours of running without progress, and never touches
anything but that room's own head. Each of those sends a notice through **Notify on
drift**, or, with none set, a Home Assistant notification; at most one an hour per room.
During a run, changing only the head's setpoint does not stop it: MXZ puts its run's
setpoint back at the next update. If someone was already heating a room run from its own
remote, MXZ keeps their setpoint when it is warmer than the clear line's, follows a
setpoint they change during the run (a raise to 74 °F stays at 74 °F), and gives them
their own heat back at their latest setpoint at the end; a hand-back that lands during an
`eco` standby hold is dropped, the head is turned off, and their heat does not come back
when the hold ends. See
[Freeze floor and overheat ceiling](FEATURES.md#freeze-floor-and-overheat-ceiling).

</details>

<a id="faq-head-dropped"></a>

<details>
<summary><b>A head dropped off the network and the other room is stuck waiting</b></summary>

A lost head's last vote keeps counting for **Keep a lost head's vote for** (30 minutes by
default), then expires so MXZ can switch the shared mode. A head that has lost its
connection keeps running its last command, though, and the outdoor unit stays in its
mode, so the waiting room may sit in standby until the head reconnects or is turned off
at the unit. See [When a head goes unavailable](FEATURES.md#when-a-head-goes-unavailable).

</details>
