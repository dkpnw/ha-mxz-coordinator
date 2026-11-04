---
name: verify-mxz
description: "Drive the MXZ Coordinator Home Assistant integration (custom_components/mxz_coordinator) the way a person and the heads do, inside pytest-homeassistant-custom-component, and keep a JSON transcript of what it showed and sent. The surface is the HA entities MXZ creates (room thermostats, enable and Fan auto switches, target numbers, the shared-mode select, the plan sensor) and the head and room-sensor states it reads. Use to prove a change to coordination, mode changes, restarts, fan handling, or Dry behaves for a user: mixed-mode rooms, unavailable or stale heads, restart continuity, manual fan choices, and Dry refused by a head."
---

# Verify MXZ Coordinator

MXZ is a Home Assistant integration, not a server: there is no app to start
and no UI to click. A user meets it through Home Assistant entities, and the
heads and room sensors meet it through their states. This skill drives the
real integration code through Home Assistant's own test harness
(`pytest-homeassistant-custom-component`), with the suite's invented heads and
sensors, and records each step.

Never point any of this at a real Home Assistant, a real head, or the LAN, and
never read a token file. The harness blocks sockets; the doctor proves it.

All paths below are relative to this skill's directory unless they start with
the repository root (`custom_components/`, `tests/`, `tools/`).

## Launch

One Home Assistant per test, in memory, created and torn down by the `hass`
fixture. Launching means choosing a Python environment that has the test
requirements:

- On this host, the five lanes are ready-made:
  `/root/mxz-work/lanes/{py312-ha2024.12.5,py313-ha2026.2.3-r1103,py314-ha2026.9.4,py314-ha2026.10.4,py314-ha2026.11dev}/bin/python`.
  `py314-ha2026.10.4` is the default to use.
- Anywhere else, build a venv per `docs/DEVELOPMENT.md` (the 2024.12.5 floor
  recipe there, or the lock for your lane) and use its `bin/python`.

Then, from anywhere:

```sh
export MXZ_PYTHON=/root/mxz-work/lanes/py314-ha2026.10.4/bin/python
scripts/verify doctor
scripts/verify run                       # every feature
scripts/verify run restart-continuity    # one or more features
```

`scripts/verify` changes to the repository root and runs
`$MXZ_PYTHON -m pytest` there. `python -m` puts the root on `sys.path`, so the
journeys can import `tests.*` and `custom_components.*`; plain `pytest` does
not. It passes the journeys directory as an explicit path: pytest skips
dot-directories only when it recurses, and CI's `pytest tests/` never collects
these journeys. Each run goes twice, as CI does: once on the host clock, once
with `-p tools.fakeboot` and `FAKE_UPTIME=120` (the clock of a host that booted
two minutes ago). Ready means the summary lines print `N passed`.

Runs are isolated by construction (one in-memory Home Assistant per test, one
evidence directory per run), so two runs can go side by side.

## Doctor

`scripts/verify doctor` is read-only (no evidence, no caches, no bytecode). It
prints the Python, Home Assistant, plugin and pytest versions, the
integration's manifest version, and `git rev-parse HEAD` (with `-dirty` when
the tree has changes), and checks a lane's name against its versions. Then it
runs `journeys/test_doctor.py`:

- `test_the_network_is_blocked`: inside a `hass` test, a TCP connection to
  `192.0.2.1` (TEST-NET-1, never a real host) raises pytest-socket's
  `SocketBlockedError`. A journey cannot reach the LAN.
- `test_this_checkout_s_integration_loads`: this checkout's integration sets
  up a config entry over the suite's invented heads, and prints
  `DOCTOR_INTEGRATION=mxz_coordinator <version>`.

It ends with `DOCTOR_OK`, or `DOCTOR_PROBLEM ...` / `DOCTOR_FAILED`. Run it
first, and again whenever a run looks wrong.

## Drive

