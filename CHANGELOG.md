# Changelog

Release notes for MXZ Coordinator, newest first. Upgrade steps and behavior changes for
each release are in [docs/MIGRATION.md](docs/MIGRATION.md). The GitHub
[releases page](https://github.com/dkpnw/ha-mxz-coordinator/releases) has every tag.

Hardware tests below were run on two-head and four-zone MXZ systems. Automated tests use
invented heads and sensors: they establish software behavior, not physical performance
on your equipment.

## Unreleased

Fixes and cleanups after 3.13.0. No setting or default changes.

- **A head starting to run counts at once.** In a room run from its own remote whose vote
  counts only while its head runs, the vote now counts on the head's first heating or
  cooling report. Before, the mode change that set the head going had just asked for a
  recompute, so the running report could wait out Home Assistant's 10-second refresh
  cooldown (about 4 seconds on hardware). MXZ still sends the head nothing.
- **Such a room's head asks for a recompute only once it is released.** Before MXZ's off
  lands, the head has no vote, so its `hvac_action` changing no longer recomputes. That
  off landing now also ends any run MXZ drove on the head. Before, a person turning the
  head back to heat or cool before the next recompute (up to 10 seconds in 3.13.0) could
  read as that run stopping and hold a vote for 10 minutes.
- **The plan sensor no longer reads `cool` for a moment on a reload.** On every reload,
  such as an options save, it published the placeholder plan made before any setting had
  restored, whose shared mode is always `cool`, then the real plan a few milliseconds
  later. It now stays unavailable until the first plan computed from the restored
  settings. No head was ever commanded from that placeholder.
- A head's record no longer keeps a stale run or hold from a sighting without a time.
  MXZ itself always passes one.
- **Docs.** The FAQ for a head that looks off in an enabled room now sends you to sensor
  health, the standby hold, waiting, and command status first. The setpoint explanations
  are scoped to rooms with one target and say that a dual-setpoint room uses Heat to while
  heating and Cool to while cooling. MIGRATION's `mode_by_safety` note includes tripped
  freeze floors and overheat ceilings, and the plan screenshot's alt text includes its
  Friendly name and Icon lines.
- **Tested** with the full test suite on Home Assistant 2024.12.0, 2026.2.3, 2026.9.0,
  2026.10.4, and a 2026.11 development build. **Not tested** on hardware.

## 3.13.0 (October 28, 2026)

A room run from its own remote can now count its vote only while its head is actually
running. It's off by default, so nothing changes until you choose it.

- **A room run from its own remote can count its vote only while its head runs.** A new
  per-room setting in Configure → Rooms → (room), **While its own remote runs it, its
  vote counts**: **Whenever it is set to heat or cool** (the default, today's behavior,
  stores nothing) or **Only while it is heating or cooling**. With the second, the head's
  heat or cool vote counts only while its own `hvac_action` reports it running that way,
  and for 10 minutes after it stops, so a head idle at its own setpoint stops holding the
  outdoor unit against a room that needs the other mode. A mode and action that disagree
  (the action lagging a mode change for a poll, such as `off` while still `heating`) never
  start, extend, or restart a vote or its hold; a running head that reports the opposite
  action in the same mode is treated as stopped. A head that reports no `hvac_action` counts by its mode. Safety priority, the
  enabled-room tiers, the lost-head vote, and the freeze floor and overheat ceiling are
  unchanged, and MXZ sends the head nothing for this. The plan's room rows gain
  `vote_basis` and `vote_hold_until` for rooms set to it. See
  [Count its vote only while it runs](docs/FEATURES.md#count-its-vote-only-while-it-runs).
  Reading votes from observations is
  [@csingley](https://github.com/csingley)'s idea (ha-climate-zone-sequencer
  [f911e4a](https://github.com/csingley/ha-climate-zone-sequencer/commit/f911e4a1cb70ea247205491719bb2295e2a8ebbd),
  "derive zone votes from observations, not event recency"); the action filter is
  MXZ's extension.
- **Tested** with the full test suite on Home Assistant 2024.12.0, 2026.2.3, 2026.9.0,
  2026.10.4, and a 2026.11 development build, and on hardware on a two-head system in heat
  (October 28, 2026): the vote counted while the head heated, held 10 minutes after it
  went idle, cleared the hold when it heated again, and dropped at once when the head was
  turned off or to fan only. MXZ sent the head nothing. **Not tested** on hardware: the
  cool side and the safety-priority room while idle (covered by automated tests).

## 3.12.0 (October 27, 2026)

Ready for Home Assistant 2026.11, with no change to settings or to
how MXZ controls your heads. The minimum is still Home Assistant 2024.12.0.

- **No deprecation warnings on Home Assistant 2026.11.** Home Assistant 2026.11 renames
  the temperature properties a thermostat reports, and logs a warning for every
  integration still using the old names, which stop working in 2027.11. The room
  thermostats use the new names on 2026.11 and later, and the old ones before it, and
  MXZ reads each head's temperature unit the same way, so MXZ logs no such warning.
  A head integration that hasn't moved to the new names yet can still log its own.
- **A `temperature_unit` attribute on every thermostat (Home Assistant 2026.11).**
  Home Assistant itself adds it to every climate entity, MXZ's room thermostats and
  your heads alike: the unit your system uses (°F or °C). MXZ adds nothing of its own.
- **Names you type into an entity's settings (Home Assistant 2026.11).** Home Assistant
  now puts the device name in front of them, so a room target you rename to
  "Kids' comfort" shows as "MXZ Coordinator Kids' comfort". A name you typed before
  upgrading keeps showing as you typed it, also after you rename or reorder rooms.
- **Tested** with the full test suite on Home Assistant 2024.12.0, 2026.2.3, 2026.9.0,
  2026.10.4, and a 2026.11 development build (commit 3c9da83). **Not tested** on
  hardware or on a released 2026.11.

## 3.11.1 (October 24, 2026)

A docs-only release: no code, settings, or behavior change.

- **The README displays properly in HACS.** HACS couldn't show the logo or the setup
  and Configure screenshots from the README, so they showed as stray text or blank
  boxes. The logo is now one banner that reads on both light and dark themes, and every
  HTML image and the License badge use full web addresses.
- **Install from the HACS default store.** MXZ Coordinator is in HACS's default list
  now, so [Install](README.md#install) says to search HACS for **MXZ Coordinator** and
  select Download, with no custom repository to add, and the HACS badge reads Default
  ([#47](https://github.com/dkpnw/ha-mxz-coordinator/pull/47)).

## 3.11.0 (October 23, 2026)

- **A lost head's vote goes stale, then expires.** When a head drops off the network
  (`unavailable` in Home Assistant, or gone from it), its room's last vote keeps
  counting, marked stale, for a time limit, and then expires. While it's stale, a room
  run from its own remote keeps the heat or cool its head last showed, and an enabled
  room keeps its demand in the heat-versus-cool decision, so a short dropout doesn't
  swing the shared mode. Once it expires, the room stops holding the shared mode, so MXZ
  can switch it for a room that needs the other mode. A head that comes back counts
  again at once. Before this, a room run from its own remote lost its vote the moment
  its head went unavailable, and an enabled room held the shared mode for as long as its
  head was gone. An enabled room whose Safety priority sensor is `on` keeps its want
  after the limit, and a tripped freeze floor or overheat ceiling is never dropped by
  it. None of it is saved: after a restart or reload, each head starts `unknown` until
  it is seen. The approach, and its live, stale, expired, and unknown votes, are adapted
  from [@csingley](https://github.com/csingley)'s MIT-licensed
  [ha-climate-zone-sequencer](https://github.com/csingley/ha-climate-zone-sequencer)
  (commit
  [f911e4a](https://github.com/csingley/ha-climate-zone-sequencer/commit/f911e4a1cb70ea247205491719bb2295e2a8ebbd));
  [NOTICE](NOTICE) carries its MIT notice. See
  [When a head goes unavailable](docs/FEATURES.md#when-a-head-goes-unavailable) and
  [When a head goes unavailable (3.11.0)](docs/MIGRATION.md#when-a-head-goes-unavailable-3110).
- **Keep a lost head's vote for**, a new setting in Configure → Comfort → Advanced: how
  long a lost head's vote counts, 1 to 240 minutes. Blank uses 30 minutes, the same
  default as ha-climate-zone-sequencer's, and leaving it untouched stores nothing.
- **Each room shows its vote status.** Each room in the plan sensor's `zones` list shows
  `vote_status` (`live`, `stale`, `expired`, or `unknown`) and `lost_since`, the time
  MXZ lost sight of its head. See
  [Diagnose a room that is waiting](docs/TROUBLESHOOTING.md#diagnose-a-room-that-is-waiting).
- **A lost head keeps running its last command.** A head that loses its connection to
  Home Assistant doesn't take MXZ's commands, and Mitsubishi's outdoor unit stays in
  that head's mode. So after the vote expires, a room that needs the other mode may wait
  in standby until the head reconnects or is turned off at the unit.
- **Hardware test.** On the maintainer's two-head system, running this release's code
  before the version bump, with Keep a lost head's vote for set to 3 minutes: one room,
  heating, held the shared mode while the other waited to cool. The heating room's head
  was taken offline by disabling its ESPHome entry. Its vote went stale and kept holding
  heat, then expired after the limit, and MXZ switched the shared mode to cool and sent
  the waiting room cool. That room still sat in standby for nearly 5 minutes, because
  the offline head kept heating the whole time it was gone, about 7.5 minutes, so the
  outdoor unit stayed in heat. When the head reconnected, MXZ turned it off and the
  waiting room started cooling. No faults were seen. With the head offline across a
  reload of MXZ, its vote read `unknown`, then expired after the limit, and it was
  `live` again once the head returned. A 60-minute run with both heads connected kept
  every vote `live`, and every plan update in it came from a room sensor, with no timed
  recomputes added. Saving the setting's page untouched changed nothing, and 0 and 241
  were refused. After the test, the previous install was put back, and its MXZ settings
  matched the backup apart from their saved time. **Not tested on hardware:** a room run
  from its own remote (none was set up on the maintainer's system), a head reading
  `unknown` (tests cover it), and a full Home Assistant restart with a head offline.

## 3.10.0

- **Opt-in dual setpoint, per room.** For power users who'd rather give a room a range,
  the way an ecobee or a Nest does: each room's page in Configure → Rooms → (room) has a
  new, collapsed **Dual setpoint** section with **Use separate heat and cool
  setpoints**. It's off by default, so nothing changes unless you turn it on, and a room
  that never turns it on is exactly as before. Turned on, the room gets **Heat to** and
  **Cool to** numbers, and its thermostat shows a range instead of one target (still
  `heat_cool`, with MXZ choosing heat or cool for the shared outdoor unit). It heats
  when it falls below Heat to by its drift and runs back up to Heat to, and it cools
  when it rises above Cool to by its drift and runs back down to Cool to. The two stay
  at least 2 °F (1 °C) apart, and moving one too close pushes the other. The room's
  target number stays: it shows the middle of the range, and writing it slides the whole
  range, keeping its width, so presence automations keep working. Assist can't set a
  range; write the room's target number instead. The idea comes from
  [@csingley](https://github.com/csingley)'s dual-setpoint proposal
  ([#35](https://github.com/dkpnw/ha-mxz-coordinator/pull/35)). See
  [Dual setpoint](docs/FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint)
  and [Dual setpoint (3.10.0)](docs/MIGRATION.md#dual-setpoint-3100).
- **Switching it on or off applies in place.** A room page saved with only the toggle
  changed is applied without a reload: the room's thermostat stays available, the other
  rooms keep their runs, and Apple Home picks up the range (or the single target again)
  without a reload of the HomeKit bridge. A save that mixes it with other changes, such
  as the room's uneven drift on the same page, reloads the integration as every other
  save does. After such a save, reload the HomeKit bridge (Settings → Devices & services
  → HomeKit Bridge → Reload) or restart Home Assistant so Apple Home shows the change.
- **One target stays the default.** [CONTRIBUTING.md](CONTRIBUTING.md) now says a dual
  setpoint is supported as a per-room opt-in, in place of 3.9.0's stance that MXZ
  wouldn't add one. MXZ won't make it the default, and won't add simultaneous heat and
  cool or the heads' hardware Auto.
- **Hardware test: the heat side.** On the maintainer's two-head MXZ (Home Assistant
  2026.10.2), running an earlier build of this release with one room switched on:
  turning it on started the room's 65 °F target as 64–66 °F, with its Heat to and Cool
  to numbers, and left the other room as it was. With eco off, the room at 62.2–62.4 °F,
  and the outdoor unit already heating, a 62–64 °F range stayed idle (heat starts below
  61.5), and a 63–65 °F range started heating it at once, with the head set to 63–65 °F.
  At 64–66 °F the room also asked the outdoor unit for heat, and the head got 64–66 °F.
  The minimum gap, the push, and the refusals worked as described; writing the target
  number slid the range and kept its width; eco ignored the range and showed the eco
  band, 50 and 78; and turning it off set the target to 65 and removed the two numbers.
  That run also found that the HomeKit bridge didn't rebuild its accessory with the
  range: the reload after the save left the thermostat briefly unavailable, and the
  bridge's store wasn't rewritten. That's why a switch saved on its own now applies in
  place. A recheck of the release code kept the thermostat available through the switch
  on and off, and the HomeKit bridge's store was rewritten with the heating and cooling
  thresholds, with no bridge reload and no restart. **The cool side isn't
  hardware-tested yet**: the room was at 62 °F in October, so no cooling run and no
  switch between heating and cooling were tried (tests cover those). Apple Home was
  checked from the HomeKit bridge's store, not on an iPhone, and Google Home and Alexa
  weren't checked in their apps. °C and two dual-setpoint rooms at once weren't tested
  on hardware.

## 3.9.0

- **Per-room uneven drift.** Each room's page in Configure → Rooms → (room) has a
  collapsed **Uneven drift** section with that room's own **Let it get cooler by** and
  **Let it get warmer by**, blank by default. At release, only a room's own Let it get
  cooler by had been tested on hardware. See
  [Per-room uneven drift](docs/FEATURES.md#per-room-uneven-drift) and
  [Per-room uneven drift (3.9.0)](docs/MIGRATION.md#per-room-uneven-drift-390).
- **Each room shows its start points** (`heat_start`, `cool_start`,
  `heat_demand_below`, and `cool_demand_above`, with where its drift comes from) on its
  thermostat and in the plan sensor's `zones` list. See
  [Start points](docs/FEATURES.md#start-points) and
  [Start points (3.9.0)](docs/MIGRATION.md#start-points-390).
- **Oxford commas** in the remaining docs and the setup and Configure screen text.

## 3.8.0

- **Let it get cooler by and Let it get warmer by** in Configure → Comfort → Advanced:
  how far below target before heating starts, and how far above target before cooling
  starts, blank by default (from 3.9.0, a room can also set either side itself). The
  idea grew out of [@csingley](https://github.com/csingley)'s dual-setpoint proposal
  ([#35](https://github.com/dkpnw/ha-mxz-coordinator/pull/35)). At release, only the
  heat side's start decision had been tested on hardware. See
  [Uneven drift](docs/FEATURES.md#uneven-drift) and
  [Uneven drift (3.8.0)](docs/MIGRATION.md#uneven-drift-380).
- **CONTRIBUTING.md** explains MXZ's design principles and what to do before opening a
  pull request.

## 3.7.1

- **The mode-change wait, the coil-drying wait, and the floor's own timers run on a
  monotonic clock**, so a jump in the host's clock can no longer end them early or hold
  them late. The mode-change wait change is by
  [@csingley](https://github.com/csingley) in
  [#32](https://github.com/dkpnw/ha-mxz-coordinator/pull/32). See
  [Mode-change wait on a monotonic clock (3.7.1)](docs/MIGRATION.md#mode-change-wait-on-a-monotonic-clock-371)
  and
  [Coil-drying wait and floor timers on a monotonic clock (3.7.1)](docs/MIGRATION.md#coil-drying-wait-and-floor-timers-on-a-monotonic-clock-371).

## 3.7.0

- **Safety priority**, designed and written by
  [@csingley](https://github.com/csingley) in
  [#31](https://github.com/dkpnw/ha-mxz-coordinator/pull/31), and the **freeze floor and
  overheat ceiling**: optional per-room settings in Configure → Rooms → (room), unset for
  every room until you set them. At release, only the freeze floor had been tested on
  hardware. See [Safety priority](docs/FEATURES.md#safety-priority),
  [Freeze floor and overheat ceiling](docs/FEATURES.md#freeze-floor-and-overheat-ceiling),
  [Safety priority (3.7.0)](docs/MIGRATION.md#safety-priority-370), and
  [Freeze floor and overheat ceiling (3.7.0)](docs/MIGRATION.md#freeze-floor-and-overheat-ceiling-370).

## 3.6.0

- **Run one room from its own remote**, built from
  [@csingley](https://github.com/csingley)'s
  [#27](https://github.com/dkpnw/ha-mxz-coordinator/pull/27) and
  [#28](https://github.com/dkpnw/ha-mxz-coordinator/pull/28): with **Let its own remote
  run it while disabled** on, disabling a room turns its head off, then leaves it to its
  own remote; its heat or cool counts below every enabled room unless the room is marked
  Safety priority. Off for every room until you turn it on, and hardware-tested by
  @csingley on a 4-zone MXZ-4C36NA2. See
  [Run one room from its own remote](docs/FEATURES.md#run-one-room-from-its-own-remote)
  and
  [Run one room from its own remote (3.6.0)](docs/MIGRATION.md#run-one-room-from-its-own-remote-360).

## 3.5.0

- **New setups dry the coil, then turn off** (`off_after_dry`); an upgraded entry keeps
  `fan_only`. See [Idle action default](docs/MIGRATION.md#idle-action-default-350).
- **FAQ and troubleshooting**, and **stock AUTO as the Mitsubishi manuals describe it**.
  See [Troubleshooting and FAQ](docs/TROUBLESHOOTING.md) and
  [The problem in detail](docs/HOW-IT-WORKS.md#the-problem-in-detail).

## 3.4.1 and 3.4.2

- **No startup warning for a sensor that is still loading**; MXZ warns once if it is
  still unusable 40 seconds after start. See
  [Startup warning for a sensor that is still loading](docs/MIGRATION.md#startup-warning-for-a-sensor-that-is-still-loading-342).
- **Configure is a menu.** Six pages, each saving only what it shows. A **Needs
  attention** line names any older saved value a page would now refuse. See
  [Configure is a menu of pages](docs/MIGRATION.md#configure-is-a-menu-of-pages-341).
- **No more setpoint re-sends** to heads that only hold 0.5 °C steps (ESPHome CN105).
  See [Setpoints on °C-native heads](docs/MIGRATION.md#setpoints-on-c-native-heads-in-a-f-system-341).
- **A new logo and icon**: the dial in the README banner, shipped in the integration's
  `brand/` folder.

## 3.4.0 and earlier

**Upgrading from 3.3.0 or earlier?** 3.4.0 changed setup, sensor handling, the room
lifecycle, and command delivery. Read [Migration](docs/MIGRATION.md#upgrading-to-340)
before you upgrade. [docs/MIGRATION.md](docs/MIGRATION.md) also covers every earlier
change back to the YAML package.
