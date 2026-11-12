# MXZ Coordinator feature map for the throwaway-instance driver

Written read-only against `301ed4d` (manifest `3.22.0b1`) on 2026-11-10, then landed
with the driver it describes. Where building the driver showed a statement wrong, the
statement was corrected here. [README.md](README.md) is how to run it.

**Status when it landed.**

| Feature | Journey | Status |
|---|---|---|
| 5.1 Setup and turning it on | `setup-and-enable` | Proven, negative included: the flow, the `need_two_heads` refusal, and the Coordinator enable gate (a room past its drift, evaluated with it off, gets no call; turned on, its head is sent cool). |
| 5.2 Shared mode | none yet | Not yet proven. |
| 5.3 Room targets | `room-targets` | Steps 1 to 7 proven, each reading checked after MXZ evaluated it. The heat variant and the out-of-range negative are not yet driven. |
| 5.4 Accept changes made at the head | none yet | Not yet proven. |
| 5.5 Door/window pause | none yet | Not yet proven. |
| 5.6 Manual fan | none yet | Not yet proven. |
| 5.7 Restart continuity | none yet | Not yet proven. The device file that carries a head across a restart is tested in the suite. |
| 6 Demo, stock versus coordinated | `demo-stock`, `demo-coordinated` | Both arms run and filmed for the README (readme1110), each beat checked, the stock arm's Studio-never-leaves-standby negative included. |

