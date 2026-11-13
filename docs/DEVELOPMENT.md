# Developer setup and tested support

For contributors. Users only need the [README](../README.md). Before you open a pull
request, read [CONTRIBUTING.md](../CONTRIBUTING.md); to cut a release, follow
[RELEASING.md](RELEASING.md).

## Tested support

The declared HA floor is 2024.12.0. The floor CI lane runs HA 2024.12.5, the newest
patch of that line; 2024.12.0 itself is no longer run. The full test suite runs on these
exact targets with invented entities; it covers software behavior, not physical behavior
on equipment.

| Python | Home Assistant | Dependency setup |
| --- | --- | --- |
| 3.12.14 | 2024.12.5 | `requirements/constraints-py312-ha2024.12.5.txt` |
| 3.13.16 | 2026.2.3 | `requirements/constraints-py313-ha2026.2.3.txt` |
| 3.14.8 | 2026.9.4 | `requirements/constraints-py314-ha2026.9.4.txt` |
| 3.14.8 | 2026.10.0 | [Exact generated stable test environment](../requirements/ha2026.10/README.md) |

- The first three locks belong to the existing CI lanes. Each lane, and the .10
  qualification, also runs the full suite a second time (the 2024.12.5 and 2026.9.4
  lanes in a separate parallel job) with `-p tools.fakeboot` and
  `FAKE_UPTIME=120`, on a clock that reads as if the runner had booted two minutes ago.
  The mode-change wait, the coil-dry wait, and the floor's own clocks are timed on the
  monotonic clock, which counts from boot, so a test that only passes on a long-running
  host fails there.
- The .10 recipe uses public immutable core/plugin inputs, a pinned Python archive, and
  actual locally built artifact hashes. Public plugin `0.13.370` requires beta4;
  installing it by version alone does not reproduce the stable .10 environment.
- Use the linked recipe on Linux x86-64 with glibc 2.41 or newer. Its local evidence
  does not establish genuine hosted qualification.
- The 3.12 form screenshots were captured on HA 2026.10.0 and the retaken ones on
  2026.9.4; the floor's rendering of the forms was not captured.
- This source does not implement the separate HA 2026.11 restore changes, and no .11
  support claim follows from .10 results.

The property tests (`tests/test_properties_*.py`) need Hypothesis, which each lock pins. A
lane without it fails at collection. [docs/formal](formal/README.md) explains them and the
TLA+ model.

## Run the floor lane (HA 2024.12.5) locally

For a small local floor environment, use a new work directory outside the checkout
and an installed Python 3.12.14. This command is for your development machine; it does
not impersonate GitHub runner context or run the CI-only installer.

```bash
work="$HOME/mxz-floor-work"
mkdir -p "$work/home" "$work/tmp" "$work/cache"
export HOME="$work/home" TMPDIR="$work/tmp" XDG_CACHE_HOME="$work/cache"
export PYTHONDONTWRITEBYTECODE=1 PIP_CONFIG_FILE=/dev/null
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
unset PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST PYTHONPATH
python3.12 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --index-url https://pypi.org/simple \
  -c requirements/constraints-py312-ha2024.12.5.txt \
  -r requirements/constraints-py312-ha2024.12.5.txt -r requirements_test.txt
"$work/venv/bin/python" -m pip check
"$work/venv/bin/python" tools/check_lock.py \
  requirements/constraints-py312-ha2024.12.5.txt \
  21c7f4507efdfacb8489a8b6e044dcc2598c9ccd470d93646ae3883c7b5ec501 3.12.14
"$work/venv/bin/python" -m pytest -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
FAKE_UPTIME=120 "$work/venv/bin/python" -m pytest -p tools.fakeboot -p tools.pytest_phases \
  tests/ -q -s -p no:cacheprovider
"$work/venv/bin/ruff" check --no-cache .
```

