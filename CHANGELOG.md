# Changelog

Release notes for MXZ Coordinator, newest first. Upgrade steps and behavior changes for
each release are in [docs/MIGRATION.md](docs/MIGRATION.md). The GitHub
[releases page](https://github.com/dkpnw/ha-mxz-coordinator/releases) has every tag.

Hardware tests below were run on two-head and four-zone MXZ systems. Automated tests use
invented heads and sensors: they establish software behavior, not physical performance
on your equipment.

## Unreleased

- **Resting mode no longer cuts a room's run short.** With Resting mode set to Cool or
  Heat, a room still running to its target in the other mode kept that mode only until it
  came back inside the Mode switch threshold; once the mode-change wait had passed,
  Resting mode took it and parked the head short of its target. Resting mode now waits
  until the room reaches its target. Another room's call for the other mode, or a pick on
  the shared-mode select, still takes the mode as before.
- **A mode a hands-off vote chose is remembered across a reload or restart.** When an
  enabled room then runs in it, the mode-change wait starts, as it already did without a
  reload or restart in between. Before, a reload or restart forgot that the vote chose
  the mode, and the room's next call the other way reversed the outdoor unit at once.
- **The shared mode is stored at once with the time of the last change.** The record that
  keeps the mode-change wait across restarts (3.18.0) now also keeps the shared mode,
  written at each change, and a start prefers it over Home Assistant's restore data
  unless that is newer. Before, a crash or power cut soon after a change could restart MXZ
  in the earlier mode, held there for the rest of the wait the change had started. A
  record written by an earlier version has no mode in it and is read as before.
- **Cloud heads (beta): fan boost no longer stalls behind a new setpoint.** After a heat
  or cool write that sent no fan, MXZ made no fan decision for that write's whole confirm
  wait (up to 10 minutes), so a boost step that came due waited, and a speed picked on
  the head was noticed only when the wait ended. Now a step that comes due inside that
  wait goes out on its own once the write interval allows, and a picked speed is held
  from the next plan. The 5-minute step limit and the write interval still apply, a
  write that sent a fan still waits out its wait, and an off or fan-only write never has
  a fan sent after it.

- **A saved Allowed drift that isn't a number no longer stops MXZ from loading.** It now
  reads as the default Allowed drift (1 °F, 0.5 °C) until you fix it. A saved Let it get
  cooler by or Let it get warmer by, global or for one room, that isn't a number already
  read as blank. All three are now named once in the log when MXZ loads, and the
  Configure menu still points to the page where you fix them. Defaults are unchanged.
- **Docs.** The README's Configure table names every setting and section on a room's
  page, including **Let its own remote run it while disabled** and **Freeze floor and
  overheat ceiling**. The Mushroom card note now says its 2-second wait is a default a
  theme can change. The 3.9.0 notes list its CONTRIBUTING.md change again.

## 3.19.0 (November 3, 2026)

A room thermostat shows a change at once, and changing an MXZ entity's ID no longer acts
like a restart. Settings, defaults, and what MXZ sends the heads are unchanged, and no
migration is needed.

- **Release downloads you can check.** Each published release gets
  `mxz_coordinator.zip`, the `custom_components/mxz_coordinator` folder at that tag, with
  a GitHub build-provenance attestation. Check a download with
  `gh attestation verify mxz_coordinator.zip -R dkpnw/ha-mxz-coordinator`; see
  [Verifying a release download](docs/RELEASING.md#verifying-a-release-download). HACS
  still installs from the repository as before.
- **CI:** every action is pinned to a commit SHA, and the CI contract tests check those
  pins. Dependabot proposes weekly updates for the actions and the pip test
  requirements. The per-lane Home Assistant locks under `requirements/` are left out:
  they are only changed on purpose.
- **Repository:** a security policy ([SECURITY.md](SECURITY.md): report a vulnerability
  privately from the Security tab), CODEOWNERS, a pull request template, and a bug
  report template.
- **Tests:** entity ID renames. New tests rename every MXZ entity, both the way Home
  Assistant does today (remove and add again) and the way 2027.11 will (in place,
  without running MXZ's add and remove steps). They check that every control still
  acts on its own room under the new ID, and that a rename changes nothing else: every
  value, everything MXZ remembers, and a paused run. Both ways pass (see Fixed).

### Fixed

- **Changing an MXZ entity's ID no longer acts like a restart.** Home Assistant renames
  MXZ's entities by removing them and adding them again, and MXZ treated that as a
  fresh start. On Home Assistant before 2026.11, renaming a room's target number could
  change the target to the head's current setpoint. On every version, renaming the
  switches could make MXZ forget a run paused by a mode conflict, and on 2026.11 and
  later, renaming a Fan auto switch made MXZ check a held fan speed again as it does
  after a restart. Before 2026.11, changing an ID back to an earlier one could also
  bring back the values from before the first change. Now a rename only changes the
  ID. A restart or a reload still restores everything as before.
- **A room thermostat shows a change at once.** A dashboard card (Mushroom, tile) used
  to show a room's old mode or setpoint until the plan that follows a change finished,
  up to 10 seconds later on a test install, so a second tap could undo the first. Now
  the new mode (Off, Heat/Cool or Dry), target, or Heat to and Cool to shows at once,
  whether it was set on the thermostat or on the room's enable switch or number. Dry
  shows from the moment it is picked, and Heat/Cool or Off from Dry shows at once too.
  What MXZ sends the heads, and when, is unchanged.
- **A Dry pick that doesn't take leaves Dry at once.** If the head refused Dry, or didn't
  report it within the pick's wait (60 seconds, or a cloud room's confirm wait if
  longer), the thermostat kept showing Dry until the plan that follows ran, up to 10
  seconds later. It now shows Heat/Cool at once. What MXZ sends the heads, and when, is
  unchanged.
- **The thermostat's action follows Dry at once.** Right after Dry was picked, the
  thermostat could show Dry with `idle` while the head was already drying, and after
  Heat/Cool from Dry it could show `drying`, until the plan that follows ran. The action
  now follows the same Dry as the mode: the head's own action (`drying` once it is in
  Dry) from the pick, and the plan's action from Heat/Cool or Off. A head being parked
  for heat still shows `idle`. Display only.
- **README:** a Dashboard cards section with a Mushroom climate card setup tested with
  Mushroom v5.2.3. It notes that the card's + and - buttons wait about 2 seconds after
  the last tap before sending the setpoint; that wait is the card's, not MXZ's.
- **Tests:** a restart part way through the mode-change wait, at the default 600
  seconds, in both directions. A change, 400 seconds, the room now wants the other
  direction, a restart: the mode holds, and still holds 10 seconds before the end. Once
  the wait is over, the change goes through and its new time is stored.
- **Tests:** a Dry pick the head refuses, or never reports, shows Heat/Cool before the
  plan that follows runs. The thermostat's action shows `drying` at once on a pick, and
  stops at once on Heat/Cool from Dry. Each test failed before its fix.

## 3.18.0 (November 2, 2026)

Comfort priority (off by default) and a mode-change wait that survives restarts.
Existing installs keep their settings; with no Comfort priority entity set, MXZ decides
exactly as before. See [docs/MIGRATION.md](docs/MIGRATION.md).

- **The mode-change wait survives a reload or restart.** The wait between two cool/heat
  changes (Minimum time between mode switches, 600 seconds by default) used to start
  over every time the integration reloaded or Home Assistant restarted, so a house that
  needed to switch could wait the full 10 minutes again. MXZ now stores when the last
  real change happened and, after a reload or restart, waits only for what is left.
  A fresh install, and the first start after this upgrade, have no stored change and
  don't wait. A stored time in the future (the clock was set back) counts as now, so
  the wait is never longer than the setting. The setting and its default are unchanged.
- **The wait shows on each room thermostat.** `mode_change_hold_remaining` is the whole
  seconds left as of the thermostat's last state update, not a live countdown (`0` when
  the mode may change now), and `mode_change_hold_until` is the exact time the wait
  ends, in UTC (`null` when there is none). Both clear on time when the wait ends. There
  is no button to skip it.
