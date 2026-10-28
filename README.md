<p align="center">
  <img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/docs/readme-banner.png" width="460" alt="MXZ Coordinator wordmark on a light card: a dial running cool blue on the left to warm orange on the right, two wall heads on the ring, outdoor unit at the hub.">
</p>

<p align="center"><b>Set one temperature per room. The coordinator chooses one shared heating or cooling mode.</b></p>

<p align="center">
  <a href="https://github.com/dkpnw/ha-mxz-coordinator/releases/latest"><img src="https://img.shields.io/github/v/release/dkpnw/ha-mxz-coordinator" alt="Latest release"></a>
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Default-41BDF5" alt="HACS: Default"></a>
  <a href="https://github.com/dkpnw/ha-mxz-coordinator#before-you-install"><img src="https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5" alt="Home Assistant 2024.12 or newer"></a>
  <a href="https://github.com/dkpnw/ha-mxz-coordinator/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/dkpnw/ha-mxz-coordinator/ci.yml?branch=main&label=CI" alt="CI status on main"></a>
  <a href="https://github.com/dkpnw/ha-mxz-coordinator/blob/main/LICENSE"><img src="https://img.shields.io/github/license/dkpnw/ha-mxz-coordinator" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open this repository in HACS."></a>
  <a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=mxz_coordinator"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Start setting up MXZ Coordinator."></a>
</p>

### Tesla-style Auto for your Mitsubishi multi-split

**Set a temperature in each room and forget it.** Like a Tesla's cabin climate in Auto,
MXZ Coordinator decides heat or cool, runs the fan hard when a room is far off and eases
it down as the room gets close, and has a room that's done step aside so the others get
served. One outdoor unit still means one mode at a time; MXZ just makes that call for you,
and makes it well.

- **No more stranded rooms.** Stock AUTO can leave a head waiting in standby for an hour
  while the other one runs. MXZ picks one mode for the whole system, and a room that has
  reached its target idles so the room that needs it gets served.
- **Heat or cool, chosen for you.** Stock heads each decide from their own reading, and
  they can disagree. With MXZ you only set temperatures. When rooms want opposite modes,
  the room you ranked first wins, and the mode won't flip back and forth.
- **A fan that works like a Tesla's.** The further a room is from target, the faster the
  fan runs, up to the top speed you allow, and it slows step by step as the room closes
  in. Pick a speed yourself and it stays put until you hand it back.
- **Reads the room, not the ceiling.** Each room runs on the temperature sensor you choose,
  not the thermistor inside a head mounted up near the ceiling.
- **Knows the season and when you're away.** Optional weather lockouts stop it heating on
  a day forecast to be warm, or cooling on a cold one. Away limits keep an empty house from
  getting too hot or cold, and an optional hold signal (say, from your utility) can pause it.
- **One clean thermostat per room.** Each room gets a single thermostat for Home Assistant
  dashboards, Apple Home, or Google Home, and everything is set up in the UI with no YAML.

