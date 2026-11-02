# Configuration

Open **Settings → Devices & services → MXZ Coordinator → Configure**. Configure opens on
a menu of six pages. Some settings sit in a collapsed section on their page. Heads,
rooms, and sensors are changed in [Reconfigure](SETUP.md#reconfigure-heads-rooms-and-sensors)
instead.

- [Every setting and its default](#every-setting-and-its-default)
- [How saving works](#how-saving-works)
- [Saving rules, warnings, and clearing fields](#saving-rules-warnings-and-clearing-fields)
- [Choose a sensor freshness contract](#choose-a-sensor-freshness-contract)

## Every setting and its default

Defaults follow your Home Assistant temperature unit. Blank optional settings store
nothing and leave today's behavior unchanged.

| Page | Setting | Default (°F / °C) | What it does |
| --- | --- | --- | --- |
| **Comfort** | Allowed drift | 1 / 0.5 (range 0.5–5 / 0.25–2.5) | How far a satisfied room drifts before it runs again. A room's own drift number can override it. |
| | Mode switch threshold | 3 / 1.5 | How far off target a room must be to vote to flip the shared mode. |
| | Resting mode | Last used | Last used, Cool, or Heat: the shared mode when no room is calling. |
| | *Advanced:* Minimum time between mode switches | 600 seconds | Minimum dwell between shared heat/cool changes. |
| | *Advanced:* Let it get cooler by / Let it get warmer by | blank (range 0.5–5 / 0.25–2.5) | [Uneven drift](FEATURES.md#uneven-drift): how far below target before heating, and above before cooling. |
| | *Advanced:* Keep a lost head's vote for | blank = 30 minutes (range 1–240) | [When a head goes unavailable](FEATURES.md#when-a-head-goes-unavailable). |
| **Fan and idle** | Fan boost | on | The automatic fan ladder: faster when far off target, slower near it. A manual hold takes precedence. See [Who drives the fan](FEATURES.md#who-drives-the-fan). |
| | Fan boost top speed | `high` | The ladder's ceiling. |
| | When a room reaches target | Dry the coil (fan only), then off (upgraded entries that never chose keep Fan only) | Fan only, Off, or Dry the coil (fan only), then off (called Off after drying before 3.15.0; not the heads' Dry mode). Choices depend on common head support. See [How a satisfied head idles](FEATURES.md#how-a-satisfied-head-idles). |
| | Coil drying time | 10 minutes | How long Dry the coil (fan only), then off runs the fan after cooling, or after a head leaves its own Dry mode in a room with Allow Dry mode on. |
| **Seasons** | Weather source | none | A weather entity's daily forecast, or an outdoor temperature sensor, that drives the seasonal lockouts. |
| | Lock out heat when the high is at least | 68 / 20 | |
| | Lock out cool when the high is at most | 50 / 10 | |
| | *Safety overrides:* Heat anyway below | 58 / 14 | A locked-out heat still runs for a room below this. |
| | *Safety overrides:* Cool anyway above | 80 / 27 | A locked-out cool still runs for a room above this. |
| **Away and setpoint limits** | Away low limit | 50 / 10 | With eco on, or under an `eco` standby hold, rooms heat only below this. Keep the equipment's own safeguards. |
| | Away high limit | 78 / 26 | With eco on, or under an `eco` standby hold, rooms cool only above this. |
| | *Head setpoint range:* Lowest setpoint / Highest setpoint | 59 / 15 and 88 / 31 | The head's allowed range. Commands are also bounded by usable head limits. |
| **Standby hold** | Hold signal | none | An external signal that holds enabled coordination. Missing, `unknown`, or `unavailable` input releases the hold. |
| | Active state | `on` | The signal state that means "hold". Many grid sensors read `off` when the grid is down. |
| | During a hold | `eco` | `eco`, `off`, or `fan_only`. |
| **Rooms** (one page per room, in priority order, titled by the room's name) | Head type | Local | Local, or Cloud (beta) for a head behind a cloud integration. Only setup suggests Cloud (beta), for a `melcloud`, `kumo_cloud` or `kumo` head; this page shows Local until you choose Cloud (beta) here. An ESPHome head is always local. See [Cloud heads (beta)](../README.md#cloud-heads-beta). |
| | Airflow sensor, Vertical vane, Horizontal vane | detected at setup | Correct detection, select another entity, or clear a field to remove that optional wiring. |
| | Safety priority sensor | none | See [Safety priority](FEATURES.md#safety-priority). |
| | Let its own remote run it while disabled | off | See [Run one room from its own remote](FEATURES.md#run-one-room-from-its-own-remote). |
| | While its own remote runs it, its vote counts | Whenever it is set to heat or cool | Or **Only while it is heating or cooling** (plus 10 minutes after it stops). Stored as `hands_off_vote_basis: action` only when chosen. See [Count its vote only while it runs](FEATURES.md#count-its-vote-only-while-it-runs). |
| | Allow Dry mode | off | Shown only for a head that lists `dry`. On: while the room is enabled, a head someone puts in Dry stays there and votes cool. Stored as `allow_dry: true` only when on; a room page that doesn't show it leaves what is stored alone. See [Allow Dry mode](FEATURES.md#allow-dry-mode). |
| | *Uneven drift:* Let it get cooler by / Let it get warmer by | blank | This room's own sides; see [Per-room uneven drift](FEATURES.md#per-room-uneven-drift). |
| | *Sensor freshness:* Report interval, Maximum age, Startup grace, Freshness evidence, Sample time attribute, Sample counter attribute | new room: Maximum age 360 min and Every update is a new reading, the rest empty; room from before 3.16.0: empty | See [Choose a sensor freshness contract](#choose-a-sensor-freshness-contract). |
| | *Freeze floor and overheat ceiling:* Freeze floor / Overheat ceiling | new room: floor 45 °F / 7 °C, ceiling empty; room from before 3.16.0: empty (range 35–65 / 2–18 and 75–100 / 24–38) | See [Freeze floor and overheat ceiling](FEATURES.md#freeze-floor-and-overheat-ceiling). |
| | *Dual setpoint:* Use separate heat and cool setpoints | off | See [Dual setpoint](FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint). |
| | *Cloud head (beta):* Setpoint drive offset | 2 / 1 (range 0–6 / 0–3) | While the room runs, its cloud head gets the target plus this in heat, minus it in cool. 0 sends the bare target. Left at the default, it follows your unit. A value you set is kept as that number: switching Home Assistant between °F and °C doesn't convert it (in °C it is held to 3), so check it after a switch. |
| | *Cloud head (beta):* Write interval | 60 seconds (range 30–300) | At most one write to the cloud head per this long; a newer change replaces the waiting one. |
| | *Cloud head (beta):* Confirm wait | 90 seconds (range 30–600) | After a write, how long MXZ waits for the head to report it before sending again. |

The outdoor-unit name and **Notify on drift** service are set in setup and Reconfigure.

## How saving works

- Submitting a page saves that page and shows **Options successfully saved.**; **Finish**
  closes the dialog. Open Configure again for the next page.
- A save merges the page's values into the existing options, mirrors them into entry
  data, and reloads once. It never touches a setting on another page.
- A save that leaves entry data and options unchanged needs no reload.
- A room page saved with only **Dual setpoint** turned on or off is applied without a
  reload; see
  [Dual setpoint](FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint).
- Units are shown in each number box: temperatures use your HA unit, the mode switch time
  is in seconds, and coil drying and freshness durations are in minutes.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-menu-dark.png" alt="Configure's Settings menu listing six pages, each with a subtitle: Comfort, Fan and idle, Seasons, Away and setpoint limits, Standby hold, and Rooms."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-comfort-advanced.png" alt="Comfort page with Allowed drift 1 °F, Mode switch threshold 3 °F, and Resting mode Last used (default), and the Advanced section open: Minimum time between mode switches 600 s, Let it get cooler by and Let it get warmer by blank, and Keep a lost head's vote for blank (minutes), then Submit."></td>
</tr>
<tr>
<td>The menu: one page per group of settings.</td>
<td>Comfort, with its <b>Advanced</b> section opened. <b>Keep a lost head's vote for</b> is blank, which means 30 minutes.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-needs-attention.png" alt="Settings menu headed by Needs attention: the saved lowest setpoint is above the highest setpoint. Open Away and setpoint limits to fix it. The six pages follow."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-room.png" alt="Room page titled Studio: empty Airflow sensor, Vertical vane, Horizontal vane, and Safety priority sensor pickers, Let its own remote run it while disabled off, and collapsed Uneven drift, Sensor freshness, Freeze floor and overheat ceiling, and Dual setpoint sections, then Submit."></td>
</tr>
<tr>
<td><b>Needs attention</b> names an older saved value and the page that fixes it.</td>
<td>A room page, titled by the room's name, with its four sections collapsed.</td>
</tr>
</table>

<details>
<summary>Screenshots of the other pages</summary>

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-fan-idle.png" alt="Fan and idle page: Fan boost on, Fan boost top speed High, When a room reaches target Off after drying (default; renamed Dry the coil (fan only), then off in 3.15.0), and Coil drying time 10 min."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-seasons.png" alt="Seasons page: no weather source, Lock out heat when the high is at least 68 °F, Lock out cool when the high is at most 50 °F, and Safety overrides open with Heat anyway below 58 °F and Cool anyway above 80 °F."></td>
</tr>
<tr>
<td>Fan and idle, with the defaults.</td>
<td>Seasons, with <b>Safety overrides</b> opened.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-limits.png" alt="Away and setpoint limits page: Away low limit 50 °F and Away high limit 78 °F, and Head setpoint range open with Lowest setpoint 59 °F and Highest setpoint 88 °F."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-standby.png" alt="Standby hold page: no hold signal, Active state on, and During a hold Eco, protection only (recommended)."></td>
</tr>
<tr>
<td>Away and setpoint limits, with <b>Head setpoint range</b> opened.</td>
<td>Standby hold, with no hold signal.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-rooms-menu.png" alt="Rooms menu: Pick a room. Rooms are listed in priority order. Studio (priority 1, climate.studio_head), Loft (priority 2, climate.loft_head), Den (priority 3, climate.den_head), then Back."></td>
<td></td>
</tr>
<tr>
<td>Rooms lists each room in priority order with its head.</td>
<td></td>
</tr>
</table>

</details>

## Saving rules, warnings, and clearing fields

**Numbers.** All numeric comfort temperatures, thresholds, and durations must be finite
numbers: `nan` and `±inf` are rejected.

- Mode switch threshold, Allowed drift, Minimum time between mode switches, and Coil
  drying time cannot be negative.
- Zero is allowed for the threshold and the two durations.
- Allowed drift also has its selector's 0.5–5 °F / 0.25–2.5 °C bounds, and so do Let it
  get cooler by and Let it get warmer by. Those two may be left blank; emptying one
  removes its saved value.
- Negative Celsius temperatures are legitimate in temperature fields.

**Pairs.** Each pair sits on one page:

| Labels (in HA's temperature unit) | Page | Accepted order |
| --- | --- | --- |
| Away low limit / Away high limit | Away and setpoint limits | Low ≤ high; equality is allowed. |
| Lowest setpoint / Highest setpoint | Away and setpoint limits → Head setpoint range | Lowest ≤ highest; equality is allowed. |
| Heat anyway below / Cool anyway above | Seasons → Safety overrides | Heat ≤ cool; equality is allowed. |
| Lock out cool when the high is at most / Lock out heat when the high is at least | Seasons | Cool threshold < heat threshold; equality is rejected to retain a shoulder band. |

**When a page refuses a save**

- A page checks only the values it shows.
- A refused page saves none of its edits, keeps what you typed, and does not reload the
  entry.
- An error on a field inside a collapsed section is shown above that section, which
  opens: Home Assistant does not show field errors inside a section.
- An invalid freshness profile is one error for the room page: “Nothing was saved.”

<details>
<summary>Screenshots of Configure errors and a successful save</summary>

<table>
<tr>
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-error-setpoint-range.png" alt="Away and setpoint limits page with Lowest setpoint 80 °F and Highest setpoint 70 °F, and the error The lowest setpoint can't be above the highest setpoint. shown above the open Head setpoint range section."></td>
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-error-freshness.png" alt="Studio's room page with Sensor freshness open, Report interval 10 min and Maximum age 5 min, and an error at the top: these freshness settings don't fit together, ending Nothing was saved."></td>
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-configure-saved.png" alt="Success dialog reading Options successfully saved. with a Finish button."></td>
</tr>
<tr>
<td>A lowest setpoint above the highest: the error shows above the <b>Head setpoint range</b> section.</td>
<td>A maximum age shorter than the report interval rejects that room's page.</td>
<td>A valid page saves; <b>Finish</b> closes the dialog.</td>
</tr>
</table>

</details>

**Older saved values: Needs attention.** Values saved by 3.3.0 or earlier are not
silently rewritten on upgrade. A stored value that a page would now refuse does not block
the other pages. Instead, the menu shows a **Needs attention** line naming each such value
and the page that fixes it:

- an idle action the heads no longer support,
- an inverted pair, or
- a room's freshness profile that doesn't fit together.

The line is empty when nothing needs fixing. Open the named page, correct the value, and
save.

**When heads block every save.** Configure shows an error instead of its menu when:

- a head has no HA state or unknown `hvac_modes`,
- a head is missing either `heat` or `cool`, or
- the heads have no common idle mode.

A page opened just before the head changed shows the same error. Restore the head
integration's capability reports, or use Reconfigure to select compatible heads, then
reopen Configure. An `unavailable` head that still reports the required capabilities can
pass this check; availability alone does not determine it.

**Clearing optional fields**

- On a room page, a cleared vane or airflow field removes that wiring from that room only.
- On the Standby hold page, a cleared Hold signal explicitly removes the hold. On the
  Seasons page, a cleared Weather source stops the lockouts following the forecast. No
  other page can clear either.
- Do not infer that every optional field has the same clearing behavior.

**Recovered values.** The data mirror can recover tuned values when options are empty, but
does not prove how they became empty. Review and save Configure if that warning appears.

## Choose a sensor freshness contract

Each room's profile is on its page (Configure → Rooms → the room → **Sensor freshness**).

**New rooms start with a 6-hour limit** (3.16.0): setup and Reconfigure store Maximum age
360 min and **Every update is a new reading** in each room they create; startup grace
then defaults to the same 6 h. A room from before 3.16.0 keeps its profile, or none. A
sensor that writes to Home Assistant only when its reading changes (a template sensor,
some MQTT setups, an ESPHome delta filter) can go stale in a room that holds steady for
6 h. Its room then stops calling and its freeze floor pauses, with a notice. If that
happens, clear **Maximum age** (or the whole profile, with evidence **Not set**), or use
a sensor that reports periodically. Zigbee, Z-Wave and ESPHome sensors with a periodic
report stay fresh, because each report counts even when the value is unchanged.

**No contract? Leave it empty.** Leave the profile empty if you cannot establish a
reporting contract. MXZ shows `cadence_unknown`, displays age, and applies no age cutoff.
Giving only an interval, or keeping `evidence_basis: unknown`, does not make a sensor
trustworthy.

**To enable age checking**, supply a positive **Report interval** or **Maximum age**, then
choose a **Freshness evidence** basis.

- With only an interval, the maximum is three intervals.
- An explicit maximum must be at least the interval.
- Startup grace defaults to that maximum.

| Evidence basis | Choose it when |
| --- | --- |
| Every update is a new reading (`ha_state_write`) | The source guarantees each HA write is a current reading, even when its numeric value is unchanged. Cached/restored replays can defeat this basis. |
| Sensor reports a sample time or counter (`sample_timestamp`) | The source supplies a trustworthy advancing sample marker. Enter exactly one timestamp or sequence attribute. Timestamps need a timezone and cannot be in the future; sequences must increase. |
| Not set (`unknown`) | No reliable reporting contract exists. Clear durations and marker fields to remove the profile. |

**When a reading goes bad**

- An invalid reading steps aside immediately.
- A valid but stale room leaves automatic demand and parks by the configured idle action
  (`off` under eco), while its recognized manual fan hold stays intact.
- Its tile keeps the valid reading; age/health in the plan explain why it is no longer
  calling.
- A qualifying fresh report recovers the room. A cached marker does not extend a deadline
  or recover an unhealthy room.

**After a reload or restart**

- Reload/restart does not persist health.
- A valid recent sample timestamp can start healthy.
- Other valid profiled readings start `awaiting_report` for one grace period; a sequence
  found at load is only a baseline.
- An unusable reading gets no grace.

See [Migration](MIGRATION.md#stale-room-sensors-340) for deadline and recovery edges.