Use the floor when your platform cannot run the exact .10 bundle, and label the result
with its actual floor versions. It does not validate .10. Do not disable ordinary HA
plugin loading, replace failed phases with collection, or infer hardware results from
mock entities.


## How the coordinator is split

`coordinator.py` still runs most of the control loop. Concerns are moving out of it one at
a time. Each moves into a module that owns its state, its wakeups and its stored record:

| Module | What it owns |
| --- | --- |
| `logic.py` | The decision math: room calls, arbitration, door/window states, floors, setpoints. No Home Assistant. |
| `cloud.py` | A cloud or generic head's write record and its setpoint math. No Home Assistant. |
| `plan.py` | The plan one compute decides (`Plan`, and a `RoomPlan` per room), which the apply, the mode's writeback, the dwell wakeup and the room thermostat read back as typed fields. `to_attributes()` renders it as the plan sensor, the room rows and diagnostics show it. A room row also carries what other controllers render for it (delivery, head, floor, Dry, door/window), kept as each one renders it. No Home Assistant. |
| `openings.py` | The door/window pause: each room's record and its store, the delay wakeups, the notices, and the vanes a resume owes. |
| `dry.py` | Allow Dry: which rooms are held or parked in dry, a Dry picked on a thermostat and its wakeup, a room released from dry, and the Dry notices. In memory only. |
| `fan_hold.py` | Fan boost and the manual fan hold, per head: the ladder's rung, whether a person's pick is held, the echo memory of MXZ's own writes (`FanEcho`), and what the Fan auto switch restores. The switch keeps the restore data; `FanHold` reads and writes it through `restore`, `command_memory` and `older_record`. |
| `delivery.py` | The delivery queue: one plan decided at a time, each head's commands in turn (`Delivery`), across reloads, the one follow-up refresh owed to a request made during a plan, and what each software call to a head returned. On Home Assistant before 2025.11 it makes up for two gaps in the request debouncer; that code goes when the minimum version reaches 2025.11. |
| `heads.py` | Each room's head driver behind one interface (`HeadDriver`): `LocalDriver` for a local head and `CloudDriver` for a cloud or generic one, with its `CloudHead` record, its batches, whether it is out of control, and its repair issue. `HeadGrid` reads what a head accepts: its native band and its setpoint grid. `HeadDrivers` settles each room's head type at the start, runs a delivery through the head's driver, and keeps the one wakeup the cloud heads share. |
| `floor.py` | The freeze floor and overheat ceiling of each disabled room that sets one (`FloorGuard`): the run and its no-progress clock, a person's setting a hands-off run took over and the hand-back that returns it, a yield to a person, the floor notices and their hourly limit, and the record the room's enable switch restores. |
| `host.py` | `ControllerHost`, the coordinator members every controller uses: Home Assistant, the clock, `retired`, `notify`, `redraw_rooms` and the two recomputes. `HeadCommands`: the delivery queue's `async_send`, for a controller below it. `Effect`: one thing a compute decided that reaches outside MXZ's memory. |

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

Still in `coordinator.py`: the compute and the apply, the mode-change hold and its record,
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
`tools/import_bounds.py`. See [ARCHITECTURE-RULES.md](ARCHITECTURE-RULES.md).

## Restore, flow schemas, and abort text

Restore uses normal `RestoreEntity` state/extra data; flow schemas use Voluptuous. MXZ
raises its own `reconfigure_successful` and `already_configured` aborts without HA's
central translation domain (the floor has no such parameter), so both strings ship
locally in `strings.json`/`translations/en.json`. Tests resolve every flow reason
through HA's translation loader on each lane.

On HA 2026.10.0 (frontend 20260930.2) the real frontend rendered the local text,
“Reconfiguration was successful.” with its trailing period, not HA's central text,
which has none ([screenshot](../images/mxz-3.12-reconfigure-complete.png)). The
floor's rendering of local abort text was not captured, and `already_configured` was
not rendered.
