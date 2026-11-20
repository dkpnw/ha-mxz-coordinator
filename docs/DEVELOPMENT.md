# Developer setup and tested support

For contributors. Users only need the [README](../README.md). Before you open a pull
request, read [CONTRIBUTING.md](../CONTRIBUTING.md); to cut a release, follow
[RELEASING.md](RELEASING.md).

## Tested support

The declared HA floor is 2024.12.0. The floor CI lane runs HA 2024.12.5, the newest
patch of that line; 2024.12.0 itself is no longer run. The full test suite runs on these
exact targets with invented entities; it covers software behavior, not physical behavior
on equipment.

| Python | Home Assistant | Lock |
| --- | --- | --- |
| 3.12.14 | 2024.12.5 | `requirements/constraints-py312-ha2024.12.5.txt` |
| 3.13 | 2026.2.3 | `requirements/constraints-py313-ha2026.2.3.txt` |
| 3.14 | 2026.9.4 | `requirements/constraints-py314-ha2026.9.4.txt` |
| 3.14 | 2026.10.4 | `requirements/constraints-py314-ha2026.10.4.txt` |
| 3.14 | 2026.11.2 | `requirements/constraints-py314-ha2026.11.2.txt` |

- Each lane is one lock and one row of the `pytest` job in `.github/workflows/ci.yml`; a
  contract test fails if a lock has no lane or a lane names the wrong lock. Each lane
  runs the full suite, then runs it again with `-p tools.fakeboot` and `FAKE_UPTIME=120`,
  on a clock that reads as if the runner had booted two minutes ago. The mode-change
  wait, the coil-dry wait, and the floor's own clocks are timed on the monotonic clock,
  which counts from boot, so a test that only passes on a long-running host fails there.