- **CI:** the normal test-suite step may run for 600 seconds instead of 300, as the
  fakeboot step already does. A test job that runs both may take 35 minutes instead of
  25.

### Added

- **Comfort priority (off by default).** Each room can have an optional **Comfort
  priority** entity, in Configure → Rooms → (room), under Safety priority. When rooms want
  opposite modes, a room whose entity is `on`, or reads a higher number, goes ahead of
  rooms with less; rooms with the same value keep the room order. It can be an on/off
  entity (`binary_sensor`, `input_boolean`) or a number entity (`number`,
  `input_number`, numeric `sensor`). A reading that is unavailable, unknown, missing,
  negative or not a number counts as no priority, so it can move a room up but never
  down. Freeze floors, Safety priority rooms, overheat ceilings and the mode-switch wait
  still come first, and a room run from its own remote still never beats an enabled
  room's real call. An on/off entity that has been `on` for longer than the new
  **Comfort priority stale limit** (Configure → Comfort → Advanced; 12 hours when blank,
  0 for no limit) stops counting. The plan's room rows show `comfort_priority_entity`,
  `comfort_priority` and `comfort_priority_status`, and `mode_by_comfort_priority` is
  true only when comfort priority changed the mode. With no entity set, MXZ decides and
  stores exactly what it did before. The idea is
  [@csingley](https://github.com/csingley)'s comfort priority rank in
  [ha-climate-zone-sequencer](https://github.com/csingley/ha-climate-zone-sequencer)
  (commit f911e4a); no code was copied. Not tested on hardware yet. See
  [Comfort priority](docs/FEATURES.md#comfort-priority).

## 3.17.0 (November 2, 2026)

Generic heads (beta), off unless you choose them, and cleanup after 3.16.0. Existing
installs run exactly as before, with the same defaults, and need no migration.

**Generic heads are a beta and untested on our side.** We have no non-Mitsubishi unit, so
only the automated test suite, on invented heads, covers them. If you try one, please
[open an issue](https://github.com/dkpnw/ha-mxz-coordinator/issues) or post on the
[forum thread](https://community.home-assistant.io/t/one-temperature-per-room-on-a-mitsubishi-mxz-multi-split-mxz-coordinator-hacs/1018382), whether it worked or not.

- **Generic heads (beta).** A third head type, next to Local and Cloud (beta), for any
  Home Assistant `climate` entity that offers heat and cool and isn't a Mitsubishi head.
  Choose it with **Generic heads (beta)** on setup's first page, or **Head type** on a
  room page; nothing suggests it. MXZ drives it as a cloud head (mode on its own call,
  one setpoint past the target by the drive offset), with a 30-second write interval and
  confirm wait by default. Only being unavailable for 15 minutes takes it out of MXZ's
  control; a head that sends nothing for hours while idle is still controlled. A generic
  head can have an entry of its own (one head); one local or cloud head is still refused.
  A head without heat and cool, or without off or fan only, is refused in setup and on
  its room page, and an ESPHome head can't be generic. Missing fan modes, step, current
  temperature, `hvac_action`, dry or swing make MXZ do less, never fail. Tested on
  invented heads only. See [Generic heads (beta)](README.md#generic-heads-beta).
- **Apple Home note.** The Allow Dry mode note now says only that Apple Home may show a
  Cool choice for that room. Auto was always there. Changed in the README, FEATURES, and
  the room page's help text.
- **Docs.** FEATURES says a dual-setpoint room's thermostat shows its Heat to and Cool
  to, not one target. CONFIGURATION says a cloud head's drive offset that you set isn't
  converted if Home Assistant switches between °F and °C. The README says that inside a
  cloud head's confirm wait, a fan boost step and a fan speed you pick wait for the wait
  to end.
- **Tests.** New tests cover the action vote's settle wakeup (a parking room arms none,
  one still due at unload computes nothing, and one that fires early re-arms for the
  rest), a head report that changes only the temperature asking for no refresh, and a
  cloud fan step waiting out the confirm wait of a write that sent no fan. The invented
  heads of the issue-25 harness use Home Assistant's new `native_` temperature names.
- **Code tidy.** Allow Dry's step that decides held or parked no longer reads a floor
  list the rooms' calls already include. The plan is the same.
- **CI.** A failed pass's log tail is capped at 64 KiB as well as 200 lines, and the
  fresh-boot clock reads its two-minute uptime again as each test starts.

## 3.16.0 (November 1, 2026)

New rooms now start with a 45 °F (7 °C) freeze floor and a 6-hour sensor freshness limit.
Existing rooms are unchanged; you can set both on the room page.

- **Defaults for new rooms.** A room that setup or Reconfigure creates stores a freeze
  floor of 45 °F, or 7 °C on a Celsius system, with no overheat ceiling, and a sensor
  freshness profile of Maximum age 360 min on the **Every update is a new reading**
  basis (startup grace defaults to the same 6 h). They are ordinary values, shown on the
  room page (Configure → Rooms → (room)), editable and clearable; a cleared one stays
  cleared. The setup and Reconfigure summaries list them for each new room.
- **Existing rooms are unchanged.** Nothing supplies these values at runtime and the
  config entry stays at version 2.2: a room saved before 3.16.0 keeps exactly what it
  stored, and saving any Configure page, the room's own page included, adds nothing.
- **A stale sensor's floor notice says what to do.** When a room's sensor goes stale,
  the floor's blind notice names the window that ran out (6 h on a new room) and the two
  fixes: clear the room's Maximum age, or use a sensor that reports periodically. A
  sensor that reports only when its reading changes can go stale in a room that holds
  steady. The room page's help text for both settings says the same.
- **Dry on the room thermostat.** With **Allow Dry mode** on, for a head that lists
  `dry`, the room's MXZ thermostat offers Dry beside Off and Heat/Cool. Picking it
  enables the room and sends the head `dry` once, with no fan, vane or setpoint write (a
  fan speed you picked is kept); from then on it is the 3.15.0 Dry: the room's target
  waits, the head votes cool, and heat for another room parks it with one notice. The
  thermostat shows Dry whenever MXZ keeps the head in Dry, a Dry set on the head's own
  remote included. Heat/Cool takes the head back to the room's plan; Off turns the room
  off. Dry is still offered with Cool lockout on, and a stale room sensor doesn't end it.
  Turning the option off, or a head that stops listing `dry`, ends Dry with one notice.
  A room without the option, every room saved before 3.15.0 included, offers Off and
  Heat/Cool exactly as before. Requested by @WhimsySpoon on the
  [Home Assistant forum](https://community.home-assistant.io/t/1018382/14).
- **Apple Home note.** The docs and the Allow Dry mode help text now say that Home
  Assistant's HomeKit bridge shows Dry as Cool, so Apple Home may show a Cool choice (and
  Auto) for a room with the option on; leave it off if you use Apple Home and don't need Dry.
- **Tested** with the full test suite (2750 tests, full and fakeboot) on every supported
  Home Assistant lane, and on hardware: on the Bedroom CN105 head on a house system on
  2368dfa, upgrading an existing room added no settings, picking Dry sent one `dry` with
  no fan, vane or setpoint write, a fan pick in Dry was kept, Dry and a manual fan pick
  survived a Home Assistant restart with no writes to the head, Heat/Cool and Off worked,
  and turning Allow Dry off put the head back on plan with one notice. **Not tested** on
  hardware: the defaults on a newly added room, and how Apple Home shows Dry.

## 3.15.0 (October 30, 2026)

Allow Dry mode, opt-in per room. Nothing changes for a room until you turn it on.

- **Allow Dry mode.** A new per-room option (Configure → Rooms → (room)), off by default
  and shown only for a head that lists `dry`. Off, a head put in `dry` is put back after
  about 20 seconds, as before. On, while the room is enabled, a head someone puts in Dry
  stays there: MXZ sends it no mode, setpoint, fan or vane write, the room's target waits
  (its demand reads `neutral`), and the head is a cool vote at the rank of a head run from
  its own remote, below any enabled room's heat or cool call, a Safety priority want and
  a tripped freeze floor; the room's own Safety priority sensor doesn't raise it, and Cool
  lockout blocks it. When another room wins heat, MXZ parks the Dry head at its idle
  setting and sends one notice, and never turns Dry back on (MXZ never sends `dry`, so an
  ESPHome head's commands are unchanged). Leaving Dry starts the coil-dry run, as leaving
  cool does. A fan speed picked in Dry is held like any manual pick. After a restart a
  head still in Dry is left alone and votes again. Cloud heads follow the same rules, and
  a report of Dry inside a confirm wait isn't taken as someone's Dry. The plan's row for
  such a room adds `allow_dry`, `dry` (`held` or `parked`) and `dry_vote`, and its
  thermostat shows `drying`. Prompted by a tip from @mikepell007 on the
  [Home Assistant forum](https://community.home-assistant.io/t/1018382).
- **Clearer coil-dry label.** The idle choice **Off after drying** is now **Dry the coil
  (fan only), then off**, in the UI, its translation and the docs, so it isn't mistaken
  for the heads' Dry mode. Still stored as `off_after_dry`; nothing migrates.
- **Tested** with the full test suite (2701 tests, full and fakeboot) on every supported
  Home Assistant lane, and on hardware: on the Bedroom CN105 head on a house system on
  39b008c, with Allow Dry off a head put in Dry was put back (in about 34 seconds), with it
  on MXZ left Dry alone with no writes, leaving Dry ran the 10-minute coil dry, and a manual
  fan pick survived a Home Assistant restart. **Not tested** on hardware: Dry as a cool
  vote (the house's seasonal Cool lockout was on) and parking a Dry head when another room
  wins heat; both are covered by the test suite only. MXZ-run dehumidifying to a humidity
  target is not part of this release.

## 3.14.0 (October 29, 2026)

Cloud heads (beta). They add a head type and three cloud settings to a room's page.
Nothing changes for an existing room until you set it to cloud, and ESPHome (CN105) heads
are always driven as before.

- **Cloud heads (beta).** A head that reaches Home Assistant through a cloud integration
  (MELCloud, or Kumo Cloud through comfort_HA or hass-kumo) can now be coordinated as a
  cloud head, opt-in per room. Setup asks about each head from the `melcloud`,
  `kumo_cloud` or `kumo` integration with **Cloud (beta)** filled in for you to confirm;
  any room can be set on its page (Configure → Rooms → (room) → **Head type**). An ESPHome
  head is always local, and an existing room stays local until you choose otherwise: a
  room's page shows Local until you choose Cloud (beta) there, so saving it for anything
  else never makes a room cloud. For
  a cloud head MXZ sends the mode on its own call, then one setpoint (never a low/high
  pair) pushed past the target by the room's **Setpoint drive offset** while the room runs
  (2 °F / 1 °C by default, 0 for the bare target), rounded to the head's own step and
  kept inside its limits; the room sensor still decides when the room is done. It writes
  a head at most once per **Write interval** (60 s, 30-300 s), sending the newest change
  when the interval ends, and waits a **Confirm wait** after each write (90 s, up to 600 s)
  in which it sends nothing again and reads a report of an older state as late, not as
  someone's change, so a late poll can't start a fan hold or an off-while-running alert.
  Fan boost steps one speed at most every 5 minutes. After a restart or reload MXZ holds
  each cloud head for one confirm wait, in which an own-remote room whose cloud head is
  under its own remote has no vote. A head unavailable for 15 minutes, or silent for 35 (Home
  Assistant's own MELCloud integration reports a quiet head every 15 minutes), is not
  controllable: MXZ stops writing it, its room's call stops counting toward the
  shared mode (unless the room's Safety priority sensor is on), and a repair issue says so
  until it reports again. Off for cloud heads in this beta: vanes and the vane kick, the
  airflow (stage) sensor, dual setpoint, and a vote only while running (own-remote rooms
  vote by mode); a freeze floor over a person's own run ends with the head off rather than
  handed back. The room's row on the plan sensor shows `head_type: cloud`,
  `cloud_status` (`pending`, `confirmed`, `unconfirmed` or `not_controllable`),
  `cloud_pending_until`, `cloud_confirmed_at` and the three settings; a local room's row is
  unchanged. Tested on invented heads that behave like comfort_HA (drops the mode inside
  `set_temperature`, 1 °F step, no `hvac_action`, late reports) and MELCloud (0.5 °C step,
  reports when polled, drops writes when rate limited, goes unavailable), not yet on a
  real cloud head; a new issue form, **Cloud head beta report**, asks for what we need
  to know. The idea and the setpoint drive offset, with its 2 °F and 90-second defaults,
  come from @mikepell007's MIT-licensed
  [mxz_coordinator_blueprint](https://github.com/mikepell007/mxz_coordinator_blueprint),
  reimplemented here with no blueprint code. Prompted by @mw1029's report on the
  [Home Assistant forum](https://community.home-assistant.io/t/1018382).
- **Tested** with the full test suite (2660 tests, full and fakeboot) on every supported
  Home Assistant lane, and on hardware: the Bedroom CN105 head on a house system on
  57cd00b stayed local, with the same single call, voting, hold and drop as before. **Not
  tested** on a real cloud head: that hardware check was waived because the test house
  has no cloud heads.

## 3.13.2 (October 29, 2026)

A wake-up fix for heads from cloud integrations, and test-only preparation for Home
Assistant 2026.12. No setting or default changes.

- **A head from a cloud integration wakes from `fan_only` or `off`.** MXZ woke a parked
  head with one `climate.set_temperature` call carrying both the mode and the setpoint.
  ESPHome, which runs the CN105 heads, applies the mode from that call. Kumo Cloud heads
  through comfort_HA drop it, so MXZ could park them but never wake them. For a head
  whose climate entity is not on the ESPHome platform, MXZ now sends
  `climate.set_hvac_mode` first, then `climate.set_temperature` with only the setpoint.
  If the mode call fails, MXZ sends no setpoint, logs the error and tries again at the
  next update. ESPHome heads, and heads with no entity registry entry, get the same single
  call as before. The suite's CN105 test heads now register on the ESPHome platform, so the
  existing tests keep checking those calls. This fixes the wake-up only; fuller cloud-head
  support is still in progress. Tested on invented heads, not yet on a cloud head.
  Reported by @mw1029 on the
  [Home Assistant forum](https://community.home-assistant.io/t/1018382).

- **The invented test heads use Home Assistant's new climate temperature names.** Home
  Assistant 2026.11 renamed a climate entity's temperature members `native_*` (such as
  `native_target_temperature_low`) and logs a deprecation for code that still writes or
  defines the old ones, which stop working in 2027.11. MXZ's own thermostats moved in
  3.12.0. On the 2026.12 development core, the test heads' old names logged thousands of
  such warnings, many naming MXZ because the heads write them inside commands MXZ sends.
  The heads now stand on a shared test base and use only the new names. On Home
  Assistant 2024.12 to 2026.10, which know only the old names, the base stores each new
  name under the old one.
- **Tested** with the full test suite on Home Assistant 2024.12.0, 2026.2.3, 2026.9.0,
  2026.10.4, a 2026.11 development build, and a 2026.12 development build. **Not tested**
  on hardware: the ESPHome (CN105) path is unchanged.

## 3.13.1 (October 28, 2026)

Fixes and cleanups after 3.13.0. No setting or default changes.

- **A head starting or stopping a run counts at once.** In a room run from its own remote
  whose vote counts only while its head runs, the vote now counts on the head's first
  heating or cooling report (unless that report comes just after a mode change; see the
  next item). It also ends on the first report that leaves the run (such
  as `fan_only`, another mode, or `off`), and its 10-minute hold starts on the first
  `idle` report. Before, such a report could wait out Home Assistant's 10-second refresh
  cooldown when something else had just asked for a recompute: the mode change that set
  the head going, or a room's sensor report. On hardware that was about 4 seconds for a
  start and about 5 seconds for `fan_only` or `idle`. MXZ still sends the head nothing.
- **A run reported just after the head's mode changes counts only if it lasts.** On
  hardware, MXZ ran a room's head in heat, parked it off when the room was disabled, and 3
  seconds later a remote set it to cool. The head reported `cooling` 0.3 seconds after
  that, left over from the heat run, and `idle` 5 seconds later. It really started
  cooling 100 seconds on. MXZ counted a cool vote and then held it for 10 minutes. Now a
  `heating` or `cooling` report within 15 seconds of the head's mode changing (any change,
  MXZ's own off included) counts only if the head is still running when those 15 seconds
  end, and MXZ recomputes at that moment without waiting for another report. Stopping
  inside them starts no hold. Every real start seen on that hardware came 10.6 to 16.7
  seconds after a mode change, so a real start counts at most about 4 seconds later.
  Unchanged: a start in a mode the head has been in for longer, stops, `fan_only`, `off`,
  the 10-minute hold, Safety priority votes, and the default vote by mode.
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
  2026.10.4, and a 2026.11 development build, and on a two-head system in cool: a start
  after a mode change counted about 15 seconds later, a start in the same mode at once,
  and three left-over run reports just after a release (one heat, two cool, each about
  5.4 seconds long) counted no vote and started no hold.

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
- **One target per room, stated plainly.** [CONTRIBUTING.md](CONTRIBUTING.md) said MXZ
  wouldn't add a dual setpoint or a heat/cool range, even as an option (3.10.0 replaced
  that with a per-room opt-in, and one target stays the default). It also asked that a
  new tuning option be blank by default and change nothing when left unset.
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
