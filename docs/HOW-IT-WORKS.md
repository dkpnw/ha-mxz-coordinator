# How it works

Why MXZ exists, how it decides, and where its control stops. The
[README](../README.md) has the short version.

- [The problem in detail](#the-problem-in-detail)
- [How MXZ decides](#how-mxz-decides)
- [Best practice: give the firmware your room sensor too](#best-practice-give-the-firmware-your-room-sensor-too)
- [Service-manual note: a parked room warming in heating season](#service-manual-note-a-parked-room-warming-in-heating-season)
- [Limits of control](#limits-of-control)

## The problem in detail

**What the manual says.** For the cited
[MSZ-FH25VE/FH35VE/FH50VE indoor units, PDF page 7](https://library.mitsubishielectric.co.uk/pdf/download_full/56),
COOL/DRY/FAN and HEAT cannot run at the same time when several indoor units share one
outdoor unit: "the unit selected last goes into standby mode." AUTO selects from that
indoor unit's room temperature and setpoint, and changes mode only after a sustained
difference. The manual does not recommend AUTO on an MXZ outdoor unit and warns that a
unit may be unable to switch between COOL and HEAT and enter standby.

By default that is first come, first served, not one room governing the system. Some
outdoor units can be set up otherwise. For the MXZ-4C36NAHZ to 8C60NA families in
[OCH573E, PDF page 127](https://www.mitsubishitechinfo.ca/sites/default/files/SH_MXZ-%284%29%285%29%288%29C%2836%29%2842%29%2848%29%2860%29NA%28HZ%29_PAC-MKA%2830%29%2831%29%2850%29%2851%29BC_OCH573E_1.pdf),
outdoor switch SW9-1 (off by default, set before power-on) "enables the indoor unit with
the minimum address to select AUTO mode, and switches the operation mode of the other
indoor units to the same mode." The same manual's
[page 108](https://www.mitsubishitechinfo.ca/sites/default/files/SH_MXZ-%284%29%285%29%288%29C%2836%29%2842%29%2848%29%2860%29NA%28HZ%29_PAC-MKA%2830%29%2831%29%2850%29%2851%29BC_OCH573E_1.pdf)
shows a CN3N external input that sets heating or cooling through an adapter
(PAC-SC36NA-E). Whether either is set on your system is an installer question; the
manuals do not say how other models behave.

**What it looks like in practice.** On the maintainer's two-head system, a room 6 °F
past its cooling target drew **~26 W for over an hour** while the other head was
satisfied. After that head was turned off, the room ramped to ~460 W and cooled. This is
a household observation, not a controlled comparison or a measured energy, comfort,
noise, or equipment-life result.

**The same scenario, coordinated:** both rooms served within minutes instead of an hour of
starvation.

```
elapsed    draw       what's happening
0:20       675 W      hot room cooling hard; satisfied room idle in fan_only
1:20–3:40   45 W      compressor anti-short-cycle pause (~2.5 min — normal)
4:00–6:20  101→609 W  both rooms served, sustained
```

**Mitsubishi's own alternative: kumo cloud changeover groups.** Basic AUTO is not the
only manufacturer option. The
[kumo cloud® 2.22 technician manual, pages 15–17](https://www.mitsubishitechinfo.ca/sites/default/files/TM_Kumo_Cloud_2.22_ver.13_FINAL-2-20250917.pdf)
documents optional changeover groups for some MXZ systems, including zone priorities,
maximum standby, and minimum run time. That is a relevant alternative where its
supported hardware and setup apply. It is not tested for parity with this integration.

**Mitsubishi's standby lamp** means a head set, by its remote or stock AUTO, to the mode
the system is not running. Under MXZ a waiting head is parked by command instead.

## How MXZ decides

On every update (a room sensor or head report, a setting change, and a 15-minute check)
MXZ works out one plan for the outdoor unit:

1. **Each enabled room reads its own sensor.** A room with an invalid reading, or a stale
   one under its [freshness contract](CONFIGURATION.md#choose-a-sensor-freshness-contract),
   makes no automatic demand.
2. **Each room asks for a mode only when it is well off target.** Past the **Mode switch
   threshold** (default 3 °F / 1.5 °C), or that side's drift if wider, a room votes for
   heat or cool. Closer than that, it reads `neutral` and waits.
3. **One shared mode is chosen.** Wants are ranked: a tripped
   [freeze floor](FEATURES.md#freeze-floor-and-overheat-ceiling) first; then, by room
   priority, rooms whose [Safety priority](FEATURES.md#safety-priority) sensor is `on`
   and tripped overheat ceilings; then enabled rooms in priority order (position 1 wins a
   standoff). A room [run from its own remote](FEATURES.md#run-one-room-from-its-own-remote)
   counts only when no enabled room is calling or still running to its target, unless its
   Safety priority sensor is `on`. A reversal
   waits out **Minimum time between mode switches** (default 600 seconds). With no room
   calling, **Resting mode** picks the mode.
4. **Rooms in that mode run; the rest step aside.** A room runs once it drifts past its
   band (**Allowed drift**, default 1 °F / 0.5 °C, or its own
   [drift](FEATURES.md#drift-when-a-room-starts-a-run)) and keeps running until it reaches
   its target. A satisfied room, and a room waiting for the other mode, parks by
   [the idle action](FEATURES.md#how-a-satisfied-head-idles).
5. **Your gates bound all of it.** Coordinator enable, room enables, seasonal lockouts,
   eco/away limits, and a standby hold limit what is commanded.
6. **Commands go out.** Mode, a bounded setpoint, and, under fan boost, a fan speed for
   each head. MXZ never requests hardware AUTO, `heat_cool`, or `dry` on a head, and puts
   an enabled room's head back if someone sets one.

The plan, with each room's reasons, is on `sensor.*_plan`; see
[Diagnose a room that is waiting](TROUBLESHOOTING.md#diagnose-a-room-that-is-waiting).

**Setpoints sent to the heads.**

- **Range heads** (`target_temp_low`/`high`) get a band: target − 2 °F to the target when
  cooling, the target to target + 2 °F when heating (a 1 °C band on a °C system).
  **Single-setpoint heads**, common on MXZ indoor units, get the target itself.
- Setpoints are rounded to 1 °F or 0.5 °C and kept within Configure's Lowest/Highest
  setpoint and the head's own limits.
- A head that only holds 0.5 °C steps (ESPHome CN105) in a °F system shows some whole-°F
  setpoints 0.5 °F off, such as 64.5 for 64. Since 3.4.1 MXZ compares on the head's own
  steps and does not re-send. See
  [Setpoints on °C-native heads](MIGRATION.md#setpoints-on-c-native-heads-in-a-f-system-341).
- While eco/away is on, heads get fixed eco setpoints instead of the room target.
- A parked head (`fan_only` or `off`) is sent a mode only, so it keeps its last setpoint.

## Best practice: give the firmware your room sensor too

The coordinator reads your room sensors, but each head's own control loop still runs on
its internal thermistor. On the system MXZ was developed on, that reading was several
degrees warmer than the occupied room while idle; other models and placements can
differ.

Feed the SAME room sensor to the firmware so both layers use one reading. On CN105/ESPHome
that is a `homeassistant` sensor bound via `remote_temperature_source`:

```yaml
sensor:
  - platform: homeassistant
    id: remote_temp_ha
    entity_id: sensor.your_room_temperature   # the same sensor you give the coordinator
    filters:
      - lambda: return (x - 32) * (5.0/9.0);  # only if your HA runs °F
      - clamp:                                # the firmware accepts 1–40 °C
          min_value: 1
          max_value: 40
          ignore_out_of_range: true

climate:
  - platform: cn105
    # ...
    remote_temperature_source:
      sensor_id: remote_temp_ha
    remote_temperature_timeout: 30min
    remote_temperature_keepalive_interval: 20s
```

The timeout is the safety: if the sensor drops out, the head falls back to its internal
reading instead of holding a stale number.

## Service-manual note: a parked room warming in heating season

For the MXZ-4C36NAHZ, 5C42NAHZ, 8C48NAHZ, 8C48NA, and 8C60NA families covered by
[OCH573E, PDF page 126](https://www.mitsubishitechinfo.ca/sites/default/files/SH_MXZ-%284%29%285%29%288%29C%2836%29%2842%29%2848%29%2860%29NA%28HZ%29_PAC-MKA%2830%29%2831%29%2850%29%2851%29BC_OCH573E_1.pdf),
Mitsubishi documents SW5-8 as a countermeasure against room-temperature rise in parked
indoor units. Enabling it before power-on fully closes the indoor electronic expansion
valve in FAN, COOL, STOP, or thermo-OFF. The same row warns that refrigerant can collect
in thermo-OFF units, reducing capacity and raising discharge temperature.

This supports a model-specific explanation for why a parked room can warm in heating
season. It does not show whether SW5-8 is enabled, prove the actual valve position, or
measure the temperature effect in your installation. The idle action commands airflow and
power; `off` is not a refrigerant-seal setting.

## Limits of control

**If Home Assistant goes down**, heads use their own loops. A head last commanded cooling
can continue cooling; one last parked stays parked. Their thermistors and firmware
safeguards still matter. On the system MXZ was developed on, compressor pauses and
reversal lag outlast the HA mode report.

**Failures and what they reach**

- A global update failure can make the plan and coordinator-listening entities
  unavailable, even while heads remain registered.
- Room isolation covers independent head delivery failures; it is not a guarantee against
  every coordinator, core, or transport failure.
- Reload/unload suppresses retired follow-on work, but cannot undo an accepted external
  call.
- A vane kick on a parked head briefly wakes it, sends the requested vane change, and
  requests parking; software cleanup still needs hardware confirmation.

**Diagnostics and your automations.** The thermostat, Fan auto switch, and plan's `zones`
include added diagnostics listed in
[Entity map](ENTITY-MAP.md#integration-diagnostic-surfaces-340).

- Delivery changes publish immediately, even outside refresh completion.
- A head update the room thermostat does not display, such as the head's own temperature
  reading, adds no thermostat `state_changed` event.
- Delivery progress, fan state, and reason changes still update these attributes, which
  can add recorder history/storage and fire automations with a bare state trigger. Use an
  explicit state transition or selected attribute when that is your automation's intent.
- Exact database rows, bytes, and hourly growth have not been measured.

**How refreshes are coalesced.** Sensor refreshes already covered by a newer decision are
coalesced by generation. Pending delivery can prompt a follow-up outside the ordinary
debounce; several requests can collapse into that follow-up. There is no
one-compute-per-sensor-write guarantee.