- Both passes run on every CPU with `-n auto --dist loadfile` (pytest-xdist, which each
  lane's test plugin installs). `loadfile` keeps a file's tests in one worker and in file
  order, as a serial run does.
- Coverage (96% branch floor, `pyproject.toml`) and strict mypy run on the 2026.9.4 lane
  in `.github/workflows/quality.yml`.
- The lanes up to 2026.10.4 use `pytest-homeassistant-custom-component`. It has no
  release for HA 2026.11, so the 2026.11.2 lane uses Home Assistant's own test plugin,
  `pytest-homeassistant`, which needs Python 3.14.2 or newer. Tests import the plugin's
  helpers only through `tests/ha_test.py`, which takes them from whichever of the two is
  installed. With neither installed the suite fails at import; no test file skips itself.
- The form screenshots were captured on HA 2026.9.4; the floor's rendering of the forms
  was not captured.

The property tests (`tests/test_properties_*.py`) need Hypothesis, which each lock pins. A
lane without it fails at collection. [docs/formal](formal/README.md) explains them and the
TLA+ model.

### Make or refresh a lane lock

A lock is `pip freeze` of a fresh venv on the lane's Python, unedited. The venv holds the
test plugin release that pins that exact Home Assistant (`pytest-homeassistant` from HA
2026.11, `pytest-homeassistant-custom-component` before it; never both), numpy at the
version Home Assistant's `package_constraints.txt` pins, and `requirements_test.txt`.
Hypothesis and sortedcontainers keep the versions every lock shares
(`tests/test_formal_contract.py` checks them):

```bash
python3.14 -m venv /tmp/lane
/tmp/lane/bin/python -m pip install --index-url https://pypi.org/simple \
  pytest-homeassistant==2026.11.2 numpy==2.3.2 hypothesis==6.170.15 -r requirements_test.txt
/tmp/lane/bin/python -m pip check
/tmp/lane/bin/python -m pip freeze --all --exclude pip > requirements/constraints-py314-ha2026.11.2.txt
```

Then add the lane's row to the `pytest` matrix and the table above.

## Run a lane locally

Use a new work directory outside the checkout and the lane's Python. This is the floor
lane; any lane works the same way with its own lock and Python.

```bash
work="$HOME/mxz-floor-work"
python3.12 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --index-url https://pypi.org/simple \
  -c requirements/constraints-py312-ha2024.12.5.txt \
  -r requirements/constraints-py312-ha2024.12.5.txt -r requirements_test.txt
"$work/venv/bin/python" -m pip check
"$work/venv/bin/python" tools/check_lock.py requirements/constraints-py312-ha2024.12.5.txt 3.12.14
"$work/venv/bin/python" -m pytest -p tools.pytest_phases tests/ -q -n auto --dist loadfile
FAKE_UPTIME=120 "$work/venv/bin/python" -m pytest -p tools.fakeboot -p tools.pytest_phases \
  tests/ -q -n auto --dist loadfile
```

Each pass must end `PHASES_VALID=true` with exit 0. `tools/pytest_phases.py` fails a run
that skipped, deselected or xfailed any collected test, or lost one of its phases. Lint
with the pinned tools: `pip install --require-hashes -r requirements_ci.txt`, then
`ruff check .`. Label a local result with the lane's versions, and do not infer hardware
results from mock entities.


## Adding a setting

Every Configure setting is one `Setting` row in `settings.py`, and every field on a room's
page is one `RoomField` row. A row gives the key, the page and section that show it, the kind
of field, its default, unit and bounds, and how it is checked and stored. The setup pages,
Configure and the room page are built from the rows, so a new setting is one row, its label
and help text in `strings.json` and `translations/en.json`, and the code that reads it.
`tests/test_settings.py` holds the rows and `strings.json` to each other.

`settings.HOME` says which store keeps each key: options for every setting, entry data for the
rooms, the notify service and the saved-temperature unit. Read an entry through
`settings.entry_config`, and write it with each key in its home (`settings.one_home`). A change
to where a key lives is a config-entry migration (`async_migrate_entry`).

## How the coordinator is split

`coordinator.py` still runs most of the control loop. Concerns are moving out of it one at
a time. Each moves into a module that owns its state, its wakeups and its stored record:

| Module | What it owns |
| --- | --- |
| `logic.py` | The decision math: room calls, arbitration, door/window states, floors, setpoints. No Home Assistant. |
| `cloud.py` | A cloud or generic head's write record and its setpoint math. No Home Assistant. |
| `units.py` | How a saved temperature or difference follows a °F/°C switch (`offset_in_unit`, `target_in_unit`), and the rounding and bounding they use (`snap_to_step`, `clamp`). No Home Assistant. |
| `grid.py` | What a head accepts (`Grid`): its band in the system unit (`safe_band`), a command clamped into it, the bounds a room target may take, whether a setpoint is already where a command puts it on the head's own grid (`already_at`), and the target that leaves a head where a person put it (`target_for_setpoint`); `local_calls`, the service calls a local head gets for one command; `convert`, Home Assistant's °C/°F conversion. `heads.HeadGrid` reads a head's entity into a `Grid`. No Home Assistant. |
| `head_writes.py` | MXZ's memory of its own mode and setpoint writes to a head, field by field (`HEAD_WRITE_FIELDS`): one call's order, its writes remembered before it is sent (`issue_steps`, `remember_writes`), a refused call taking back what it counted (`writes_refused`), a report passing the writes before it (`writes_caught_up`), a person's change the adopt hold took (`writes_pressed`), and whether MXZ's writes explain a shown value (`write_is_own`), a setpoint compared on the head's grid. No Home Assistant. |
| `plan.py` | The plan one compute decides (`Plan`, and a `RoomPlan` per room), which the apply, the mode's writeback, the dwell wakeup and the room thermostat read back as typed fields. `to_attributes()` renders it as the plan sensor, the room rows and diagnostics show it. A room row also carries what other controllers render for it (delivery, head, floor, Dry, door/window), kept as each one renders it. No Home Assistant. |
| `planner.py` | What one compute decides from what it read (`decide`): each room's demand and engage, the engage latch and the startup seeds it advances, and the shared mode they arbitrate to, with why. What one apply does, head by head and in order (`apply_gate`, `apply_plan`): each head's gates, then its steps as data (`HeadApply.steps`), each head sent `head_command` with the idle action and coil dry from `idle_for`; then what the plan's mode does to the shared mode's record (`mode_write_back`, stamped by `stamp_mode_change`; `mode_change_stamp` at a start). What a head's delivery does besides its mode and setpoints (`delivery_steps`): whether the fan goes back to auto before an off, whether the act is the head's last run for coil dry, and the fan step after the mode. What a head's report does, in order (`report_steps`): its startup seed, the band and off heal waits (`band_drift`, `off_while_enabled`, `heal_due`), and whether a change at a head is a person's and what it asks of its room (`head_change`, `adopt_head_change`, `head_report`, `hold_end`; Accept changes made at the head). What a person's write to a room's target, range or enable does to its adopt hold, engage latch, startup seed and release (`target_written`, `enable_written`, `range_target`). The coordinator reads Home Assistant and its controllers, calls these, and acts on the result: it builds the plan, queues each command and writes the record. No Home Assistant. |
| `openings.py` | The door/window pause: each room's record and its store, the delay wakeups, the notices, and the vanes a resume owes. |
| `dry.py` | Allow Dry: which rooms are held or parked in dry, a Dry picked on a thermostat and its wakeup, a room released from dry, and the Dry notices. In memory only. |
| `fan_hold.py` | Fan boost and the manual fan hold, per head: the ladder's rung, whether a person's pick is held, the echo memory of MXZ's own writes (`FanEcho`), and what the Fan auto switch restores. It reads the head, the settings and the clock, and `fan_latch.py` decides. The switch keeps the restore data; `FanHold` reads and writes it through `restore`, `command_memory` and `older_record`. |
| `fan_latch.py` | The manual fan hold's decision (`observe_fan`): whether a speed a head shows is a person's pick, MXZ's own write shown late (`FanEcho`), or a hold handed back; what a start's record and the first speed after it decide (`restored`, `reconcile_restored_hold`); a Fan auto gesture (`fan_auto`); and the boost step a delivery writes (`fan_step`, in order with its guards as `fan_boost`; `fan_read` for a head MXZ sends no fan). What a start reads back of a head's hold is a `FanRecord`, and what a stop saves of it `fan_record`. `FanHold` reads Home Assistant, calls these, and keeps the result. No Home Assistant. |
| `resume.py` | What a start takes back (`resume`): every room's target, Heat to and Cool to, drift and enable, the switches, each head's fan hold and MXZ's memory of its head writes, read out of the room state store in this unit and held to each head's bounds, with which values the store holds (`SavedState`, the section names). The mode record a start reads (`mode_record`, `ModeRecord`) and the shared mode it resumes in against the select's restore data (`resumed_mode`), and what a resumed enable does to the engage latch and the startup seeds (`resumed_enable`). The coordinator reads the stores and the heads, calls these, and applies the result; which source wins stays with it and the entities. No Home Assistant. |
| `timing.py` | MXZ's own timing: how each input asks for a compute (`Refresh`: through the request debouncer, as a head's report, or at once; `head_refresh`, `same_reading_refresh`), what each wakeup asks for when it runs (`WAKEUPS`), each wakeup's delay (`fresh_delay`, `dwell_delay`, `coast_delay`, `coil_dry_delay`, `nearest`, `cloud_delay`, `opening_wait`, `opening_delay`, `hold_end_delay`), the wakeups a compute leaves armed, in order and with their gates (`arms` of a `Wakeups`, as `Arm` and `CancelAll`; `cloud_arm`, `hold_end_arm`), whether a start is still quiet (`startup_quiet`), the request cooldown and the 15-minute heartbeat. The coordinator and its controllers read Home Assistant and the clock, call these, and arm what they say. No Home Assistant. |
| `sim_timing.py` | For a simulator only; no module of the integration imports it. When MXZ computes: `start`, `step` and `due` run `timing.py`'s rules on a model of Home Assistant 2025.11's request debouncer (the 10-second cooldown, one request kept behind it, a refresh at once that cancels it) and the delivery queue's follow-up, with each delivery done within its compute. The coordinator keeps Home Assistant's own debouncer. `tests/test_sim_timing_parity.py` checks the model against timing recordings from the real coordinator on every supported release and names each difference. No Home Assistant. |
| `delivery.py` | The delivery queue: one plan decided at a time, each head's commands in turn (`Delivery`), across reloads, the one follow-up refresh owed to a request made during a plan, and what each software call to a head returned. On Home Assistant before 2025.11 it makes up for two gaps in the request debouncer; that code goes when the minimum version reaches 2025.11. |
| `heads.py` | Each room's head driver behind one interface (`HeadDriver`): `LocalDriver` for a local head and `CloudDriver` for a cloud or generic one, with its `CloudHead` record, its batches, whether it is out of control, and its repair issue. `HeadGrid` reads what a head accepts: its native band and its setpoint grid. `HeadDrivers` settles each room's head type at the start, runs a delivery through the head's driver, and keeps the one wakeup the cloud heads share. |
| `floor.py` | The freeze floor and overheat ceiling of each disabled room that sets one (`FloorGuard`): the run and its no-progress clock, a person's setting a hands-off run took over and the hand-back that returns it, a yield to a person, the floor notices and their hourly limit, and the record the room's enable switch restores. |
| `clock.py` | MXZ's clock (`Clock`): the monotonic seconds every hold and limit reads, and every wait. `call_later` arms one wait per name (and per head or room), so arming it again replaces it; `retire` cancels them all when the coordinator is unloaded or replaced. It is the only module that calls Home Assistant's `async_call_later`. Nothing in it is stored: a wait that must outlast a reload is timed from a stored record, as the mode-change hold is. |
| `host.py` | `ControllerHost`, the coordinator members every controller uses: Home Assistant, the clock, `retired`, `notify`, `redraw_rooms`, the two recomputes, and `wake`, which a wakeup of each name ends in. `HeadCommands`: the delivery queue's `async_send`, for a controller below it. `Effect`: one thing a compute decided that reaches outside MXZ's memory. |

