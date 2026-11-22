# Simulator: stock versus MXZ Coordinator

Open the simulator at https://dkpnw.github.io/ha-mxz-coordinator/simulator/.

Change room temperatures, targets and wall remotes, and compare stock heads against MXZ
Coordinator in two rows, one above the other. It runs in your browser with nothing to install.
The heads and rooms are invented; nothing here measures real equipment, energy, comfort or
noise.

## Run it from your own copy

Download or clone the repository and open `docs/simulator/index.html` in a browser.

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
1 °F a minute, a room no head moves drifts toward 76 °F, and the fan speed does not change
either rate. Each room moves every 5 simulated seconds, a second after the room before it.

## What it leaves out

- Door and window pauses, Dry, freeze floors and overheat ceilings, hands-off rooms, cloud
  heads, eco and the season lockouts, a dual setpoint, a room's own drift, and the shared-mode
  select.
- Heads that answer late or become unavailable. These heads show each change at once.
- MXZ sends to one head after another, where Home Assistant can interleave the heads' writes.
  A command a newer plan replaces before it is sent is not modeled.
- When MXZ takes a change made at a head while a plan's commands are still going out, Home
  Assistant can work out the next plan at once. Here the next plan waits for Home Assistant's
  10-second cooldown, so a head can get its next command up to 10 seconds later than in Home
  Assistant.
- MXZ takes a change made at a head 5 seconds after it and writes it to the room. When that
  write starts a plan, Home Assistant finishes taking the change inside the plan that comes
  next: the write is a service call that waits for its plan, and the follow-up plan starts
  before the wait returns. No MXZ function says so, so this page follows that order by a rule
  of its own (`MxzRow._go_on`). It shows when two heads are changed at the same moment.

## Files

| File | What it is |
| --- | --- |
| `index.html`, `lab.css`, `lab.js` | The page. `lab.js` draws the rows and passes the controls to Python. |
| `world.py` | Both rows, the rooms' model, the clock and the scenarios, with their titles. |
| `mxz_row.py` | The MXZ row: the integration's functions over the invented heads. |
| `bundle.js` | Generated: the Python the page runs, as strings, so the page works from a file. |

The page loads [Pyodide](https://pyodide.org/) (Python in the browser, about 10 MB) from
cdn.jsdelivr.net the first time, pinned to release 314.0.8, so it needs an internet connection
then.

After changing any Python the page runs, regenerate the bundle with
`python3 tools/simulator_bundle.py`. `tests/test_simulator.py` fails while it is stale.

## How it is checked

`tests/test_simulator.py` runs scripted scenarios twice: on the real coordinator in Home
Assistant over the real invented heads, and on `mxz_row.py` from the shipped bundle in a Python
with no Home Assistant.

- **Parity.** After every step the two must agree on the plan, every call to each head, every
  pending wakeup and when it runs, each compute and why, what each head shows, and at a restart
  what MXZ saved. The scenarios cover mixed demand, everyone at target, a standoff, Comfort
  priority, the wait between heat and cool, a fan speed a person picks, the fan handed back
  before an off, a restart inside the wait with a held fan, and changes made at a head, two of
  them at one instant.
- **Broken copies.** Broken copies of `mxz_row.py` must fail them.
- **Timing edges.** When two heads' setpoints are changed at one instant, the real
  coordinator's number of computes varies from run to run with how Home Assistant interleaves
  the two heads' writes, so that case is checked on the calls to each head and what the rooms
  and heads end with. Three more scenarios have a change at a head taken at the instant a plan
  starts: at the end of the 10-second cooldown, at the end of the wait between heat and cool,
  and in the page's standoff after two remote presses. They must agree in full, the number and
  times of the computes included. One more turns two heads off at one instant and then changes
  a reading 5 seconds later; it fails if the row drops its rule of its own.
