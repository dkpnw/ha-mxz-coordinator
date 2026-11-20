# Simulator: stock versus MXZ Coordinator

A page where you set the rooms, the heads and MXZ's settings yourself and watch two rows side
by side: stock, and MXZ Coordinator. It is a simulation with invented heads and rooms. It
measures nothing about real equipment, energy, comfort or noise.

## Open it

GitHub shows this folder's files, not the page. To run it, download or clone the repository
and open `docs/simulator/index.html` in a browser. The page needs an internet connection the
first time: it loads [Pyodide](https://pyodide.org/) (Python in the browser, about 10 MB)
from cdn.jsdelivr.net, pinned to release 314.0.8.

## What each row is

- **Stock** follows one rule from Mitsubishi's manual for heads that share an outdoor unit:
  "the unit selected last goes into standby mode." A person sets each head. It is not stock
  AUTO. [What stock AUTO does, with sources](../HOW-IT-WORKS.md#the-problem-in-detail).
- **MXZ Coordinator** is the integration's own code at its default settings: the planner, the
  timing and the fan hold in `custom_components/mxz_coordinator`, the same functions the
  integration calls in Home Assistant. `mxz_row.py` only turns the page's controls into
  their inputs and carries out the steps they return. Its timing follows Home Assistant 2025.11
  and later.

Both rows share the invented house of [the README's animations](../../README.md):
three rules in `verification/instance/invented_house/model.py`. A running head moves its room
1 °F a minute, a room no head moves drifts toward 76 °F, and the fan does not change either
rate. Each room moves every 5 simulated seconds, a second after the room before it.

The page leaves out door and window pauses, Dry, freeze floors and overheat ceilings,
hands-off rooms, cloud heads, eco and the season lockouts, a dual setpoint, a room's own drift,
the shared-mode select, and heads that answer late or go out of sight. MXZ sends to one head
after another, where Home Assistant can interleave the heads' writes, and a command a newer plan
replaces before it is sent is not modeled.
When MXZ takes a change made at a head while a plan's commands are still going out, Home
Assistant can work out the next plan at once. Here a plan's commands all go out inside it, so the
next plan waits for Home Assistant's 10-second cooldown, and a head can get its next command up
to 10 seconds later than in Home Assistant.

## Files

| File | What it is |
| --- | --- |
| `index.html`, `lab.css`, `lab.js` | The page. `lab.js` draws the rows and passes the controls to Python. |
| `world.py` | Both rows, the rooms' model, the clock and the starting points. |
| `mxz_row.py` | The MXZ row: the integration's functions over the invented heads. |
| `bundle.js` | Generated: the Python the page runs, as strings, so the page works from a file. |

After changing any Python the page runs, regenerate the bundle with
`python3 tools/simulator_bundle.py`. `tests/test_simulator.py` fails while it is stale.

## How it is checked

`tests/test_simulator.py` runs scripted scenarios twice: on the real coordinator in Home
Assistant over the real invented heads, and on `mxz_row.py` from the shipped bundle in a
Python with no Home Assistant. After every step the two must agree on the plan, every call to
each head, every pending wakeup and when it runs, each compute and why, what each head shows,
and at a restart what MXZ saved. The scenarios cover mixed demand, everyone at target, a
standoff, Comfort priority, the wait between heat and cool, a fan speed a person picks, the fan
handed back before an off, a restart inside the wait with a held fan, and changes made at a
head, two of them at one instant. Broken copies of `mxz_row.py` must fail them. When two heads'
setpoints are changed at one instant, the real coordinator's number of computes varies from
run to run with how Home Assistant interleaves the two heads' writes, so that case is checked
on the calls to each head and what the rooms and heads end with.
