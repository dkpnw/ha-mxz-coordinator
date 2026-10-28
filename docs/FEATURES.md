# Feature guide

Everything MXZ does, in detail. The [README](../README.md) has the short version. Settings
named here live in Configure; see [Configuration](CONFIGURATION.md) for the menu.

- [Everyday control](#everyday-control)
- [Drift: when a room starts a run](#drift-when-a-room-starts-a-run):
  [uneven drift](#uneven-drift), [start points](#start-points), and
  [per-room uneven drift](#per-room-uneven-drift)
- [Give a room separate heat and cool setpoints (dual setpoint)](#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint)
- [Run one room from its own remote](#run-one-room-from-its-own-remote)
- [Safety priority](#safety-priority)
- [When a head goes unavailable](#when-a-head-goes-unavailable)
- [Freeze floor and overheat ceiling](#freeze-floor-and-overheat-ceiling)
- [Who drives the fan](#who-drives-the-fan)
- [How a satisfied head idles](#how-a-satisfied-head-idles)

## Everyday control

**Your room thermostat.** Use each room's thermostat for one target and
`heat_cool`/`off`. A room you switch to a dual setpoint shows a range instead; see
[Dual setpoint](#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint).

- `heat_cool` means the coordinator chooses a shared direction. It does not send
  hardware AUTO or heat/cool simultaneously.
- Turning the tile off disables that room. Disabling a room relinquishes its
  coordination; it does not promise to power the raw head off.
- By default a disabled room's head is held off: MXZ turns it off on every update,
  including after someone turns it on from its own remote or timer. To run one room from
  its own remote instead, turn on **Let its own remote run it while disabled** for it;
  see [Run one room from its own remote](#run-one-room-from-its-own-remote).

Expose the per-room thermostats to HomeKit/Google, or use them in HA. Avoid exposing a
second raw-head control for the same room.

**What the coordinator does.** It commands mode and bounded setpoints for enabled rooms.
Rooms run to target, then coast until their drift band is crossed. A fresh target uses
the head's setpoint when readable; 70 °F / 21 °C is the fallback.

| Value | °F | °C |
| --- | --- | --- |
| Default demand (Mode switch threshold) | 3 °F | 1.5 °C |
| Default drift (Allowed drift) | 1 °F | 0.5 °C |
| Fallback for a fresh target (head's setpoint unreadable) | 70 °F | 21 °C |
| Range of each room's drift number | 0.5–5 °F | 0.25–2.5 °C |

**Per-room drift.** Writing a room's drift number creates an override, even if you enter
today's global value. Press **Follow global drift** to remove that override. The number
updates immediately; the ordinary debounced recompute can then change demand and head
output. The button is unavailable until its drift number is loaded and available. To let
rooms drift further one way than the other, see [Uneven drift](#uneven-drift).

**Shared mode** offers `cool` and `heat`.

- A changed choice stamps the ordinary mode-flip dwell. Automatic arbitration can change
  direction again after that dwell.
- Selecting the current direction adds no hold or new dwell.
- The selector does not enable the coordinator, enable rooms, or lift lockouts/standby
  holds.

**Resting mode** (Configure → Comfort) picks the shared mode when no room is calling:
Last used (the default), Cool, or Heat. It only sets the neutral resting mode; a real
call for the other mode still flips it, subject to the same dwell.

**Seasons.** A weather source (Configure → Seasons: a forecast or an outdoor temperature)
drives the **Heat lockout** and **Cool lockout** switches. A daily high at or above **Lock
out heat when the high is at least** (default 68 °F / 20 °C) locks out heat; one at or
below **Lock out cool when the high is at most** (default 50 °F / 10 °C) locks out cool;
in between, neither. No weather reading means no lockout. A locked-out mode still runs
for a room below **Heat anyway below** (default 58 °F / 14 °C) or above **Cool anyway
above** (default 80 °F / 27 °C). The two switches can also be set by hand or by an
automation; with a weather source set, it re-drives them when the source updates and at
least hourly.

**Away.** With the **Eco idle** switch on, rooms ignore their targets: MXZ heats a room
only below **Away low limit** (default 50 °F / 10 °C) and cools it only above **Away high
limit** (default 78 °F / 26 °C), with fixed eco setpoints.

**Stepping back: Coordinator enable and standby hold**

- Turn **Coordinator enable** off to stop new coordinated control and use the heads' own
  controls. Heads keep their last commanded state.
- A handler already accepted by another integration can return late; turning MXZ off
  cannot retract it.
- A standby hold instead parks coordinated heads for as long as its signal is active. Its
  default `eco` action allows conditioning at protection extremes; `off`/`fan_only` are
  fixed parks. Missing, `unknown`, or `unavailable` input releases the hold.
- These are software policies, not certified freeze protection or guarantees of zero
  draw.

**A head changed at the wall.** While the coordinator is enabled it is the head's writer.
A head switched to `auto`, `heat_cool`, or `dry` is put back after about 20 seconds; a
head turned off while the plan wants it running, after about 30 seconds. A head the plan
parked off is not treated as drift. If you set a **Notify on drift** service, you get an
alert.

**Presets and manual refresh.** Example presets:
[day/night/away](../examples/presets.yaml). The `mxz_coordinator.recompute` service
requests refreshes for loaded entries; the `mxz_recompute` event is also honored. Neither
bypasses safety gates or proves delivery.

<details>
<summary>After a restart: how a room resumes its run</summary>

After restart, MXZ can resume an observed `cool`/`heat` run when both Coordinator enable
and that room's enable restore ON. It waits for usable head evidence and a valid room
reading that is not stale under an enforced freshness profile. Construction and a live
enable do not adopt a previous run.

Changing a target while the coordinator is OFF or the room is disabled clears that run's
latch; when enabled again, the room starts coasting and uses the ordinary re-engage band.
This fixes the restart intent that 3.3.0's construction-time compute could spend too
early. After a room coasts, a follow-up refresh lets it progress without needing a head
echo.

</details>

## Drift: when a room starts a run

### Uneven drift

Allowed drift is the same distance either way. To let rooms wander further one way than
the other, set **Let it get cooler by** (how far below target before heating starts) and
**Let it get warmer by** (how far above target before cooling starts) in Configure →
Comfort → Advanced. Each takes 0.5–5 °F / 0.25–2.5 °C. Leave one blank to use Allowed
drift on that side; both blank is ordinary Allowed drift.

These only move where a run starts. A run always goes back to the target. With target 70
and warmer by 5, cooling starts above 75 and runs down to 70. To have it stop at 73, set
the target to 73.

A room's own drift number, once you or an automation set it, replaces both values for
that room, the same distance either way; **Follow global drift** hands the room back to
them. A room's drift number keeps showing Allowed drift even while the room is using
**Let it get cooler by** or **Let it get warmer by**, until you give that room its own
drift; the values in use are `drift_heat` and `drift_cool` on the plan sensor and on the
drift number itself. A room can also have its own values for either side; see
[Per-room uneven drift](#per-room-uneven-drift). A room still needs to be off target by
the mode switch threshold, or that side's drift if it is wider, before the whole system
switches between heating and cooling for it. The plan sensor's `zones` list shows the
values in use per room as `drift_heat` (cooler by) and `drift_cool` (warmer by), beside
`drift`. Nothing changes on the room's tile, which keeps one target.

### Start points

Each room's plan also shows where it will start heating and cooling, worked out from its
target, these settings, Allowed drift, its own drift number, and the mode switch
threshold.

- `heat_start` and `cool_start` are where the room's head starts heating or cooling while
  the outdoor unit is already in that mode.
- `heat_demand_below` and `cool_demand_above` are where the room asks the outdoor unit to
  switch to heating or cooling.
- They are the numbers MXZ is using right now, in your temperature unit: with eco on,
  they are **Away low limit** and **Away high limit**, and a heat or cool lockout moves
  its side out to **Heat anyway below** or **Cool anyway above**.
- `start_points_mode` reads `comfort` or `eco`, and `start_points_lockouts` lists any
  lockout in use (`heat`, `cool`, or both).
- `drift_heat_from` and `drift_cool_from` say where each side's drift comes from
  (`room_cooler_by`, `room_warmer_by`, `room`, `cooler_by`, `warmer_by`, or
  `allowed_drift`; see [Per-room uneven drift](#per-room-uneven-drift)), and
  `drift_source` is `room` when either side comes from the room, else `global`.
- A disabled room shows no start points.

The room's thermostat carries the same values as extra attributes. They are read-only,
and the tile, Apple Home, Google Home, and Alexa still see one target.

### Per-room uneven drift

For power users: any room can have its own **Let it get cooler by** and **Let it get
warmer by**, in Configure → Rooms → (room), in the collapsed **Uneven drift** section.
Each takes 0.5–5 °F / 0.25–2.5 °C and is blank by default. A blank side stores nothing,
and emptying a box removes that side.

For each side, MXZ uses the first of these that is set:

1. The room's own value on this page.
2. The room's drift number, but only once you or an automation set it. A room that was
   never set, or that you handed back with **Follow global drift**, is not set, even
   though its drift number shows Allowed drift. Setting the number to exactly Allowed
   drift does count as set, because someone chose it; press **Follow global drift** to
   undo that.
3. **Let it get cooler by** or **Let it get warmer by** in Configure → Comfort →
   Advanced.
4. Allowed drift.

Each side falls through on its own. For example, with Comfort's cooler by 3 and warmer by
5, a room with its own warmer by 2 starts cooling 2 above target and heating 3 below.
Write 1.5 to that room's drift number, and it starts heating 1.5 below while still
cooling at 2 above. Leave both of a room's sides blank and nothing changes for it. As
with the global settings, a run still goes back to the target, and the room asks the
whole system to switch only past the mode switch threshold, or that side's drift if it
is wider.

The room's drift number still shows a single value, so presence automations that write to
it work as before. Its `drift_heat` and `drift_cool` attributes, like the plan sensor's,
show the drift in use on each side. `drift_heat_from` and `drift_cool_from` read
`room_cooler_by` or `room_warmer_by` for a side that comes from this page, and `room` for
one that comes from the room's drift number. `drift_source` is `room` when either side
comes from the room, else `global`.

## Give a room separate heat and cool setpoints (dual setpoint)

For power users. One target is the default, and most rooms never need more. If you'd
rather give a room a range, the way an ecobee or a Nest does, open Configure → Rooms →
(room), expand the collapsed **Dual setpoint** section, and turn on **Use separate heat
and cool setpoints**. It's off by default and set per room, so the other rooms keep one
target.

**What the room gets.** Two numbers, the room's **heat to** and **cool to** (for example
`number.mxz_coordinator_den_heat_to` and `number.mxz_coordinator_den_cool_to`), and its
thermostat shows a range: two handles on the Home Assistant tile, `target_temp_low` (heat
to) and `target_temp_high` (cool to), and no single `temperature`. The thermostat still
offers only `heat_cool` and `off`, and MXZ still picks heat or cool for the shared
outdoor unit; there's no heat or cool mode to choose, and nothing runs both at once. The
plan sensor's `zones` row for the room gains `target_low` and `target_high`.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-room-dual-setpoint.png" alt="Configure room page titled Den with the Dual setpoint section open, its description, and Use separate heat and cool setpoints switched on, before Submit."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-thermostat-dual-setpoint.png" alt="Den thermostat card showing a range of 68 °F and 74 °F, Idle, with the room at 69 °F and heat/cool selected; beside it a Den card with Den heat to 68 °F, Den cool to 74 °F, and Den target 71 °F."></td>
</tr>
<tr>
<td>Configure → Rooms → Den, with <b>Dual setpoint</b> opened and the switch on.</td>
<td>After saving, with heat to 68 °F and cool to 74 °F: the 69 °F room is inside the range, so it rests, and the target number shows the middle, 71 °F.</td>
</tr>
</table>

**How it runs.** The room heats when it falls below heat to by its drift, and runs back up
to heat to. It cools when it rises above cool to by its drift, and runs back down to cool
to. In between, it rests. With the default drift of 1 °F, a 68–74 °F room heats below 67
and stops at 68, and cools above 75 and stops at 74. That's the one difference from a
single target, where every run goes back to the target. Everything else works the same
way, per side:

- Drift, uneven drift, per-room uneven drift, and the room's drift number set how far
  past each edge the room can get before a run starts.
- The room asks the outdoor unit to switch between heating and cooling only past the mode
  switch threshold (or that side's drift, if it's wider), measured from heat to on the
  heat side and from cool to on the cool side.
- The heads get setpoints built from heat to while heating and from cool to while
  cooling.
- Fan boost speeds up by how far the room is outside the range (0 inside it).
- `heat_start`, `cool_start`, `heat_demand_below`, and `cool_demand_above` are worked out
  from heat to and cool to, and they're the numbers MXZ uses.
- Eco ignores heat to and cool to and uses **Away low limit** and **Away high limit**, as
  it always has. A heat lockout moves the heat side out to **Heat anyway below**, and a
  cool lockout moves the cool side out to **Cool anyway above**. The freeze floor, the
  overheat ceiling, priority, and hands-off are unchanged.

**The rules for heat to and cool to.** Both move in steps of 1 °F (0.5 °C) and stay inside
the room's limits (its thermostat's `min_temp` and `max_temp`). They stay at least 2 °F
(1 °C) apart. Move one too close to the other, or past it, and the other one is pushed
along to keep the gap, the way an ecobee or a Nest does. If pushing it would take it past
a limit, the change is refused. Setting both at once closer than the gap, or crossed, is
refused too. A refused change shows an error and changes nothing; nothing is ever clamped
quietly.

**The room's target number stays.** It shows the middle of the range, which can be a half
degree (for example 71.5 °F). Writing it moves the whole range so it's centered on the new
value, keeping its width, and slides it back inside the limits if needed. So a presence
automation that sets the guest room to 66 when nobody's home keeps working after you
switch that room to a range.

**Turning it on and off.** Turning it on starts the range around the room's target at the
minimum gap: a 70 °F target becomes 69–71 °F, and 21 °C becomes 20.5–21.5 °C. Turning it
off while the integration is loaded sets the target to the middle of the range, rounded
to the step (exact halves round to the even step, as the tile does: 68–73 °F becomes 70),
and removes the heat to and cool to numbers. A restart keeps the range. If a restored
range no longer fits the head's limits with the gap, it's re-centered at the gap, and the
room's `control_reasons` say so until you next change it.

A save that only turns the option on or off is applied in place, with no reload: the
room's thermostat stays available, and its features, range, and target change in one
step. That's what lets Apple Home pick up the change (see below), and the rest of the
system isn't disturbed: the other rooms keep their runs, and the mode-change wait isn't
restarted. A save that also changes something else (for example, the room's uneven drift
on the same page) reloads the integration, as every other save does, with the same result
for the room. After such a save, reload the HomeKit bridge (Settings → Devices & services
→ HomeKit Bridge → Reload) or restart Home Assistant so Apple Home shows the change. To
avoid that, save the toggle on its own.

**Voice and other apps.** These follow Home Assistant's own bridges:

- **Apple Home** shows the room in Auto with a heating and a cooling threshold. Home
  Assistant's HomeKit bridge rebuilds a thermostat when its features change while it
  stays available, so a toggle saved on its own reaches Apple Home without reloading the
  bridge; one saved with other changes needs a bridge reload or a restart (see above).
  This is read from the bridge's code and tested against a copy of its rule. On the
  maintainer's system, the bridge's store was rewritten with the two thresholds after a
  toggle saved on its own, with no bridge reload; it hasn't been checked on an iPhone
  yet. Apple Home sends a threshold change as a pair with the other one unchanged, so
  the push rule applies. When you drag one threshold past the other, though, Home
  Assistant's HomeKit bridge pushes first: it sets the other one 5 °C (9 °F) away,
  within the room's limits, so you see that wider push rather than MXZ's 2 °F (1 °C).
- **Google Home** shows the low and high setpoints in heat-cool, and setting the range
  sets both. A single number for the room ("set the living room to 70") reaches it as one
  temperature, which Home Assistant refuses for a range, so nothing changes. Google hears
  about the refusal only when Report state is off; with it on, Home Assistant answers
  Google before the refusal.
- **Alexa** shows a lower and an upper setpoint in Auto, and "make it two degrees warmer"
  moves both. A single target from Alexa is refused by Home Assistant after Alexa has
  already answered, so Alexa may say OK while nothing changes.
- **Turning it on or off, for Google and Alexa.** Through Home Assistant Cloud, the
  thermostat's changed features make Google sync and Alexa rediscover it on their own,
  with or without a reload. A manually set up Google Assistant or Alexa skill isn't told,
  so ask it to sync or discover devices, as for any new device. Neither app was checked on
  a phone.
- **Assist** (Home Assistant's own voice) can't set a range: its set-temperature intent
  only matches thermostats with one target. `climate.set_temperature` with only
  `temperature` is refused for a range room too; send `target_temp_low` and
  `target_temp_high`. For one number in an automation or a script, write the room's
  target number, which moves the range.

## Run one room from its own remote

**Let its own remote run it while disabled** is a per-room setting in Configure → Rooms →
(room). It is off for every room, and for every room of an existing install after
upgrading, so nothing changes until you turn it on: a disabled room's head is held off,
as always.

With it on, when you disable that room (its switch, or its tile set to `off`):

- MXZ sends `off` and keeps sending it until the head reports `off`. A command the head's
  integration rejects, or one that returns while the head still reports a running mode,
  does not count. If the head is already off, nothing is sent.
- From then on MXZ sends that head nothing. Use its own remote, timer, or app. This holds
  across a Home Assistant restart. Enabling the room hands the head back to the plan;
  disabling it again sends `off` again, until it lands.
- MXZ cannot tell an `off` that never arrived from one someone undid a moment later, so
  the first `off` the head reports, with no other command of MXZ's still in flight, ends
  the parking. That includes an `off` the head's integration reports for a moment while
  it reconnects, if it does that.
- The room tile stays `off`, because its mode is the room's coordinator switch: turning
  the tile on still enables the room. Its action shows what the head is doing: the head's
  own action if it reports one, else heating, cooling, fan, or drying from its mode. In
  Apple Home that is the current state; the target, and so most likely the tile's label,
  stays Off.
- If the head runs `heat` or `cool`, that counts in the shared direction, below every
  enabled room, unless the room is marked [Safety priority](#safety-priority) and that
  sensor is `on`. Otherwise it decides the direction only when no enabled room is calling
  or still running to its target, and a direction change it causes alone does not start
  the mode-change wait, so an enabled room that then calls the other way is not held back
  by it. Once an enabled room runs in the direction it chose, the wait starts, just as if
  that room had chosen it.
- `sensor.*_plan` shows the head's vote in that room's `hands_off_vote` (its `hands_off`
  reads `parking` or `released`), and `standoff` is true when it and another room want
  opposite modes. The plan's top-level `mode_by_hands_off` is true when the vote, not an
  enabled room, set the shared mode on that update: no enabled room is calling or still
  running to its target and the vote picks a mode the enabled rooms alone would not, or
  the mode goes back after the vote ends. Such a change starts no mode-change wait. With
  **Resting mode** set to Cool or Heat it stays true while the vote holds the mode away
  from rest; on Last used it is true only on the update that makes the change. With no
  room opted in it is always false.
- If the head goes unavailable, its last heat or cool keeps counting for **Keep a lost
  head's vote for** (30 minutes by default), then stops. See
  [When a head goes unavailable](#when-a-head-goes-unavailable).
- A standby hold still holds this head too: at the hold's park mode, or `off` under an
  `eco` hold, for as long as the hold lasts. Afterwards the head is left alone again. MXZ
  does not "heal" this head out of `dry` or `auto`.

Two heads on one outdoor unit asking for opposite modes is still a standoff that the
outdoor unit settles its own way (see
[the problem in detail](HOW-IT-WORKS.md#the-problem-in-detail)): the room you run by hand
can make an enabled room wait, or wait itself.

## Safety priority

Any room, enabled or run from its own remote, can be marked **Safety priority** in
Configure → Rooms → (room): an optional binary sensor reference. It is unset for every
room, so nothing changes until you wire one.

While that sensor reads `on`, the room's want for heat or cool outranks every other
room's, enabled or run from its own remote, regardless of room priority. That includes
an enabled room that is calling or still running to its target, which an ordinary vote
never overrides. If two rooms marked Safety priority are `on` and want opposite modes,
room priority decides between them. A tripped
[freeze floor](#freeze-floor-and-overheat-ceiling) still ranks above it, and a tripped
overheat ceiling shares its tier, settled by room order.

Taking the direction from an enabled room that is calling or still running to its target
parks that room's head, so it starts the mode-change wait like any other reversal: a
sensor that flaps on and off cannot reverse the compressor twice inside one wait.
Deciding the direction while no enabled room is calling or still running does not start
the wait, the same as an ordinary vote. A wait already running still applies to the
safety room's want too. `sensor.*_plan` shows each room's sensor in its
`safety_priority`, and the top-level `mode_by_safety` is true while a room marked Safety
priority is asserting and its want is the shared mode.

What the sensor should mean:

- **It raises a want the room already has, and creates none.** It only makes the room's
  own heat or cool want win: an enabled room's call, or the heat or cool a head run from
  its own remote is running. An enabled room that is not calling (including one coasting
  to its target inside its band), or a head that is off, fan-only, or drying, has no want
  to raise, and the sensor does nothing for it. It never starts heat or cool by itself,
  so it is not freeze protection.
- **A standby hold limits it.** An `off` or `fan_only` hold parks every head, the safety
  room's included. Under an `eco` hold (the default), an enabled room calls only past the
  Away limits, so the sensor can raise only such a call. Under any hold, a head run from
  its own remote is held and casts no vote.
- **Only the exact state `on` counts.** `off`, `unavailable`, `unknown`, or a missing
  entity is no priority.
- **A sensor stuck `on` keeps that room's want on top for as long as it is on**, and a
  room wanting the other mode waits the whole time. Drive it from an automation or
  template with a clear `off` condition, and consider what happens if it sticks.
- Reserve it for a real floor or ceiling condition in that room, not everyday comfort: an
  override that is on every day defeats the priority order.

For a disabled room that must not freeze or overheat, use
[Freeze floor and overheat ceiling](#freeze-floor-and-overheat-ceiling) instead: that does
start heat or cool.

## When a head goes unavailable

A head that drops off the network reads `unavailable` in Home Assistant, or disappears
from it. MXZ can neither see what that head is doing nor send it anything, but it may
still be running on the shared outdoor unit. So MXZ keeps counting what it last saw for a
while, then lets go:

- **For a time limit, its last vote still counts (`stale`).** A room run from its own
  remote keeps the heat or cool its head last showed. An enabled room keeps its demand and
  its run to target in the heat-versus-cool decision, as before. If a room run from its
  own remote has its Safety priority sensor `on`, its last heat or cool keeps its safety
  rank while stale, so for up to the limit it can take the mode from an enabled room that
  is calling. Holding the last vote keeps a short dropout from swinging the shared mode.
- **Then it expires (`expired`).** After **Keep a lost head's vote for** (Configure →
  Comfort → Advanced: 1 to 240 minutes, or blank for 30), the vote stops counting. A
  room run from its own remote no longer sets the direction, and an enabled room stops
  holding the shared mode, so MXZ can switch it for a room waiting for the other mode.
  MXZ keeps sending an enabled room's lost head its usual commands, but a head that
  loses its connection to Home Assistant doesn't take them: it keeps running its last
  command. Mitsubishi's outdoor unit stays in that head's mode, so a room that needs the
  other mode may wait in standby until the head reconnects or is turned off at the unit.
  On the maintainer's system, a lost head kept heating the whole time it was offline,
  about 7.5 minutes, and the other room sat in standby after the vote expired; when the
  head reconnected, MXZ turned it off and the other room started cooling, and no faults
  were seen.
- **A head that comes back counts again at once (`live`).** Any reversal that follows
  waits out the mode-change wait as usual.
- **Kept as before:** an enabled room whose Safety priority sensor is `on` keeps its want
  after the limit, and a tripped freeze floor or overheat ceiling is never dropped by it.
  A floor whose own head is unavailable already casts no want, as before.
- **Only a lost head counts.** `unknown` means the head is there without a mode: it is in
  sight, and it has no vote, as before. A head that only reports nothing new is in sight
  too.
- **Nothing about it survives a restart or reload.** Each head starts `unknown` until it
  is seen. A room run from its own remote then has no last vote, so it holds nothing. An
  enabled room's demand counts for the same time limit from the start (heads are briefly
  unavailable while Home Assistant starts), then expires.

`sensor.*_plan` shows each room's `vote_status` (`live`, `stale`, `expired`, or `unknown`)
and `lost_since`. Before 3.11.0, a room run from its own remote lost its vote the moment
its head went unavailable, and an enabled room held the shared mode for as long as its
head was gone.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-plan-vote-stale.png" alt="The Plan sensor's Details: state heat, then Studio's zones row with engage heat, a control reason that climate.studio_head is unavailable, vote_status stale, and lost_since 2026-10-28T07:21:56."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-plan-vote-expired.png" alt="The same Plan Details 75 seconds later: Studio's row still shows engage heat, now with vote_status expired and the same lost_since."></td>
</tr>
<tr>
<td>Studio's head has just gone unavailable (an invented test head, with <b>Keep a lost head's vote for</b> set to 1 minute): its vote is <code>stale</code>.</td>
<td>Past the limit it reads <code>expired</code>. <code>engage</code> still shows Studio's own call, but it no longer counts.</td>
</tr>
</table>

## Freeze floor and overheat ceiling

Each room can have a **Freeze floor** and an **Overheat ceiling**: two optional numbers in
Configure → Rooms → (room) → **Freeze floor and overheat ceiling**, in your Home Assistant
temperature unit (floor 35–65 °F / 2–18 °C, ceiling 75–100 °F / 24–38 °C). Both are empty
for every room, including every room of an existing install, and an empty field stores
nothing, so nothing changes until you fill one in. Saving a room page for anything else
never sets one. 50 °F (10 °C) is a typical freeze floor.

### What it does

- **It acts only while the room is disabled**, held off, or run from its own remote. An
  enabled room below its floor just calls heat as an ordinary room; the floor does not
  lift it.
- **Below the floor MXZ heats the room; above the ceiling it cools it.** This can turn on
  a head you hold off. It runs the head until the room sensor reaches the clear line,
  3 °F (1.5 °C) back inside (a 50 °F floor clears at 53 °F), then turns the head off. It
  trips again only past the floor or ceiling again.
- **The setpoint is the clear line**, clamped to your setpoint range and the head's own
  limits: with the default 59 °F lowest setpoint, a 50 °F floor commands 59 °F. Never the
  away band. Over a person's own heat it keeps their setpoint if it is warmer (below). MXZ
  ends the run on the room sensor; if the head reads warmer than the room, its own
  thermostat can stop before the room reaches the clear line, and the no-progress limit
  below then ends the run.
- **Where it ranks.** Freeze first. A tripped freeze floor's heat beats every other want
  in any room order, including a [Safety priority](#safety-priority) room that wants
  cool, earlier in the order or not. A tripped overheat ceiling's cool beats every
  ordinary want; against a Safety priority room that wants heat, the earlier room in
  priority order wins. Two trips of the same kind want the same mode, so both run. The
  mode-change wait still applies; while the room waits for its mode its head is held
  `off` (never `fan_only`), and a flip a floor causes starts the wait like any other.
- **It never writes the fan.** A floor run sends only the mode and the setpoint, so a fan
  speed someone picked on that head stays.
- **It hands back with the head off.** A held-off room goes back to being held off. A room
  run from its own remote is parked off and then left alone again, as when you first
  disable it; MXZ's own heat or cool never counts as that room's vote.
- **Standby hold and Coordinator.** It still runs under an `eco` standby hold, the default
  for a load-shed, so a held-off or own-remote head can start during one. It never runs
  under an `off` or `fan_only` hold. With Coordinator off it sends nothing; a head it was
  running keeps its last command.

### When someone was already running the head

- **Unless someone was already running it that way.** If a person was running a room run
  from its own remote in the floor's direction when it tripped (heat for a freeze floor,
  cool for an overheat ceiling), MXZ runs the head at the higher of each of their
  setpoints and the clear line's (for a ceiling, the lower), so a person heating at 70 °F
  keeps 70 °F and one heating at 55 °F gets the clear line's 59 °F. It gives the head back
  in their mode, at their latest setpoint (a setpoint they change during the run counts,
  below), when the run ends: the room clears, its sensor or head is lost, or the run
  stalls. The room is theirs again at once. Their latest setpoint survives a Home
  Assistant restart during the run. If they change the head's mode during the run, MXZ
  stands down (below) and forgets their earlier setting. A run MXZ started from `off` or
  from another mode runs at the clear line and still hands back with the head off.
- **If the head refuses the hand-back**, MXZ tries once more at the next update, but only
  while the room is still theirs and the head still shows MXZ's command; a change anyone
  made to the head since cancels it. That one retry is dropped, with no notice, if the
  next update cannot reach the room: Coordinator off, a standby hold, a vane kick on that
  head, the plan not ready, the head unavailable, or a command to that head still in
  flight. Refused again, MXZ leaves the head as it is and sends a notice saying it could
  not be handed back. That notice counts toward the limit of one notice per room per hour
  (below): if it is held back, it goes out after the hour only while the room is still
  released and the head still shows MXZ's command, and is dropped otherwise. A hand-back
  that could not be sent at all is not sent later: with Coordinator off the head keeps
  what it last had. A hand-back that lands during an `eco` standby hold is dropped, the
  head is turned off, and the person's heat does not come back when the hold ends. An
  `off` or `fan_only` hold parks the head as it parks every head. A known limitation: if
  the floor trips again before a refused hand-back lands, what MXZ remembers and hands
  back at the next clear is what the head shows then, MXZ's setpoint in the person's
  mode, not the setpoint they had.
- **A person at the head.** On a held-off room, MXZ puts its heat or cool back at the next
  room-sensor report or the 15-minute check. On a room run from its own remote, changing
  the head's mode during a run hands it to that person at once: MXZ stands down until the
  room reaches the clear line or 4 hours pass, then the floor is armed again, so someone
  who keeps turning the head off in a freezing room is overridden again every 4 hours.
  Changing only the setpoint is not handing it over. On a room whose person was already
  running it that way when it tripped (above), the new setpoint becomes their setting:
  MXZ runs the head at the higher of it and the clear line's, as at the trip, and gives it
  back when the run ends. A person heating at 70 °F who turns the head up to 74 °F during
  the run keeps 74 °F, and gets 74 °F back at the end; one who turns it down to 55 °F gets
  the clear line's 59 °F at the next update, and 55 °F back at the end. For a ceiling it
  is the mirror: a lower cool setpoint stays, a higher one is held at the clear line's and
  given back at the end. On a held-off room, or a run MXZ started from `off` or another
  mode, MXZ writes the run's setpoint (the clear line's) back at the next update, until
  the run ends.

**How MXZ tells a person's change from its own.** While a command of its own to the head
is in flight, nothing the head shows counts. After it, three readings do not count: the
head showing MXZ's last command, the head showing the run's clear line at any time in the
run (MXZ may have sent it at any point, so a late report of it is MXZ's own even after the
person changed the setpoint since), and a late report of what the head showed just before
MXZ's last command. The command and the clear line are read as the head holds them: on
its own setpoint steps, within its own setpoint range, and on a single-setpoint head as
its one setpoint. After a Home Assistant restart nothing the head shows counts until
MXZ's first update of the restored run. What that means for a person:

- A setpoint changed while MXZ's command to the head is in flight counts only if the head
  still shows it once that command is done. If MXZ's command lands after it, the change
  is lost and the person has to set it again.
- A setpoint changed at the head while Home Assistant is down is not followed: after the
  restart, MXZ writes the floor run's setting back over it at its first update. Setting
  that same value again by hand is not recognised either (MXZ writes its setting back
  again) for the rest of the run, until MXZ's command to the head changes. Another value
  they set is followed as usual and changes MXZ's command, so after that the first value
  counts again too.
- A person who sets exactly the run's clear line cannot be told from MXZ's own write of
  it, so it does not become their setting: their previous setting is what they get back
  at the end. If that one is warmer (for a ceiling, cooler), MXZ puts it back on the head
  at the next update.
- A setpoint set to exactly what the head showed just before MXZ last changed it is not
  taken either; in practice that is almost always the person's recorded setting already.
- A known limitation: when a person's setting is above the clear line at one edge and
  below it at the other, MXZ's command mixes the two (59/64 °F for their 58/64 °F). That
  command is neither the clear line nor, once they change the setpoint again, MXZ's last
  command, so a late report of it after their change is taken as their setting, and is
  what they get back at the end.

### Notices and restarts

- **Notices.** MXZ notifies when a floor trips, when it cannot protect the room (`blind`,
  below), when a head rejects a floor command, and when a head refuses to be handed back
  twice, through the service you set as **Notify on drift**. With no notification service
  set, or one Home Assistant does not have (a removed phone app, a typo), each notice
  appears in Home Assistant's own notifications instead, one per room and kind (trip,
  blind, stalled, rejected, handback), so a repeat replaces the last one rather than
  piling up. Nothing is sent when the room clears. Every notice is also written to the
  Home Assistant log as a warning.
- **At most one notice per room per hour**, whatever its kind, so a flapping sensor or
  head cannot flood you. A notice held back is still logged as a warning that says it was
  rate-limited. A held-back notice goes out at the first update after the hour if it is
  still true then: a blind, stalled, or rejected room still in that state, a trip whose
  run is still going (a run that clears, ends, or stalls first never sends it), a refused
  hand-back whose room is still released and whose head still shows MXZ's command. The
  plan always shows the current state.
- **Restarts.** A run in progress, with its progress record, survives a Home Assistant
  restart. Time Home Assistant is down does not count toward the 2-hour limit. Which
  notices were sent and the hourly limit are kept only in memory, so after a restart a
  room that is still stalled or blind notifies once more; that is intended. Held-back
  notices and a hand-back's one retry are also kept only in memory. Losing them at a
  restart is a known limitation, accepted in 3.7.0 and still present; a fix is planned.
  After a restart a held-back notice, a held-back trip notice included, or a pending
  hand-back retry is lost. A run restored after a restart is not a new trip, so a trip
  notice held back before the restart is never sent, although the plan sensor shows the
  run throughout.

### When it does not act (fail safe)

- **No usable reading, no action.** A room sensor that is unavailable, unknown, missing,
  not a number, or stale under its
  [freshness contract](CONFIGURATION.md#choose-a-sensor-freshness-contract) never trips
  the floor, and a run whose sensor is lost ends at once. The room shows `blind`. A sensor
  that flaps in and out ends and restarts the run each time.
- **Head unavailable.** A tripped room whose head is unavailable casts no want, so it
  cannot hold the other rooms' direction for a head that cannot run. It shows `blind`.
- **No-progress limit.** A sensor can be stuck at a valid number, and a room whose sensor
  has no freshness contract is never stale. So a run that has actually driven the head for
  **2 hours without 1 °F (0.5 °C) of new best reading** ends as `blind` (stalled), with a
  notice. Waiting for the mode, losing to a higher-ranked want, a standby hold, and
  Coordinator off do not count. It is armed again only when the room reaches the clear
  line or a new low (a new high for a ceiling): one full step past its worst reading in
  the last hour of driving before the stall, so a room that heated from 40 °F to 44 °F
  and then stalled is armed again at 43 °F. A dip earlier than that hour does not count:
  a room that heated to 44 °F, dipped to 41 °F when a door opened, recovered to 43.5 °F
  and stalled there more than an hour of driving later is armed again at 42.5 °F; had the
  dip been within that hour, at 40 °F. That hour counts only the run's own driving time,
  the same time the 2-hour limit counts, not time on the clock: waits do not move it, so
  a dip read during a long wait still counts until an hour of driving has passed since. A
  sensor flickering between two adjacent values does neither while its lower value comes
  back within each hour of driving. A reading held steady for over an hour of driving and
  then one step lower does count as a new low: a stalled room that read 45 °F, and nothing
  lower, through its last hour of driving is armed again when it reads 44 °F. A real but
  slow room cycles: 2 hours on, stalled until it falls another degree, on again, with a
  notice each time. A stuck sensor stays `blind` after one notice (one more after each
  Home Assistant restart).

### What it does not do

- **It only ever touches the room's own Mitsubishi head**, the one configured for that
  room. It never controls any other climate entity, and it does not change the room's
  target, enable switch, or fan.
- It is not a substitute for freeze protection that works without Home Assistant. If Home
  Assistant, the room sensor, the head, or the outdoor unit is down, nothing here can act.
  Whether the outdoor unit can cool at a low outdoor temperature is not known here; if it
  cannot make progress, the 2-hour limit ends the run.
- There is no automation recipe for it, and nothing about it applies to enabled rooms.

### What the plan shows

On `sensor.*_plan` each room in `zones` shows `floor_low`, `floor_high`, `floor_want` (the
direction its floor asks of the outdoor unit, or null), `floor_state`, and
`floor_reason`:

| `floor_state` | Meaning |
| --- | --- |
| `none` | No floor or ceiling set. |
| `enabled` | One is set, but the room is enabled, so it does not act. |
| `armed` | Watching; not tripped. |
| `heat` / `cool` | Tripped, and MXZ is running the head that way now. |
| `held` | Tripped and asking for its mode, but not running: the mode-change wait, a higher-ranked want, a standby hold, Coordinator off, or the head rejecting MXZ's command. `floor_reason` says which. A rejected command still counts toward the 2-hour no-progress limit, so a head that never accepts one stalls the run and releases the other rooms' direction. |
| `blind` | Cannot protect the room: no usable reading, head unavailable, or stalled by the 2-hour limit. `floor_reason` says which. |
| `yielded` | A room run from its own remote that a person changed during a run; `floor_reason` says when the floor re-arms. |

The top-level `mode_by_floor` is true while a tripped floor's direction is the shared
mode, and `mode_by_safety` is true then too. `standoff` counts a floor's want. A room run
from its own remote reads `floor` in `hands_off` during a run. The room tile stays `off`
(the room is still disabled); while a floor is tripped its action follows the head, as it
does for a room run from its own remote.

## Who drives the fan

Fan boost starts enabled. The further a room is from target, the faster the fan runs, up
to **Fan boost top speed** (default `high`), and it steps back down as the room closes
in. On compatible heads it follows the supported ladder
`quiet < low < medium < middle < high`, skipping absent rungs. To take over:

| You do | What happens |
| --- | --- |
| Pick a recognized manual speed | It becomes a hold. |
| Turn **Fan auto** OFF | Requests a hold at the current speed. At `auto` that action is a no-op. |
| Turn **Fan auto** ON, or set the head's fan to `auto` | Releases the hold. |
| Change targets or room drift | Nothing: they do not release the hold. The hold has no timeout. |

**What the controls show**

- The room thermostat exposes the head's exact settable fan names. Without advertised fan
  control it offers no fan control.
- The separate Fan auto switch stays registered but is unavailable without exact `auto`
  support, or after a coordinator update failure.
- An optional airflow `stage` sensor maps the firmware's reported blower stage to a
  displayed speed while automatic. It is display information, not physical verification
  of a sent command. Without that sensor the tile uses the reported fan token.
- The thermostat's fan feature follows the head's reported capabilities. HomeKit/Google
  bridge behavior when those capabilities arrive after accessory creation is untested;
  entity tests do not prove the fan control appears in the bridge without a reload.

**Vanes.** If your heads expose vertical and horizontal vane `select` entities, they
appear as swing modes on the room thermostat. A vane change on a head that is off briefly
runs it in `fan_only` so the louvre can move (a "vane kick"), then parks it again.

<details>
<summary>Fan edge cases: missing reports, queued picks, and restore</summary>

**Missing reports and queued picks**

- If support exists but current speed is missing, intent is retained and fan writes wait
  for a usable report. An explicit ON can remain pending; OFF cancels that handback.
- Manual fan picks on the room thermostat queue behind the current service call to that
  same head; a hung handler can delay the pick. Other heads can progress concurrently.

**Restore after a restart**

- A stored hold restores as held. Stored automatic ownership recognizes supported
  coordinator residue, including the issue 25 idle/delayed-report case.
- Clean switch states and unavailable-state extra restore data are distinct restore
  channels. Neither is a command journal or an arbitrary-crash guarantee.
- Missing, invalid, or stale restore records use conservative reported-speed fallback.
  With no reported speed, fallback still sends no fan write.
- A fresh install or newly added room has no saved fan record, so nothing was lost and no
  fan-hold reason is shown.
- A record that exists but cannot be used still explains itself in `control_reasons`: it
  belongs to an older entry, or it is missing or malformed. Use ON when you want automatic
  control back.
- A wall-remote speed change while HA was down may be treated as residue if the room was
  previously automatic.
- Reselecting a speed already reported can be invisible; use Fan auto OFF to express a
  hold.

</details>

## How a satisfied head idles

`fan_only` idle keeps the indoor fan moving. On the heads MXZ was developed on, idle air
smelled off and the smell stopped while the coil was actively cooling. That pattern does
not diagnose its cause; it is why **When a room reaches target** (Configure → Fan and
idle) offers three parks:

| Setting | Requested idle action |
| --- | --- |
| `Fan only` | circulates in `fan_only`. The default through 3.4.2. |
| `Off` | sends `off`, asking the head to stop. |
| `Off after drying` (default) | requests `fan_only` for a dwell after **cooling** (**Coil drying time**, default 10 min), then `off`. After heating it requests `off` immediately. |

**Upgrading keeps what you had.** An entry that never chose an idle action was running
`Fan only`; the upgrade stores `Fan only` for it, so it keeps idling that way. A choice
you saved is kept. Only new setups start on `Off after drying`. See
[Idle action default](MIGRATION.md#idle-action-default-350).

Facts to know before you switch:

- **What `off` does not prove.** The idle choice commands airflow and power; it is not a
  refrigerant-seal setting. See the
  [service-manual note](HOW-IT-WORKS.md#service-manual-note-a-parked-room-warming-in-heating-season).
- **The room's thermostat tile still reads *Idle*, not *Off*.** The room is enabled and
  coordinated; only the head is parked. Crossing the re-engage band requests conditioning
  through the ordinary debounced delivery path.
- **Fan holds survive.** A head parked off keeps a manual fan hold and comes back holding
  it. On the way into an idle `off` park (outside eco/away and standby holds) the
  coordinator first returns a boost-driven fan to `auto`. A delayed or missing report still needs the restore reconciliation and
  conservative fallback described in [Who drives the fan](#who-drives-the-fan).
- **Vane changes still work.** Changing a louvre on a parked-off head briefly wakes it
  (the usual vane kick), then parks it again.
- **Standoff losers park the same way.** A room waiting for the other mode idles in the
  same configured action as a satisfied room.
- **Eco and standby holds come first.** While eco/away is on, a satisfied room parks off
  at once, with no drying run.