This map says which product features a proof CLI should drive on a throwaway Home
Assistant, and for each one the setup, the action, what to capture and what a pass is. It
also recommends the shape of the CLI, because the standing rule (the project's standing rule) is to
build it cleanly or stop: the journeys below only stay honest if the parts under them are
separate.

## 1. What already exists, and what the throwaway instance adds

The repository already has a `verify-mxz` skill (`.claude/skills/verify-mxz/`). It drives
the real integration inside `pytest-homeassistant-custom-component`: one in-memory Home
Assistant per test, sockets blocked, time moved with the suite's clock helpers, a JSON
transcript per journey. It covers five features (mixed-mode rooms, unavailable or stale
heads, restart continuity, manual fan, Dry refused by a head).

That driver cannot show four things, and its own feature files say so:

- A restart there is "a store round trip and a new coordinator, not a new Python process".
- Setup is a `MockConfigEntry`, not the four-step flow a person completes.
- There is no recorder, no log file, and no frontend, so nothing can be filmed.
- Time is moved by the test, so a wakeup fires because the test fired it.

The throwaway instance is the second driver. It runs a real `hass` process on the real
clock, sets MXZ up through the real setup and Configure flows over Home Assistant's API,
stops, starts and kills the process, and keeps the event stream, the log and the recorder
history.

Each driver proves what it is good at. Do not port journeys from one to the other.

| Question | Driver |
|---|---|
| Does the logic hold at product-default durations (600 s wait, 30 min lost vote, 10 min coil dry), on a just-booted clock, across many edge cases? | In-process (existing) |
| Does a person's path work end to end on a real process: setup flow, Configure, entities, a real restart, a crash, storage on disk, history, logs? | Throwaway instance (new) |
| Can the README lane capture it? | Throwaway instance |

## 2. Recommended shape

**One skill, two drivers.** The instance driver belongs to the verify-mxz skill; do not
create a second skill. Its code lives in `verification/instance/`, as verify-mxz-formal's
lives in `verification/`, and the skill points at it. This file is the instance driver's
map; the in-process driver's map stays in the skill's `features/`. Each journey here names
the in-process feature it complements, and section 7 lists what stays in process, so the
two do not cover the same ground twice.

**Five parts, each with one job.** None may reach into another's internals.

| Part | Job | Knows nothing about |
|---|---|---|
| Instance | Build a config directory, start, stop, kill and reset one `hass` process; prove it is isolated; report readiness. | MXZ's entities, journeys |
| Invented equipment | A small development-only Home Assistant integration: heads, room temperature sensors, door/window sensors, and a device-side channel for what a person does at a remote. Its device state lives in a file that outlives the `hass` process. | MXZ, journeys |
| Client | Home Assistant's REST and WebSocket API: flows, service calls, registry lookups, the event subscription, history, diagnostics. | MXZ's rules |
| Journeys | One module per feature: actions by a named actor, each followed by expectations with a deadline. | Processes, sockets |
| Evidence | The transcript, the event stream, the log slice, stored files, the run record. | What a pass means |

**Things this shape rules out.** Each is a bolt-on that would make a journey pass for the
wrong reason:

- No test seam inside the running process. The instance must not patch
  `coordinator.monotonic`, write a coordinator field, or seed `.storage` to set a scene.
  Time-travel belongs to the in-process driver. Here a wait is waited.
- No config entry written to disk. The entry is created by the setup flow and changed by
  Configure, as a person does it.
- No fake `esphome`. A custom integration named `esphome` would shadow Home Assistant's own
  to reach the combined-call branch (`coordinator.py`, `entry.platform == "esphome"`). That
  branch stays with the in-process driver, and the feature files say so (section 7).
- No code under `custom_components/` other than the product. The invented equipment lives
  with the driver (`invented_house/`) and is copied into the instance's config directory at start. It must never
  be picked up by HACS, hassfest or the release zip.
- No pip at run time. Start `hass` with `--skip-pip`, so a missing requirement fails the
  start instead of downloading into a lane.
- No shared journey abstraction across the two drivers. Share the feature map and the
  transcript's step shape, nothing else.

**The proof CLI's own code is code.** Its pure parts (expectation checks, transcript
writer, config rendering, the equipment's outdoor-unit rule) get unit tests in the ordinary
suite, each with a negative case, and the whole tree is linted by the same `ruff` command
as `tools/`. `tests/test_outside_replays.py` is the precedent for guarding out-of-suite
code from the suite.

**If it cannot be built this way, stop.** In particular: if the instance cannot be
isolated on the build host, if the setup flow cannot be completed over the API, or if a
head on an invented platform is refused by the flow, stop and report it. Never fall back
to the house.

## 3. The throwaway instance

### 3.1 Hard constraints

- Invented or local mock heads only. No real head, adapter, cloud account or LAN device.
- No household credentials, and no credential value is ever read. The instance's own
  login is created at first start, lives only in the instance directory, and is never
  copied into evidence.
- None of the household's equipment or integrations is loaded.
- Never the live house or any LAN address. The instance has no route to either (3.2),
  and no command in the CLI takes a host argument: it only ever talks to the instance it
  started.
- Every transcript and capture is stamped as invented heads, not a hardware result.

### 3.2 Isolation

The in-process driver is safe because `pytest-socket` blocks sockets. A real `hass`
process has no such guard, so the instance needs one that holds by construction:

- **A network namespace with only loopback.** The CLI starts itself, `hass`, and any
  capture tool inside one namespace (`unshare --net`, then bring `lo` up). Nothing inside
  can reach the LAN or the internet. On this host that works: as root, a new namespace
  lists `lo` and nothing else (probe in section 8).
- **A minimal `configuration.yaml`, never `default_config`.** That keeps `zeroconf`,
  `ssdp`, `dhcp`, `usb`, `bluetooth`, `cloud`, `mobile_app` and `analytics` out. What the
  proof needs: `http` (bound to `127.0.0.1`, a port other than 8123), `api`,
  `websocket_api`, `auth`, `onboarding`, `config`, `recorder`, `history`, `diagnostics`,
  `system_log`, `logger`, `persistent_notification`. Bootstrap loads its default
  integrations regardless, `frontend` among them, and the HTTP server waits for
  `frontend`. Without the `home-assistant-frontend` wheel, which no test lane has, Home
  Assistant falls back to recovery mode and never serves the API. So the instance runs
  from its own environment: the lane's lock plus the frontend wheel its Home Assistant
  pins, built outside the namespace. Default integrations whose requirements the lock
  lacks (camera, conversation, tts and their dependents) fail to load and log errors;
  none of them is used.
- **Invented site data.** Name, coordinates, time zone and user are made up; none is the
  household's. Units are US customary (°F), to match the README's numbers.

If a namespace cannot be created (another host, an unprivileged runner), the instance
refuses to start. There is no `bind-only` mode.

### 3.3 Invented equipment

One invented house, kept as data, used by the proof and by the README lane: three rooms
in priority order, **Studio**, **Loft**, **Den** (the names in today's README dashboard
shot), each with a head, a room temperature sensor and a door/window sensor.

- **Heads** have the shape of the suite's `MockHead` (`tests/test_drive.py`): modes off,
  cool, heat, fan_only; a setpoint range (`target_temp_low`, `target_temp_high`); fan modes
  auto, quiet, low, medium, middle, high; °F. Both drivers then prove against the same
  head. A head that also lists `dry` or `heat_cool`, or has one setpoint, is a variant a
  journey asks for by name.
- **Platform.** The heads register under the equipment integration's own domain. MXZ
  treats any head that is not ESPHome and not opted in to cloud or generic as local, and
  sends it the mode on its own call, then the setpoints (`heads.py`,
  `LocalDriver._async_set_mode_and_setpoints`). So the instance exercises the local path's separate-call
  branch.
- **Device-side channel.** A person at a remote changes the head and the head reports it;
  no `climate.*` service is called. The suite's rule is the same: "the invented people
  change a head directly, as its remote does, never through a service MXZ could see"
  (`tests/test_accept_head_changes.py`). The equipment therefore offers its own actions:
  a remote press (mode, setpoint, fan), a room temperature report, a door/window reading,
  and a head going unavailable and coming back.
- **The device outlives Home Assistant.** A real head keeps its mode, setpoints and fan
  while Home Assistant is down, and can be changed during the outage. The equipment keeps
  each device's state in a file in the instance directory, writes it at every change, and
  reads it at start. A press during an outage is an edit of that file while the process
  is stopped.
- **An outdoor unit and a room model, for the demo only (section 6).** Off by default, so
  proof journeys set exact temperatures and stay deterministic.

Home Assistant's own `template` integration has a `climate.py` in the 2026.10.4 lane and
none in the 2024.12.5 or 2026.9.4 lanes (section 8), so it cannot supply the heads on every
lane. The equipment integration can.

### 3.4 Launch, doctor, cleanup

**Launch.** `up` builds a fresh instance directory, copies in this checkout's
`custom_components/mxz_coordinator` and the equipment, records the revision (with `-dirty`
when the tree has changes) and a digest of the copied code, starts `hass --skip-pip`,
completes onboarding with an invented user, and waits for readiness. Ready means the API answers, the equipment's
entities exist with states, and the recorder is running, not just that a port is open.
`up` does not set MXZ up: that is the first journey.

**Doctor.** Read-only, run first and after any failed drive. It prints the Python and
Home Assistant versions, the checkout revision and the revision the instance was built
from, and the isolation mode, then checks:

- from inside the instance's namespace, a TCP connection to `192.0.2.1` (TEST-NET-1) fails,
  and the namespace has no interface but `lo`;
- the process it is about to drive is the one it started (pid and port from the instance
  directory, never a process found by name);
- the instance was built from this checkout's revision and code; a stale instance is a
  doctor failure, not a warning;
- the log has no error from `custom_components.mxz_coordinator` or the equipment.

**Cleanup.** `down` stops the process it started. `reset` removes the instance directory.
Neither touches evidence. One driver per instance at a time, held by a lock; a second
driver is refused. Two named instances can run side by side, each in its own namespace.

### 3.5 Reuse by the README storefront lane

The instance is a recipe, not a pet: the README lane runs the same commands from its own
checkout and gets the same house with the integration at its own revision.

- **Where it lives.** The recipe is `verification/instance/`; instance directories and
  environments live under one root outside any checkout (`$MXZ_INSTANCE_HOME`); evidence
  under `$MXZ_VERIFY_OUT`.
- **UI.** There is no separate UI profile: the instance's environment has the frontend
  wheel (3.2), so every instance serves the frontend and `lovelace` at its loopback
  address, inside its namespace.
- **Capture inside the namespace.** A browser or recorder is started through the instance
  (`exec`), so it shares the loopback-only namespace. The browser is the capturing lane's
  to install, outside the namespace.
- **Boundary.** The dashboard, the filming and the screenshots are the README lane's.

## 4. Conventions for every journey

**Actors.** Each step names who acted:

| Actor | How it acts |
|---|---|
| Person in Home Assistant | A service call on MXZ's entities: the room thermostat, target number, enable and Fan auto switches, the shared-mode select, the Window Override switch. A flow: setup or Configure. |
| Person at the head | The equipment's device-side remote press. |
| Room | The equipment's temperature report or door/window reading. |
| Home Assistant | A stop and start of the process, or a kill. |
| Time | Waiting, on the real clock. |

**Handles.** Find MXZ's entities by unique-id key through the entity registry
(`<entry_id>_primary_thermostat`, `_secondary_target`, `_zone_3_enable`, `_plan`,
`_shared_mode`), as `journeys/mxz_journey.py` `entity()` does. Entity ids depend on names.

**Options a journey needs** are declared by the journey, applied through Configure before
its first step, and written into the transcript. The base instance is all defaults. Only
durations are shortened, and only where a journey says so; every other default stays, so
the proof is of what people run.

**Expectations have deadlines.** An expectation is checked against the event stream until
a deadline taken from the product's documented timing plus a margin. A miss fails the step
with the last state seen. "Nothing happened" is checked over the whole recorded window,
not by one sample.

**Three checks run across every journey**, from the event stream and the log:

1. MXZ never commands opposite directions: at no instant is its latest command to one head
   `heat` while its latest to another is `cool`.
2. MXZ never sends `auto` or `heat_cool` to a head, and sends `dry` only to a room with
   Allow Dry mode on.
3. The log has no error or traceback from the integration, unless the journey expects one.
   An ERROR or CRITICAL block counts when its header or traceback names the integration
   or the equipment, whatever the logger: Home Assistant logs an exception from an
   integration's listener under `homeassistant.core` ("Error running job") and one from
   its task or timer under `homeassistant` ("Error doing job").

Checks 1 and 2 need MXZ's calls told apart from the person's. `coordinator.py` passes no
`context=` to its head calls, so they carry no user, while API calls carry the instance
user. `room-targets` checks this as a negative control on every run: MXZ's head calls
must exist and carry no user, and the person's calls to MXZ's entities must carry the
instance user.

**Capture, per journey**, under `$MXZ_VERIFY_OUT/instance/<feature>/<journey>/`:

| File | Content |
|---|---|
| `transcript.json` | One entry per step: actor, action, time, what was shown (plan state and rows, room thermostats, Fan auto, heads, in the in-process transcript's shape), calls since the last step, each expectation with its result. The options applied. The outcome. |
| `events.jsonl` | Every `state_changed` and `call_service` event for MXZ's and the equipment's entities, as fired. This is the state history of record: the recorder drops `mode_change_hold_remaining` and `mode_change_hold_until` (`climate.py`, `_unrecorded_attributes`). |
| `home-assistant.log` | The log from the journey's first step to its last. |
| `history.json` | Recorder history for the same entities and window. It shows the recorder path works and gives the README lane replayable data. |
| `storage/` | For restart journeys: `mxz_coordinator.<entry_id>.mode_change`, `mxz_coordinator.<entry_id>.openings`, and MXZ's rows of `core.restore_state`, before the stop and after the start. Never `auth` or any other file. |
| `diagnostics.json` | The integration's own redacted diagnostics download, at the end. |

`run.json` records the revision, versions, isolation mode, instance name, features and
exit status. A transcript is written pass or fail. Evidence survives `down` and `reset`.

**A journey that fails is a finding.** Report the step and the state seen. Never loosen a
journey to make it pass; put the reason in the feature's Gotchas.

## 5. Features and journeys

Rooms are Studio (priority 1), Loft, Den. Unless a journey says otherwise, it starts with
every room enabled at target 70 reading 70, Coordinator enable on, the plan resting in
`cool`, no mode-change wait running, every window closed, every Fan auto on, and the
startup quiet over (`STARTUP_RECOVER_DELAY`, 40 s after a start). Each journey first puts
the instance in that state through ordinary actions and waits, and records having done so.

"Parked" means the head is in `fan_only` or `off` and its plan row's `engage` is not its
own direction. The default idle action runs `fan_only` after cooling, then `off`, so a
journey must not pin which of the two a parked head shows.

### 5.1 Setup and turning it on

A person installs MXZ, completes setup, and nothing is commanded until they turn it on.
Every other journey depends on this one having run.

- **Setup.** A fresh instance: equipment loaded, no MXZ entry.
- **Action.** Person in Home Assistant runs the setup flow over the API: heads in priority
  order, room names, one temperature sensor per room, the summary, **Save and finish**.
  Then sets each room's target, enables the rooms, and turns on **Coordinator enable**.
- **Capture.** Each flow step's form and result; the entity registry after the entry
  exists; the event stream from entry creation onward.
- **Pass.**
  - The flow ends in a created entry with three rooms in the order given.
  - Each room has a thermostat, target, drift, enable and Fan auto; the entry has the plan
    sensor, the shared-mode select and Coordinator enable.
  - Before anything is enabled the plan reads `cool`, the fresh-install mode
    (`docs/formal/STATECHART.md`).
  - Between entry creation and Coordinator enable turning on, MXZ sends no head any call
    (`README.md`: "A new entry starts with Coordinator enable and every room off, so it
    commands nothing"), though the rooms are enabled and Studio reads 72 against target
    70, past its drift, and the plan shows that reading evaluated (Studio's row: `temp`
    72, `engage` `cool`).
  - Positive control: once it is on, Studio's head is sent `cool` with 68 and 70.
  - Once Studio reads 70 again, is parked and MXZ is quiet, 60 s with every room at
    target send no head `heat` or `cool`.
- **Negative.** The flow run with one head selected, not generic, is refused with
  `need_two_heads` and creates no entry.
- **Gotchas.**
  - If the flow refuses the invented heads, that is a blocker to report, not a reason to
    write the entry to disk.
  - A window that claims MXZ held back proves nothing unless MXZ evaluated inside it. MXZ
    evaluates a room report through Home Assistant's 10 s request-refresh cooldown, so
    the journey waits until the plan row shows the reading, not for a fixed time. A
    product that ignored Coordinator enable fails this journey at the gate (checked once
    on the real instance by removing both of MXZ's Coordinator enable checks, in `_apply`
    and `_delivery_owned`, now `DeliveryQueue.owned`; removing only the first changes nothing).
  - A row's `demand` is the outdoor-unit call band (`cool_demand_above`, 73 for target 70 at
    the defaults); `engage` is the run starting past the drift (`cool_start`). A room at
    target + 2 engages `cool` with `demand` `neutral`.

### 5.2 Shared mode

One outdoor unit runs one direction. The first room in priority order that calls decides;
a reversal waits out **Minimum time between mode switches**; a person's pick is never
held. This is the instance's version of the in-process `mixed-mode-rooms`, on real timers.

- **Setup.** Option: Minimum time between mode switches 120 s (default 600).
- **Steps.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Room: Loft reads 64. | The plan changes to `heat` at once (nothing on record gates the first change, or the earlier wait has ended). Loft's head is `heat`. Every room thermostat shows `mode_change_hold_remaining` near 120 and one `mode_change_hold_until`, call it U1. Studio and Den are parked. |
| 2 | Room: Studio reads 76, inside the wait. | The plan stays `heat` with `mode_change_allowed: false` and `standoff: true`. Studio's row demands `cool`; its head is not `cool`. Loft keeps heating. |
| 3 | Time: nothing else happens until U1. | At U1, within a few seconds and with no action in the window, the plan becomes `cool`. Studio's head is `cool`. Loft's head is parked and its row's `engage` is still `heat`. A new wait shows, ending at U2. Over the whole window before U1, the plan never left `heat`. |
| 4 | Room: Studio reads 70. | Studio is `satisfied` and parked. The plan stays `cool` until U2, then becomes `heat` by itself. Loft's head is `heat` again. |
| 5 | Room: Loft reads 70. | Loft is `satisfied` and parked. The plan rests at `heat` (Resting mode, Last used). |
| 6 | Person in Home Assistant: picks `cool` on the shared-mode select, inside the wait. | The plan is `cool` at once. A new wait starts. |
| 7 | Person in Home Assistant: picks `cool` again. | `mode_change_hold_until` does not move. |

- **Pass, over the whole journey.** Loft's head was never sent `cool` and Studio's never
  `heat` before step 6. Check 1 of section 4 holds.
- **Capture.** As section 4. The transcript states U1 and U2 and the measured time of each
  flip.
- **Complements** the in-process `mixed-mode-rooms`, which keeps the 600 s default.
- **Gotchas.** The 120 s wait is a shortened setting, stated in the transcript; the 600 s
  default is the in-process journey's. A flip can only be seen "by itself" if the driver
  sends nothing in the window: the driver must not poll with a service call.

### 5.3 Room targets

A person sets one temperature per room. A room starts a run past its drift, runs to the
target and not just back inside the drift, then coasts.

- **Setup.** Defaults. Studio reads 72 with target 72.
- **Steps.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Person in Home Assistant: sets Studio's thermostat to 70 (`climate.set_temperature` on the room thermostat). | Studio's target number reads 70. Its row's `engage` is `cool`. Its head is `cool` with `target_temp_low` 68 and `target_temp_high` 70: a range head gets target − 2 to target when cooling (`docs/HOW-IT-WORKS.md`, Setpoints sent to the heads). |
| 2 | Room: Studio reads 70.5. | Once the plan's Studio row shows `temp` 70.5, the row still engages `cool`, the head is `cool`, and MXZ has sent it no park call. |
| 3 | Room: Studio reads 70. | `satisfied`; the head is parked. |
| 4 | Room: Studio reads 70.5. | Inside the 1 °F drift: no run, and MXZ sends that head nothing over 20 s, in which the plan's Studio row shows `temp` 70.5 and stays `satisfied`. |
| 5 | Person in Home Assistant: sets Studio's target number to 69. | The thermostat shows 69. A run starts: the head is `cool` with 67 and 69. |
| 6 | Person in Home Assistant: turns Studio's thermostat off. | Studio's enable switch is off. Its head is `off`. Its row's start points are `null`. |
| 7 | Person in Home Assistant: sets the thermostat back to `heat_cool`. | The enable switch is on and the run resumes. |

- **Variant.** The same in heat, with the plan resting in `heat`: the head gets target to
  target + 2.
- **Negative.** A target outside the target number's range does not become the room's
  target, and the head never gets a setpoint outside Lowest and Highest setpoint (59 to
  88 °F by default). Whether Home Assistant refuses the call or MXZ holds it to the limit
  was not read; record which.
- **Gotchas.**
  - The band is for a range head. A single-setpoint variant gets the target itself, and
    is a separate run.
  - Parking is two calls (`fan_only`, then the fan back to `auto`), a moment apart. A
    window that must see no call from MXZ starts only after MXZ has been quiet for 5
    seconds (`Drive.quiet`); starting it at the first sign of parking catches the
    second call.
  - The transcript lists the calls since the step before, so a call MXZ makes in
    answer to one step can appear under the next.
  - A reading MXZ never evaluated proves nothing: it evaluates a room report through
    Home Assistant's 10 s request-refresh cooldown. A step about a reading waits until
    the Studio row's `temp` shows it, with a deadline of the cooldown plus 10 s.

### 5.4 Accept changes made at the head

A setpoint or off set at a head is adopted by its room once it has held about 5 seconds
(`ADOPT_DELAY`). On by default. With the option off, MXZ is the only writer.

- **Setup.** Defaults (the option on). Studio reads 74 with target 70 and is cooling.
- **Steps.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Person at the head: sets Studio's upper setpoint to 72. | For the 5 seconds, MXZ writes nothing to that head. Then Studio's target reads 72, the log has `MXZ: adopting a change made at <head>`, and the head gets 70 and 72 in `cool`. |
| 2 | Person at the head: sets 75, then 72 again two seconds later. | The target stays 72; it never reads 75. |
| 3 | Person at the head: turns it off. | After about 5 seconds Studio's enable switch is off and its thermostat reads `off`. The head stays off for 45 seconds, past the 30-second heal, with no `MXZ heal:` line. |
| 4 | Person at the head: turns it on to cool with the upper setpoint at 71. | Studio is enabled, with target 71. |
| 5 | Person at the head: switches it to `heat` while the plan is `cool`. | Never adopted: the target and the enable do not change, the plan stays `cool`, and the head goes back to `cool`. Record how long that took. |
| 6 | Person in Home Assistant: turns **Accept changes made at the head** off in Configure → Comfort. Person at the head: sets the upper setpoint to 75. | The target does not change. The head is put back to the plan's setpoints. |
| 7 | Person at the head: turns it off, the option still off. | The head is put back on after about 30 seconds, with an `MXZ heal:` line. Then the option is turned back on. |

- **Capture.** As section 4, plus the time from each press to its adoption.
- **Gotchas.**
  - Nothing is adopted during the startup quiet, Eco idle, a standby hold, or with
    Coordinator enable off. A journey that presses too soon after a start fails for that
    reason.
  - Step 2 depends on two presses landing inside one 5-second hold on the real clock. If
    the driver cannot do that reliably, the step fails visibly; do not widen it.
  - Known limits are not driven here: a late report after a restart (SM-1), a press while
    MXZ's command is queued (SM-6), a press of a remembered value (SM-10); see
    `docs/STATE-MACHINES.md`.
  - This is not the real IR remote check. That stays on the house check list for 3.22.0.

### 5.5 Door/window pause

A room with the pause on stops while its door or window is open, and resumes when it
closes. A change on the head or the Window Override switch runs it anyway.

- **Setup.** Options, on Loft's room page: door/window sensor, Pause this room while it is
  open, open delay 10 s (default 60), close delay 15 s (default 120). Loft reads 74 with
  target 70 and is cooling.
- **Steps.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Room: Loft's window opens for 4 seconds, then closes. | Never paused: `opening_state` goes `opening` and back to `closed`, and the head is not sent `off`. |
| 2 | Room: the window opens and stays open. | After 10 seconds `opening_state` is `paused`. The head goes to `off` with no `fan_only` first. The thermostat's `hvac_action` is `off`, its state is still `heat_cool`, and the enable switch is still on. `paused_reason` is `door/window open`. The plan lists Loft in `paused_rooms`. The paused binary sensor is on. The log has `MXZ: Loft door/window opening -> paused`. |
| 3 | Time: 30 seconds pass, the window open. | MXZ sends that head nothing: no mode, setpoint or fan. |
| 4 | Room: the window closes, then opens again 5 seconds later. | `closing` with a `resumes_at`, then `paused` again. The head stays off. |
| 5 | Room: the window closes and stays closed. | After 15 seconds `opening_state` is `closed`, the head is `cool` again, and `paused_rooms` is empty. |
| 6 | Room: the window opens until paused. Person in Home Assistant: turns the Window Override switch on. | The room resumes at once: `overridden`, `paused_reason` `window open, manual override`, the head `cool`. |
| 7 | Room: the window closes. | The override switch turns itself off; `opening_state` is `closed`. |
| 8 | Room: the window opens until paused. Person at the head: turns it on to cool. | `overridden`, the override switch reads on, and MXZ does not turn the head off again. |

- **Pass, over the whole journey.** `mode_change_hold_until` never moves because of a pause
  or a resume.
- **Negative.** With **Pause this room while it is open** off and the sensor still set, an
  open window pauses nothing.
- **Gotchas.** A sensor that reads `unavailable` never starts a pause and never ends one.
  A fan or vane change alone on the head is not a takeover. Cloud and generic heads are
  not covered.

### 5.6 Manual fan

Fan boost drives the fan by how far the room is from target. A speed a person picks is
held until they hand it back.

- **Setup.** Defaults (fan boost on, top speed `high`). Studio has target 70.
- **Steps.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Room: Studio reads 75. | The head is `cool`, the fan `high`, Fan auto on. |
| 2 | Room: Studio reads 73.2, then 72.2. | The fan goes `middle`, then `medium`, each by one fan call from MXZ (`logic.fan_for_delta`: up at 1, 2, 3, 4 °F, down at 0.5, 1.5, 2.5, 3.5). |
| 3 | Person at the head: picks `quiet`. | The plan row shows `fan_hold: true` and Fan auto is off. |
| 4 | Room: Studio reads 75 again. | The fan stays `quiet`. From the pick on, MXZ sends that head no fan call. |
| 5 | Room: Studio reads 70. | The head parks. No fan call. |
| 6 | Room: Studio reads 75. Person in Home Assistant: turns Fan auto on. | `fan_hold: false`, and MXZ writes the fan again: `high`. |
| 7 | Person in Home Assistant: turns Fan auto off. | A hold at the speed showing. |
| 8 | Person at the head: sets the fan to `auto`. | The hold ends and Fan auto reads on. |

- **Gotchas.**
  - The pick must be a speed outside MXZ's last two writes. After step 2 those are
    `middle` and `medium`; a pick of either reads as MXZ's own echo and is not held. That
    is the documented exception (F5 in the statechart), not a failure.
  - Without step 6 a silent fan could be a broken boost. The hand-back is what shows the
    silence was the hold.
  - The hold across a restart is in 5.7.

### 5.7 Restart continuity

A stop and start of the real process, and a kill. This is what only the instance can
prove.

- **Setup.** Options: Minimum time between mode switches 120 s; Loft's door/window pause
  as in 5.5.
- **Restart mid-wait.**

| # | Actor and action | Pass |
|---|---|---|
| 1 | Room: Loft reads 64, then 70. | The plan changed to `heat` and rests there; the wait ends at U. |
| 2 | Room: Studio reads 76. Home Assistant: stops, then starts. | After the start the plan is `heat` with `mode_change_allowed: false`. `mode_change_hold_until` is within 2 seconds of U, never the start time plus 120. Studio's head is not `cool`. |
| 3 | Time: until U. | The plan becomes `cool` by itself and Studio's head is `cool`. |

- **Restart with rooms in progress.** One stop and start, with this state built first:
  Studio cooling and reading 70.5, inside its drift, with its fan held at `quiet`; Loft
  paused by its open window, its head off; Den at target 68, enabled.

| After the start | Pass |
|---|---|
| Studio's run | `engage` is `cool`, the head is still `cool`, and across the restart the head's mode never changed. At 70 it is `satisfied`. |
| Studio's fan | `quiet`, `fan_hold: true`, Fan auto off, and no fan call from MXZ since the start. |
| Loft's pause | `opening_state` is `paused`, the head is off, and MXZ has sent it nothing since the start. |
| Den's settings | Target 68, enabled. |

- **A press during the outage.** Home Assistant stops; the person at the head moves
  Studio's upper setpoint to 73; Home Assistant starts. Pass: the room's target does not
  change, and the head gets the plan's setpoints back ("neither is one made while Home
  Assistant was down or still starting", `docs/FEATURES.md`).
- **A kill.** A mode change and a pause are made, 5 seconds pass, and the process is
  killed with no shutdown. Pass after the start: the plan is in the changed mode with the
  same `mode_change_hold_until`, and Loft is still paused. Both are stored at once
  (`mxz_coordinator.<entry_id>.mode_change` and `.openings`).
- **Recorded, not judged.** After the kill, the transcript lists whether each target,
  enable and fan hold changed in the last minutes came back. Those ride restore data,
  written every 15 minutes and at a stop, and the product documents that a crash can lose
  them (SM-9, audit H2). The product makes no promise here, so there is nothing to pass or
  fail; the record is the before-picture for the room state store.
- **Capture.** As section 4, plus `storage/` before each stop and after each start, the
  process's exit status, and the boot time.
- **Gotchas.**
  - Every start costs the 40-second startup quiet before a head change counts.
  - A restart only lengthens a door/window delay that was running, never shortens it.
  - The equipment's device file is what makes a head "still running" across the outage. If
    the heads came back at defaults, the journey would pass or fail for the wrong reason;
    the doctor should check that a head's state survives a stop and start of the
    equipment alone.

## 6. The demo path: stock versus coordinated

One afternoon, shown twice for the README: the sun has warmed Studio to 76 °F while Loft
and Den sit at their 70 °F targets after a morning of heating. Stock, Studio waits behind
the others' heat. Coordinated, MXZ turns the system to cool for Studio alone.

**What the equipment models, and no more** (`invented_house/model.py`, off unless a demo
journey turns it on). Each rule is invented and the capture says so:

1. One outdoor unit runs one direction. A head set to the other direction waits in
   standby until no head is set to the running direction. This is the manual's rule as
   `docs/HOW-IT-WORKS.md` cites it: "the unit selected last goes into standby mode."
2. A head in the running direction moves its room to its setpoint (heat: the low one,
   cool: the high one) at 1 °F a minute, then idles in its mode.
3. A room no head is moving drifts toward 76 °F at 0.05 °F a minute. No randomness.

Both arms start from the same temperatures (Studio 76, Loft 70, Den 70), use the same
model, and are filmed for the same 450 seconds from their start step. Each runs on its
own fresh instance.

**Arm A, stock** (`demo-stock`). No MXZ entry.

- **Before the start.** Person at the heads: Loft and Den to heat at 70. The outdoor unit
  runs heat; both heads idle.
- **Action.** Studio reads 76, then the person sets Studio's head to cool at 70.
- **Pass (a control on the model, not a product claim).** Studio's head shows cool and
  waits in standby for the whole window, and never leaves it; Studio still reads 76 at the
  end; the outdoor unit still runs heat; no climate call reached a head.

**Arm B, coordinated** (`demo-coordinated`). After 5.1, every option at its default.

- **Before the start.** Loft and Den read 66, MXZ heats them to 70 and turns their heads
  off, and the 600-second wait since the turn to heat runs out on the real clock.
- **Action.** Studio reads 76, Loft and Den 70. Nothing else.
- **Pass.** The shared mode changes from heat to cool and Studio's head cools with the fan
  on high; from high the fan steps down middle, medium, low, quiet as Studio closes in;
  Studio reaches 70 and its head goes to fan only to dry the coil; Loft's and Den's heads
  are never sent heat or cool and stay off; check 1 of section 4 holds.
- **Run on readme1110 (HA 2026.9.4).** Mode to cool 10 s after the start; fan to middle at
  170 s, medium 226 s, low 286 s, quiet 341 s; Studio at 70 and fan only at 371 s.

**The turn-taking story this section used to describe is not the demo.** With Studio at
76 and Loft at 64 it expected Studio to reach 70 before the plan turned to heat. By
`logic.room_call` and `logic.shared_mode`, a room calls only while more than the Mode
switch threshold off target, so the mode leaves Studio once it is back within 3 °F while
Loft still calls (`docs/FEATURES.md` and `docs/HOW-IT-WORKS.md` step 4 say the same). That
version was never run. Run it before trusting any number for it.

**Capture.** `film.py` films each row from the instance's own frontend; `compose.py`
stacks them on the journeys' start steps, sped up evenly. See [README.md](README.md#the-readme-demo).

**Honesty limits.**

- It is a simulation with invented heads, time-compressed. It is not a measurement of
  Mitsubishi equipment, energy, comfort or noise, and the capture says so.
- Arm A shows only the cited rule. It does not model stock AUTO, installer switches,
  kumo cloud changeover groups, or how long a real system leaves a head in standby. The
  capture must not claim "an hour"; that figure is a household observation in
  `docs/HOW-IT-WORKS.md`.
- The live house is not used. Filming real heads needs the owner's approval for a
  specific short window, and is not part of this driver.

## 7. What stays with the in-process driver

| Feature | Why |
|---|---|
| The ESPHome combined call | Needs a head on the `esphome` platform (section 2). |
| The 600-second wait, the 30-minute lost vote, the 10-minute coil dry at their defaults | Too long to wait out; the in-process driver moves the clock. |
| Unavailable or stale heads; Dry refused by a head | Already covered; nothing a real process adds yet. |
| The fake-boot clock | A property of the test clock. |
| Cloud and generic heads, freeze floor and overheat ceiling, seasons, eco, standby hold, comfort and safety priority, dual setpoint, vanes | Not mapped in this pass. Each is a later feature file, not a reason to widen a journey above. |

## 8. Evidence boundary

**What this map was written from.** The existing verify-mxz skill, `docs/FEATURES.md`,
`docs/HOW-IT-WORKS.md`, `docs/ENTITY-MAP.md`, `docs/DEVELOPMENT.md`,
`docs/formal/STATECHART.md`, `const.py`, `capabilities.py`, the head-call code in
`coordinator.py` and `config_flow.py`, `logic.fan_for_delta`, and the suite's head,
restart and clock helpers. The rest of `coordinator.py`, `openings.py` and the config
flow were not read line by line; pass criteria taken from the docs are cited to the docs.

**What landing the driver settled, by running it** on HA 2026.9.4 (Python 3.14.7):

- A `hass` process starts cleanly in a loopback-only namespace from the instance's own
  environment, and does not start from a test lane (3.2).
- Onboarding and the whole setup flow complete over the API without a browser, and the
  flow accepts the invented heads, room sensors and room names (5.1).
- MXZ's head calls carry no user in their context; the person's calls carry the
  instance user (section 4).
- The number cells of 5.1 and 5.3 steps 1 to 7 held as written, re-run after review
  found that 5.1's gate and 5.3 step 2 had passed without MXZ evaluating the reading.

**Still unknown.**

- How long a real start takes on a loaded host, and so whether 120 seconds leaves
  enough of the wait to observe in 5.7. If not, lengthen the setting and say so; do not
  shorten the check.
- Every exact number in a Pass cell of a journey not yet driven. A mismatch on the first
  run is either a wrong map or a product finding; decide which from the source before
  changing either.
- Nothing here is a hardware result.

## 9. What is left, in order

1. Add 5.2 and 5.4 to 5.7 in the order written. Land each that runs clean; list any that
   does not as not yet proven, with the reason, in its Gotchas.
2. Done in readme1110: the demo model and both arms of section 6.
3. Point the verify-mxz skill's `SKILL.md` and feature index at this driver.
