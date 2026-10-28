# Contributing to MXZ Coordinator

Thanks for helping. Bug reports, fixes, and ideas are all welcome. Please read this first, especially before you change how MXZ is controlled.

## What MXZ is

MXZ works like a Tesla's cabin climate in Auto. You set one temperature per room, and MXZ decides whether to heat or cool. You never pick a mode.

Home thermostats usually ask for a heat setpoint and a cool setpoint. That confuses most people, and MXZ exists to get rid of it. Simplicity is the product.

Out of the box, setting a temperature is all anyone has to do: the defaults should give a comfortable room with no tuning. Under that, MXZ has deep configuration for power users who want to tinker, such as drift, uneven drift, per-room drift, an opt-in dual setpoint, fan, vane, and safety options, so they can get exactly the behavior they want.

## Design principles

- **One target per room, by default.** Each room has a single temperature out of the box, and that stays the product's identity. Drift and uneven drift ("Let it get cooler by" and "Let it get warmer by") give each room an adjustable band around its target. A power user can also switch one room at a time to a dual setpoint (separate Heat to and Cool to) on that room's page. MXZ won't make a dual setpoint the default, and won't add simultaneous heat and cool or the heads' hardware Auto. A dual setpoint is still not a mode picker: MXZ still chooses heat or cool for the whole system.
- **No mode selection.** MXZ picks heat or cool for the whole system. Users don't.
- **The defaults just work, and power users get the knobs.** Someone who only ever sets a temperature should get a comfortable room. Tuning settings like drift belong in the advanced options, and a new one must be optional and keep today's behavior when left blank.
- **Honest, visible state.** What Home Assistant, Apple Home, and Google Home show should match what the heads are really doing. Each room also shows, read-only, where it will start heating and where it will start cooling (`heat_start`, `cool_start`, `heat_demand_below`, and `cool_demand_above` on the plan sensor and the room's thermostat), so the band around the target is never hidden.
- **Safe by default.** New behavior that could affect comfort or safety starts off. It also has to survive a Home Assistant restart and respect a fan speed the user picked by hand.

## Before you open a pull request

- **Fixes, reliability, and safety:** go ahead and open a PR.
- **Anything that changes how people control MXZ** (the thermostat tile, setpoints, modes, or new controls): please open an issue first so we can talk it through. A PR that changes the controls without an agreed issue will probably be closed.
- **New tuning options:** welcome. Open an issue first, keep the option off or blank by default, and show that leaving it unset changes nothing.
- **Tests:** keep the full test suite passing, and add tests for new behavior. Please don't weaken an existing test. [Developer setup](docs/DEVELOPMENT.md) has the tested Home Assistant versions and how to run the suite locally.
- **Hardware:** say in the PR whether you tested on real hardware.
- **Writing:** MXZ docs, strings, and PR text use plain English and the Oxford comma.

Contributors are credited in the README. Thanks for helping keep MXZ simple to use and rewarding to tune.
