# MXZ Coordinator verification map

This directory is the maintained source for verifying what a person sees MXZ
do. Read this index before driving, then use the matching feature file as the
recipe. Each feature has one journey module in `../journeys/`.

## Baseline preconditions

- A Python environment with the test requirements (`$MXZ_PYTHON`; on this
  host, a lane under `/root/mxz-work/lanes/`). See `../SKILL.md` Launch.
- `scripts/verify doctor` ends with `DOCTOR_OK` (sockets blocked, this
  checkout's integration loads).
- Every journey builds its own Home Assistant in memory with invented heads
  (`climate.mock_head_a`, `climate.mock_head_b`) and room sensors
  (`sensor.room_a_temp`, `sensor.room_b_temp`, or the Dry rig's own). Rooms
  start at 70 with target 70 unless a recipe says otherwise.
- Never drive a real Home Assistant, a real head, or anything on the LAN.

## Driving conventions

- Run journeys through `scripts/verify run <feature>`, which runs them on the
  host clock and again with `-p tools.fakeboot`.
- A person acts through Home Assistant services on MXZ's entities or on a
  head. A sensor acts by writing its state. A head acts through its own
  entity. Time passes only through the suite's clock helpers (`../SKILL.md`
  Drive).
- Use the suite's setup helper each recipe names; do not write decision state
  into the coordinator to set a scene.

## Proof and skip reporting

- Proof is the journey's assertions passing on both clocks, plus its
  transcript `<clock>/<feature>/<journey>.json` in the evidence directory,
  which pairs each action with the resulting plan, thermostats, heads, and
  the service calls MXZ sent.
- Report the evidence directory, the revision in `run.json`, and the lane.
- A journey that fails is a finding: report the step and the observed state
  from its transcript. Never weaken a journey to make it pass; put the
  reason in that feature's `Gotchas`.
- Do not report a feature as verified through a different journey than the
  one its file lists.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the
user-visible behavior, then exactly four H2 sections in this order:
`Sub-features`, `How to get to it (user POV)`, `Driving it with
pytest-homeassistant-custom-component` (starting with `Preconditions:`, then
labeled bullets pairing each action with what it does and the observable
result), and `Gotchas`.

## Features

- [Mixed-mode rooms](./mixed-mode-rooms.md): one room calls cool, another
  heat; priority wins, the other waits paused and resumes after the wait.
- [Unavailable or stale heads](./unavailable-stale-heads.md): a lost head's
  call holds while stale and lapses at the limit; a stale room sensor stops
  calling and one fresh report recovers it.
- [Restart continuity](./restart-continuity.md): a restart keeps only what is
  left of the mode-change wait, and a run in progress resumes.
- [Manual fan choices preserved](./manual-fan-preserved.md): a fan speed a
  person picks on a head holds, through a restart, until Fan auto is back on.
- [Dry refused by a head](./dry-refused-by-head.md): Dry picked on the
  thermostat that the head rejects or never reports goes back to Heat/Cool,
  and dry is sent once.
