# verify-mxz on a throwaway Home Assistant

The second driver of the verify-mxz skill (`.claude/skills/verify-mxz/`). The first
drives MXZ inside `pytest-homeassistant-custom-component`, in memory, on a test clock.
This one runs the real integration in a real `hass` process on the real clock, sets
it up through the setup flow over Home Assistant's API, and keeps the event stream,
the log and the recorder history. [FEATURE-MAP.md](FEATURE-MAP.md) says which
journeys belong here, what each proves, and which are proven so far.

Never point it at a real Home Assistant, a real head or the LAN, and never read a
credential. It has no host argument: it only talks to the instance it started,
inside a network namespace that has only loopback. The heads are invented
(`invented_house/`); nothing it records is a hardware result.

## Launch

Needs root (for the namespace), `ip`, and a Python lane interpreter. Once per lane,
build the instance's own environment: the lane's lock from `requirements/`, plus the
`home-assistant-frontend` wheel its Home Assistant pins. Home Assistant's bootstrap
always loads the frontend, and without it the HTTP server never starts and Home
Assistant falls back to recovery mode, so no test lane can run an instance. No lane
is changed.

```sh
export MXZ_INSTANCE_HOME=~/mxz-throwaway-ha   # the default; instances and environments live here
MXZ_PYTHON=<lane venv>/bin/python verification/instance/mxz-instance env
verification/instance/mxz-instance up
```

`MXZ_INSTANCE_LANE` picks the lock (default `py314-ha2026.9.4`; also
`py312-ha2024.12.5`, `py313-ha2026.2.3`). `up` builds a fresh instance directory,
copies in this checkout's `custom_components/mxz_coordinator` and the equipment,
records the revision and a digest of that code, creates the namespace `mxz-<name>`,
starts `hass --skip-pip`, creates an invented owner, and waits until the instance is
usable: the API answers, the nine invented entities have states, the recorder is
recording, and Home Assistant is running, not in recovery mode. It prints `READY`.
`up` does not set MXZ up; that is the first journey. `--name` runs a second instance
side by side in its own namespace (default `demo`).

The instance's login lives only in `<instance>/auth.json` (mode 0600) and is never
copied into evidence.

## Doctor

`verification/instance/mxz-instance doctor` is read-only. It prints the Python and
Home Assistant versions, the checkout revision and the one the instance was built
from, then checks, from inside the namespace: only `lo` exists and a TCP connection
to `192.0.2.1` (TEST-NET-1) fails; the process is the one `up` started (its pid and
config directory, never a process found by name); the API answers; the instance was
built from this checkout's revision and code (an edit after `up` is stale, a failure,
not a warning); and the log has no error from MXZ or the equipment. It ends with
`DOCTOR_OK`, or `DOCTOR_PROBLEM ...` lines and `DOCTOR_FAILED`. `drive` runs it first
and refuses on a failure.

## Drive

```sh
verification/instance/mxz-instance drive                    # every journey, in order
verification/instance/mxz-instance drive setup-and-enable    # feature map 5.1
verification/instance/mxz-instance drive room-targets        # 5.3; needs 5.1 to have run
```

One driver per instance at a time; a second is refused. Each journey is one module
in `journeys/`, a sequence of actions by a named actor, each followed by expectations
with a deadline:

- a person in Home Assistant: a service call on MXZ's entities, or the setup flow
  over `/api/config/config_entries/flow`;
- a person at the head: `invented_house.remote` (mode, setpoints, fan), which changes
  the head the way its remote does, with no `climate.*` call MXZ could see;
- the room: `invented_house.room` (temperature, window open);
- time: waiting on the real clock. Nothing moves a clock or writes a coordinator
  field.

MXZ's entities are found by unique-id key (`<entry_id>_primary_thermostat`, `_plan`,
`_coordinator_enable`), since entity ids follow names. MXZ's own calls carry no user
in their context, and the person's carry the instance user; `room-targets` checks
that as a negative control before the cross-journey checks rely on it.

Every journey ends with three checks over the whole recorded window: MXZ never
commands one head `heat` while its latest command to another is `cool`; MXZ never
sends `auto` or `heat_cool`, nor `dry` without Allow Dry; and the log has no error or
traceback from MXZ or the equipment. An ERROR or CRITICAL block counts when its header
or traceback names `mxz_coordinator` or `invented_house`, whatever the logger: Home
Assistant logs an exception from MXZ's listeners, tasks and timers under its own
`homeassistant.core` and `homeassistant` loggers. The doctor uses the same rule.

## Evidence

Under `$MXZ_VERIFY_OUT/instance/` (default
`~/verify-mxz-evidence/<UTC stamp>/instance/`), outside the checkout:

| File | Content |
|---|---|
| `run.json` | Revision, instance, isolation, Python, Home Assistant, each feature's outcome, exit status. |
| `<feature>/<journey>/transcript.json` | One entry per step: actor, action, time, what was then shown (plan rows, room thermostats, enable, Fan auto, targets, heads), the service calls since the step before (`by`: person or mxz, `to_head`), and each expectation with its result. Outcome and error. |
| `events.jsonl` | Every `call_service` event, and every `state_changed` event of MXZ's and the equipment's entities. This is the state history of record: the recorder drops the mode-change hold attributes. |
| `history.json` | Recorder history of the same entities over the journey. |
| `home-assistant.log` | The log from the journey's first step to its last. |
| `diagnostics.json` | MXZ's own redacted diagnostics, at the end. |
| `flow.json`, `entities.json` | 5.1 only: each setup-flow page, and MXZ's entity ids by key. |

A transcript is written pass or fail; a crash is a failed journey with its error.
A journey that fails is a finding: report its step and the state seen. Never loosen
a journey to make it pass.

## Cleanup

```sh
verification/instance/mxz-instance down     # stop the process this instance started
verification/instance/mxz-instance reset    # remove the instance directory and its namespace
```

Neither touches evidence or the environment under `$MXZ_INSTANCE_HOME/env/`. Delete
an environment yourself when no checkout needs it.

## Helpers

- `mxz-instance env|up|doctor|drive [feature ...]|exec -- <command>|down|reset`
  (executable). `exec` runs a command inside the instance's namespace, for a browser
  that films the UI: the frontend is installed, so the instance serves it at
  `http://127.0.0.1:18123` there, and nowhere else.
- `home.py`: the instance (directory, namespace, process, lock). Knows nothing
  about MXZ.
- `invented_house/`: the equipment, three rooms (Studio, Loft, Den) from
  `house.json`, each with a head, a temperature sensor and a window sensor. Its device
  state lives in `config/invented_house.json` and outlives the process. It is copied
  into the instance at `up` and never ships.
- `client.py`: Home Assistant's REST and WebSocket API. Knows nothing about MXZ's
  rules.
- `journeys/drive.py`: actors, MXZ's entities, snapshots, expectations.
- `expect.py`, `evidence.py`: the event stream with deadlines and the cross-journey
  checks; the transcript and attachments.

The pure parts are tested in the ordinary suite (`tests/test_verify_instance.py`).