Each feature is one module in `journeys/`, a sequence of what a person or a
head does, each followed by assertions on what Home Assistant then shows. The
recipes, with every step and its expected result, are in
[`features/README.md`](features/README.md). The actions are real service calls
and state writes:

- a person: `switch.turn_on` on `switch.mxz_coordinator_*_enable` or
  `*_fan_auto`, `number.set_value` on `*_target`, `climate.set_hvac_mode` on
  `climate.mxz_coordinator_*_thermostat`, `select.select_option` on
  `select.mxz_coordinator_shared_mode`, `climate.set_fan_mode` on a head;
- a room sensor: `hass.states.async_set("sensor.room_a_temp", ...)`;
- a head: going unavailable or reporting a mode through its own entity, as
  its integration would (`tests/test_lost_vote.py` `_lose`/`_back`);
- Home Assistant: a restart, in process (`journeys/mxz_journey.py`
  `restart`: restore data dumped, entry unloaded, storage re-read on setup).

Time passes with the suite's own clock helpers, never by writing a decision
into the coordinator: `tests/dwell.py` `expire_dwell`, `tests/clocks.py`
`jump_wall_clock`/`age_lost_vote`, `tests/test_antiflip_persist.py`
`_let_time_pass`, `freezer` for sensor freshness, and
`async_fire_time_changed` (through `tests/test_dwell_wakeup.py` `_fire_due`)
so Home Assistant's own timers fire.

To add a journey, start from the feature file and the suite test it cites,
reuse that test's setup helper, and open a transcript with the `journal`
fixture: `log = journal(hass, entry, FEATURE, heads=[head_a, head_b])`, then
`shown = log.step("what happened")` after each action and assert on `shown`.

## Evidence

Each run writes to `$MXZ_VERIFY_OUT`, by default
`~/verify-mxz-evidence/<UTC stamp>-<pid>/`, outside the checkout:

- `<clock>/<feature>/<journey>.json`, where `<clock>` is `host` or `fakeboot`:
  the outcome, the revision, the Home Assistant and Python versions, and one
  entry per step: the action in words, the plan sensor's state and per-room
  rows (demand, engage, temperature, sensor health, vote status, fan hold,
  Dry), each room thermostat's state, action and mode-change hold, each Fan
  auto switch, each head's mode, fan and setpoints, and every service call
  made since the previous step (`to_head: true` marks the ones aimed at a
  head, which is how MXZ's commands show).
- `<clock>/pytest.log`: pytest's output with `-rA`, including Home
  Assistant's captured log for each journey.
- `run.json`: revision, Python, features, and the exit status.

A transcript is written pass or fail. The proof is the assertions passing, on
both clocks, with the transcript showing the action and the resulting state
side by side. There are no screenshots: there is no UI in this harness, and
none may be faked. The heads are invented; nothing here is a hardware result.

## Cleanup

There is nothing to stop: each test's Home Assistant ends with the test.
`scripts/verify clean` removes only `journeys/__pycache__` (left by a run made
without the helper, which sets `PYTHONDONTWRITEBYTECODE=1` and
`-p no:cacheprovider`). It never touches the evidence directory. Delete old
evidence yourself when you no longer need it.

## Helpers

- `scripts/verify doctor|run [feature ...]|clean` (executable; usage above).
  Feature names: `mixed-mode-rooms`, `unavailable-stale-heads`,
  `restart-continuity`, `manual-fan-preserved`, `dry-refused-by-head`. Exit
  status 0 only when both clocks pass.
- `journeys/mxz_journey.py`: `Transcript` (the evidence), `entity(hass,
  entry, key)` (an MXZ entity_id by its unique_id key, such as
  `"primary_thermostat"`), and `restart(hass, entry)`.
- `journeys/conftest.py`: the suite's `tests/conftest.py` hook and fixture,
  and the `journal` fixture.

Keep the feature map honest as the integration changes with
`/maintain-verification-skill`.