| | Stock AUTO | MXZ Coordinator |
| --- | --- | --- |
| Heat or cool | Each head decides alone; the head selected last waits in standby. Some outdoor units have an installer switch that lets the lowest-address head decide for all ([sources](docs/HOW-IT-WORKS.md#the-problem-in-detail)) | One mode for the system, chosen by your room priority |
| A room that's done | Stays in its own mode, which can block the other | Steps aside: fan only, off, or dry the coil then off |
| Fan | The head's own fan setting | Speeds up when far off, eases down near target |
| Temperature it uses | Thermistor in the head | The room sensor you pick |
| Seasons and away | Manual | Weather lockouts, away limits, standby hold |

![Two rooms as single-target Auto dials beside the coordinator's decision state.](images/dashboard.png)

> Shared as-is; support is best-effort. Automated checks use invented heads and sensors.
> They establish software behavior, not physical performance on your equipment.

**Start here:** [Install](#install), then [set up your outdoor unit](#set-up-your-outdoor-unit).
No YAML is required.

**Contents:** [What MXZ does for you](#what-mxz-does-for-you) · [What's new](#whats-new-in-3111) ·
[Before you install](#before-you-install) · [Install](#install) · [Set up](#set-up-your-outdoor-unit) ·
[Everyday use](#everyday-use) · [Optional extras](#optional-extras) · [Tune it](#tune-it) ·
[If something looks wrong](#if-something-looks-wrong) · [Documentation](#documentation) ·
[Remove](#remove-the-integration) · [Credits](#credits--prior-art) · [License](#license)

<!-- Anchors below keep links to sections of the older, longer README working. -->
<a id="what-it-does"></a><a id="the-problem-stock-auto-starves-rooms"></a>

## What MXZ does for you

A Mitsubishi multi-split runs every head from one outdoor unit, and that unit can only
heat or cool at one time. Stock heads each pick their own mode, and when they
disagree, the head selected last waits in standby. The
[echavet/MitsubishiCN105ESPHome](https://github.com/echavet/MitsubishiCN105ESPHome)
firmware puts each head in Home Assistant as its own thermostat, which is great access
but still leaves the heads to decide alone. MXZ sits on top and makes one decision for
the whole system, so you just set a temperature in each room.

Every core feature exists for a reason someone ran into:

| MXZ does this | So that |
| --- | --- |
| **Picks one heat or cool mode for the whole system**, from every room's temperature and target. It never asks a head for hardware AUTO. | Coordinated rooms stop fighting over heat versus cool, and the shared direction follows the rooms that need it, not whichever head was set first. |
| **Settles a standoff by your room order**, and by default waits 10 minutes between heat/cool switches (Minimum time between mode switches). | The room you rank first wins, and the mode won't flip back and forth. |
| **Has a room that's done step aside.** On a new setup its head runs the fan to dry the coil after cooling, then turns off; Fan only and Off are also available. | The rooms that still need conditioning get served, and the coil dries before the head closes up. |
| **Lets a room coast** inside its drift (1 °F / 0.5 °C by default) before it runs again, then runs it all the way back to target. | A room that's close enough rests, and every run finishes where you asked. |
| **Runs each room on the temperature sensor you pick.** | Control follows the room you sit in, not a thermistor up near the ceiling. |
| **Boosts the fan** the further a room is from target, up to a top speed you choose, and eases it down as the room closes in. A speed you pick by hand is kept until you hand it back. | The fan works hard only while a room is far off. |
| **Gives each room one thermostat** for Home Assistant dashboards, Apple Home, or Google Home, all set up in the UI with no YAML. | One clean control per room, instead of raw head controls that can fight the plan. |
| **Puts a head back** if someone switches it to auto, `heat_cool`, or `dry`, or turns it off while its room needs it, and can notify you when that happens. | A stray remote press doesn't quietly break coordination. |
| **Follows the season** from a weather forecast or outdoor sensor, with heat-anyway and cool-anyway safety limits. | No heating on a day forecast to be warm, or cooling on a cold one. |
| **Holds an empty house between away limits** (the Eco idle switch), and can **pause on a standby signal** such as a utility load-shed. | Less running when nobody's home, and a clean response to a grid or utility hold. |
| **Keeps a lost head's last vote for a while** (30 minutes by default), then lets it go. | A short Wi-Fi dropout doesn't swing the shared mode, and a long one doesn't hold it forever. |
| **Shows its reasoning** on one plan sensor: each room's demand, start points, sensor health, and command status. | When a room waits, you can see why. |

[How it works](docs/HOW-IT-WORKS.md) walks through each decision and cites the
Mitsubishi manuals on stock AUTO.

## What's new in 3.12.0

A docs-only release: the README now displays properly in HACS, and you install MXZ
Coordinator from the HACS default store. 3.11.0 added the stale, then expired, vote of
a head that goes unavailable, with **Keep a lost head's vote for** in Configure →
Comfort → Advanced. Recent release notes are in the [changelog](CHANGELOG.md); older
releases are covered in [Migration](docs/MIGRATION.md).

> **Upgrading from 3.3.0 or earlier?** **3.4.0** changed setup, sensor handling, the
> room lifecycle, and command delivery. Read
> [Migration](docs/MIGRATION.md#upgrading-to-340) before you upgrade.

## Before you install

| You need | Details |
| --- | --- |
| Home Assistant | **2024.12.0 or newer**. |
| Heads | **2–8 heads on one outdoor unit.** Add a separate coordinator entry for each outdoor unit. |
| Head modes | Each head's `climate` entity must offer `heat`, `cool`, and a common idle action. |
| Room sensors | A separate temperature `sensor` for each room. |

MXZ is built against
[echavet/MitsubishiCN105ESPHome](https://github.com/echavet/MitsubishiCN105ESPHome).
Earlier releases have field experience on two- and three-zone hardware, with six-zone
and multiple-outdoor-unit reports. Kumo Cloud, MELCloud, and other climate integrations
are unvalidated here. Simultaneous heat/cool and branch-box VRF are out of scope. The
[full requirements](docs/SETUP.md#requirements) cover idle-action and fan support.

## Install

1. Open **HACS**, search for **MXZ Coordinator**, and select **Download**. You can also
   [open it in HACS directly](https://my.home-assistant.io/redirect/hacs_repository/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration).
2. Restart Home Assistant to load the integration.
3. Open **Settings → Devices & services → Add integration → MXZ Coordinator**.
4. Complete the setup below. Installation alone does not enable control.

**Without HACS:** copy the release's `custom_components/mxz_coordinator` directory into
Home Assistant's `custom_components` directory, restart, then add the integration.

**Coming from the YAML package?** Follow
[Migration](docs/MIGRATION.md#moving-from-the-yaml-package), and stop its automations
before letting the integration control the same heads.

<a id="if-setup-stops-or-warns"></a><a id="reconfigure-heads-rooms-and-sensors"></a>

## Set up your outdoor unit

Setup has four steps. Only **Save and finish** creates the entry.

1. **Heads.** Select every head on this outdoor unit, in priority order. Position 1
   wins a heat-versus-cool standoff.
2. **Rooms.** Check each pre-filled room name.
3. **Room temperature sensors.** Pick one sensor per room.
4. **Check this before you save.** Read the summary, then **Save and finish**.
   **Change advanced settings** is optional; skip it and MXZ uses defaults for your
   temperature unit.

**Then turn it on.** A new entry starts with **Coordinator enable** and every room off,
so it commands nothing until you:

1. open the integration's device page and set the room targets,
2. enable the rooms you want to coordinate, and
3. turn on **Coordinator enable**.

Expose the per-room thermostats to HomeKit or Google, and avoid also exposing the raw
head for the same room. [Setup in detail](docs/SETUP.md) has screenshots of every step,
[what each error means](docs/SETUP.md#if-setup-stops-or-warns), and how to use
[Reconfigure](docs/SETUP.md#reconfigure-heads-rooms-and-sensors) to add, remove, or
reorder heads later.

<a id="everyday-control"></a><a id="who-drives-the-fan"></a><a id="how-a-satisfied-head-idles"></a>

## Everyday use

- **Each room's thermostat** takes one target and `heat_cool` or `off`. `heat_cool`
  means MXZ picks the shared direction. Turning the tile `off` disables that room, and
  by default its head is held off.
- **Coordinator enable** off hands the heads back to their own controls; they keep
  their last commanded state.
- **Shared mode** shows the current heat or cool. Picking one switches to it; MXZ can
  switch again after the mode-switch wait.
- **Each room's drift number** overrides Allowed drift for that room; **Follow global
  drift** hands it back.
- **Fan auto** on gives the fan back to fan boost; picking a speed holds it.
- **Heat lockout**, **Cool lockout**, and **Eco idle** switches cover the season and
  away. The example [day/night/away presets](examples/presets.yaml) set targets and Eco
  idle.

The [feature guide](docs/FEATURES.md#everyday-control) explains each control in full,
including [who drives the fan](docs/FEATURES.md#who-drives-the-fan) and
[how a satisfied head idles](docs/FEATURES.md#how-a-satisfied-head-idles).

<a id="uneven-drift"></a><a id="per-room-uneven-drift"></a><a id="give-a-room-separate-heat-and-cool-setpoints-dual-setpoint"></a><a id="run-one-room-from-its-own-remote"></a><a id="safety-priority"></a><a id="when-a-head-goes-unavailable"></a><a id="freeze-floor-and-overheat-ceiling"></a>

## Optional extras

Each is off or blank until you set it:

- **Uneven drift**, globally or [per room](docs/FEATURES.md#per-room-uneven-drift): let a
  room get cooler than it may get warmer, or the other way round.
  [Details](docs/FEATURES.md#uneven-drift)
- **Dual setpoint**, enabled separately for each room: separate Heat to and Cool to, like
  an ecobee or a Nest. One target per room stays the default.
  [Details](docs/FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint)
- **Freeze floor and overheat ceiling**: a disabled room is heated or cooled back to
  safety. [Details](docs/FEATURES.md#freeze-floor-and-overheat-ceiling)
- **Safety priority**: while a sensor you wire reads `on`, that room's own heat or cool
  outranks ordinary room demands. A tripped freeze floor ranks first; other safety
  demands and tripped overheat ceilings are settled by room order.
  [Details](docs/FEATURES.md#safety-priority)
- **Run one room from its own remote**: turn on **Let its own remote run it while
  disabled**, and a disabled room is left to its remote while its heat or cool still
  counts. [Details](docs/FEATURES.md#run-one-room-from-its-own-remote)
- **Sensor freshness**: tell MXZ how often a room's sensor reports, and a room whose sensor
  goes quiet stops asking for heat or cool.
  [Details](docs/CONFIGURATION.md#choose-a-sensor-freshness-contract)

What happens when a head drops off the network is in
[When a head goes unavailable](docs/FEATURES.md#when-a-head-goes-unavailable).

<a id="configure-comfort-and-sensor-settings"></a><a id="saving-rules-warnings-and-clearing-fields"></a><a id="choose-a-sensor-freshness-contract"></a>

## Tune it

Open **Settings → Devices & services → MXZ Coordinator → Configure**. It's a menu of six
pages, and each saves only what it shows:

| Page | What you set |
| --- | --- |
| **Comfort** | Allowed drift, Mode switch threshold, Resting mode, and (under Advanced) the mode-switch wait, uneven drift, and how long a lost head's vote counts. |
| **Fan and idle** | Fan boost and its top speed, and what a room does when it reaches target. |
| **Seasons** | A weather source and the heat and cool lockout temperatures, with safety overrides. |
| **Away and setpoint limits** | The away band, and the lowest and highest setpoint a head may get. |
| **Standby hold** | An optional hold signal, its active state, and what heads do during a hold. |
| **Rooms** | One page per room: vanes, airflow sensor, safety priority, own-remote control, uneven drift, sensor freshness, freeze floor, and dual setpoint. |

[Configuration](docs/CONFIGURATION.md#every-setting-and-its-default) lists every setting
with its default in °F and °C, the
[saving rules](docs/CONFIGURATION.md#saving-rules-warnings-and-clearing-fields), and how
to [choose a sensor freshness contract](docs/CONFIGURATION.md#choose-a-sensor-freshness-contract).

<a id="diagnose-a-room-that-is-waiting"></a><a id="faq-and-troubleshooting"></a><a id="faq-waiting"></a>

## If something looks wrong

- **Nothing happens after setup?** Turn on **Coordinator enable** and each room.
- **A head looks off but the room is enabled?** That's a satisfied room parked by its
  idle action, not a fault.
- **A room is waiting?** One outdoor unit runs one direction at a time. Open
  `sensor.*_plan` and check `standoff`, the room's `demand`, and `mode_change_allowed`.
  [More](docs/TROUBLESHOOTING.md#faq-waiting)

[Troubleshooting and FAQ](docs/TROUBLESHOOTING.md) has these and more:
[diagnosing a waiting room](docs/TROUBLESHOOTING.md#diagnose-a-room-that-is-waiting),
with what every plan attribute means, and the [FAQ](docs/TROUBLESHOOTING.md#faq).

<a id="best-practice-give-the-firmware-your-room-sensor-too"></a><a id="developer-setup-and-tested-support"></a>

## Documentation

| Page | What's in it |
| --- | --- |
| [Setup](docs/SETUP.md) | Requirements, install, every setup step with screenshots, Reconfigure, and removal. |
| [Configuration](docs/CONFIGURATION.md) | Every setting and default, saving rules, and sensor freshness. |
| [Features](docs/FEATURES.md) | Everyday control, drift, dual setpoint, own-remote rooms, safety priority, lost heads, freeze floor, fan, and idle. |
| [How it works](docs/HOW-IT-WORKS.md) | Stock AUTO in Mitsubishi's manuals, how MXZ decides, [feeding the firmware your room sensor](docs/HOW-IT-WORKS.md#best-practice-give-the-firmware-your-room-sensor-too), and limits of control. |
| [Troubleshooting and FAQ](docs/TROUBLESHOOTING.md) | Diagnosing a waiting room, and answers to common questions. |
| [Entity map](docs/ENTITY-MAP.md) | The integration's diagnostic attributes, and what to edit in the legacy YAML package. |
| [Migration](docs/MIGRATION.md) | Upgrade notes for each release, and moving from the YAML package. |
| [Changelog](CHANGELOG.md) | Release notes, newest first. |
| [Contributing](CONTRIBUTING.md) and [Developer setup](docs/DEVELOPMENT.md) | Design principles, pull requests, [tested support](docs/DEVELOPMENT.md#tested-support), test lanes, and releasing. |

<a id="removing"></a>

## Remove the integration

1. Delete the entry through **Settings → Devices & services → MXZ Coordinator → ⋮ →
   Delete**.
2. Then remove its download from HACS. Removing files first leaves a broken entry.

Heads keep their last commanded state; use their own controls if they stay parked.
[Removal details](docs/SETUP.md#remove-the-integration).

## Credits & prior art

MXZ Coordinator got this good because people ran it on their own systems, found the edges, and took the time to write them up. Some sent the fix too. Thank you all.

- [@calvindomenico](https://github.com/calvindomenico) found the restart bug where a `fan_only` idle room came back with a fan hold that never cleared, with timing across three zones that pinned it down ([#25](https://github.com/dkpnw/ha-mxz-coordinator/issues/25)). He then sent [#26](https://github.com/dkpnw/ha-mxz-coordinator/pull/26), which found the no-record restart path. His idea and test were ported into 3.4.0 with co-author credit. Before that, he caught a ducted air handler's phantom vane and sent the fix ([#9](https://github.com/dkpnw/ha-mxz-coordinator/pull/9), 3.0.2). He traced a rejected setpoint on °C-native heads down to the rounding step ([#10](https://github.com/dkpnw/ha-mxz-coordinator/issues/10), fixed in 3.0.2). And he proposed, designed, and built the standby hold ([#12](https://github.com/dkpnw/ha-mxz-coordinator/issues/12), [#13](https://github.com/dkpnw/ha-mxz-coordinator/pull/13), 3.1.0).
- [@amosyuen](https://github.com/amosyuen) caught that the room tile dropped `hvac_mode` from `climate.set_temperature`, and came with the root cause and the exact line of code ([#17](https://github.com/dkpnw/ha-mxz-coordinator/issues/17), 3.1.3). He asked for per-room drift, and his presence-tier idea shaped its design ([#18](https://github.com/dkpnw/ha-mxz-coordinator/issues/18), 3.2.0). He also caught the entry showing up under Helpers instead of Integrations ([#19](https://github.com/dkpnw/ha-mxz-coordinator/issues/19), 3.1.3). His push to consolidate entities ([#20](https://github.com/dkpnw/ha-mxz-coordinator/issues/20)) got a written explanation of why each room's number and switch are the source of truth.
- [@andrewblane](https://github.com/andrewblane) found on a 6-zone system that the first two zones ignored their own names, and sent the fix ([#8](https://github.com/dkpnw/ha-mxz-coordinator/pull/8), 3.0.0-beta.16). He also caught a parked room's tile showing heating or cooling during a standoff, and sent that fix too ([#16](https://github.com/dkpnw/ha-mxz-coordinator/pull/16), 3.1.3).
- [@helicopterrun](https://github.com/helicopterrun) validated it on 3-zone hardware and did relentless, root-caused QA through the v3 beta ([#4](https://github.com/dkpnw/ha-mxz-coordinator/issues/4), [#5](https://github.com/dkpnw/ha-mxz-coordinator/issues/5), [#6](https://github.com/dkpnw/ha-mxz-coordinator/issues/6), [#7](https://github.com/dkpnw/ha-mxz-coordinator/issues/7)). Those reports led to the removal docs, single-setpoint head support, and entry-scoped restore.
- [@oliverbrandmueller](https://github.com/oliverbrandmueller) asked for metric units ([#2](https://github.com/dkpnw/ha-mxz-coordinator/issues/2)). Since 2.8.0 the coordinator runs in your Home Assistant temperature unit from end to end.
- **mw1029** on the [Home Assistant forum](https://community.home-assistant.io/t/1018382) tried MXZ with the cloud-based `comfort_HA` integration and posted both entity dumps, which showed exactly where the two don't line up. Support for it is in progress.
- [@csingley](https://github.com/csingley) (Chris Singley):
  - **Own-remote rooms (3.6.0).** He noticed that a room left to its own remote is invisible to the heat-versus-cool standoff, even while it is really heating or cooling on the shared outdoor unit, and sent a fix with a test ([#27](https://github.com/dkpnw/ha-mxz-coordinator/pull/27)). He followed it with [#28](https://github.com/dkpnw/ha-mxz-coordinator/pull/28), which lets a disabled room's head stay under its own remote. The per-room **Let its own remote run it while disabled** setting is built from both, with him as co-author, and he tested it on his own 4-zone MXZ-4C36NA2 before release.
  - **Safety priority (3.7.0).** He designed and wrote the safety-priority arbitration ([#31](https://github.com/dkpnw/ha-mxz-coordinator/pull/31)): every room's heat or cool want ranked in one priority order, with an optional per-room **Safety priority** sensor that lifts a room's own want above every other room's. It shipped not yet tested on hardware. The 3.7.0 freeze floor and overheat ceiling build on his safety tier: a tripped floor or ceiling is one more want in that same priority order, with the freeze floor ranked first and a ceiling settled against a safety room by room order.
  - **Monotonic mode-change wait (3.7.1).** He wrote [#32](https://github.com/dkpnw/ha-mxz-coordinator/pull/32), which times the mode-change wait with a monotonic clock so a wall-clock jump (an NTP correction, a clock set by hand) can't shorten or stretch it.
  - **Uneven drift (3.8.0) and dual setpoint (3.10.0).** His native dual-setpoint proposal ([#35](https://github.com/dkpnw/ha-mxz-coordinator/pull/35)) was declined at first to keep one target, but the discussion led to **Let it get cooler by** and **Let it get warmer by**, which give heat and cool different tolerances around that one target. They shipped in 3.8.0. [#35](https://github.com/dkpnw/ha-mxz-coordinator/pull/35) is also the origin of the opt-in per-room **dual setpoint** (separate Heat to and Cool to): it carries forward his idea and Home Assistant's range contract (the range feature, `heat_cool`, and writes through the room's own numbers), rebuilt as an opt-in on the room's page. It shipped in 3.10.0, with only the heat side tested on hardware so far.
  - **Lost-head votes (3.11.0).** The stale, then expired, vote of a head that goes unavailable (see [When a head goes unavailable](docs/FEATURES.md#when-a-head-goes-unavailable)) adapts the observed-vote model from his [ha-climate-zone-sequencer](https://github.com/csingley/ha-climate-zone-sequencer) (commit [f911e4a](https://github.com/csingley/ha-climate-zone-sequencer/commit/f911e4a1cb70ea247205491719bb2295e2a8ebbd); MIT License; see [NOTICE](NOTICE)). It was tested on hardware with an enabled room but not with a room run from its own remote.

**Prior art and references**

- [BarrettPalmer/Smart-HVAC-Automation-for-Home-Assistant-Mini-Splits](https://github.com/BarrettPalmer/Smart-HVAC-Automation-for-Home-Assistant-Mini-Splits)
- [bjrnptrsn/climate_group_helper](https://github.com/bjrnptrsn/climate_group_helper)
- [bartmachielsen/smart_climate](https://github.com/bartmachielsen/smart_climate)
- The [Mitsubishi Electric FH operating instructions](https://library.mitsubishielectric.co.uk/pdf/download_full/56), the [kumo cloud® 2.22 technician manual](https://www.mitsubishitechinfo.ca/sites/default/files/TM_Kumo_Cloud_2.22_ver.13_FINAL-2-20250917.pdf), and the [OCH573E service manual](https://www.mitsubishitechinfo.ca/sites/default/files/SH_MXZ-%284%29%285%29%288%29C%2836%29%2842%29%2848%29%2860%29NA%28HZ%29_PAC-MKA%2830%29%2831%29%2850%29%2851%29BC_OCH573E_1.pdf), for the scoped AUTO, changeover, and LEV references above.

## License

[MIT](LICENSE).

---

*Not affiliated with, endorsed by, or associated with Mitsubishi Electric Corporation.
"Mitsubishi Electric" and the three-diamond logo are trademarks of their respective owner,
used here for identification/compatibility only.*