A module that moved out reaches the coordinator only through a `Protocol` it declares on top of
`ControllerHost`, which names each further coordinator member it uses (`OpeningHost`,
`DryHost`, `FanHost`, `DeliveryHost`, `HeadsHost`, `FloorHost`). mypy checks that the coordinator satisfies each one. The coordinator calls it at fixed
points and keeps no copy of its state. A controller that a lower one calls is reached through a
small Protocol the lower one can import: `HeadCommands` in `host.py`, `CloudRecords` in
`cloud.py` (a head's cloud record, for Dry and the fan hold), `HeadSteps` in `delivery.py`
(the head drivers, for the queue), and `FloorReports` in `delivery.py` (the floor, for the queue
and the head drivers, which tell it whether a command to a head was refused). Code mutants (`tools/code_mutants.py`) and the
statechart's Code column name functions, not files. A move therefore breaks an anchor only
where a function is renamed.

Still in `coordinator.py`: the compute's readings and the plan it builds, the apply, the mode-change hold and its record,
sensor freshness, head sight, the hands-off room and its votes, accept-at-head and the drift
heals, vane kicks, the season changeover and the standby hold. The hands-off release stays the
coordinator's; the floor changes it through `set_hands_off_released`.

The compute decides and performs nothing. `_compute` returns the plan and a list of effects:
the door/window notices, the Dry a resume puts back and the record's store write
(`OpeningPause.step`), the floor's trip, blind and stalled notices (`FloorGuard.want`), and a
cloud head's repair issue (`HeadDrivers.watch`). Each controller keeps its own decision, such as
a notice's rate limit, and hands back only the send. The coordinator makes the effects in the
order they were decided, right after the compute and before the plan's commands; nothing is
awaited in between. A floor notice decided while a delivery runs (a rejected command, a refused
hand-back) is sent at once, by the same code.

Which module may import which is checked in CI; a new module joins a layer in
`tools/import_bounds.py`. A new wait goes on the clock (`coord.clock.call_later`, or
`host.clock.call_later` in a controller); `tools/raw_timers.py` refuses a raw
`async_call_later` anywhere else. See [ARCHITECTURE-RULES.md](ARCHITECTURE-RULES.md).

## Restore, flow schemas, and abort text

Restore uses normal `RestoreEntity` state/extra data; flow schemas use Voluptuous. MXZ
raises its own `reconfigure_successful` and `already_configured` aborts without HA's
central translation domain (the floor has no such parameter), so both strings ship
locally in `strings.json`/`translations/en.json`. Tests resolve every flow reason
through HA's translation loader on each lane.

On HA 2026.10.0 (frontend 20260930.2) and on HA 2026.9.4 (frontend 20260826.7) the
real frontend rendered the local text, “Reconfiguration was successful.” with its
trailing period, not HA's central text, which has none (the
[screenshot](../images/mxz-reconfigure-complete.png) is from 2026.9.4). The
floor's rendering of local abort text was not captured, and `already_configured` was
not rendered.
