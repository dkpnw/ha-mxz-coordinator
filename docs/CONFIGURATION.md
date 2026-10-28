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
| | When a room reaches target | Off after drying (upgraded entries that never chose keep Fan only) | Fan only, Off, or Off after drying. Choices depend on common head support. See [How a satisfied head idles](FEATURES.md#how-a-satisfied-head-idles). |
| | Coil drying time | 10 minutes | How long Off after drying runs the fan after cooling. |
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
| **Rooms** (one page per room, in priority order, titled by the room's name) | Airflow sensor, Vertical vane, Horizontal vane | detected at setup | Correct detection, select another entity, or clear a field to remove that optional wiring. |
| | Safety priority sensor | none | See [Safety priority](FEATURES.md#safety-priority). |
| | Let its own remote run it while disabled | off | See [Run one room from its own remote](FEATURES.md#run-one-room-from-its-own-remote). |
| | *Uneven drift:* Let it get cooler by / Let it get warmer by | blank | This room's own sides; see [Per-room uneven drift](FEATURES.md#per-room-uneven-drift). |
| | *Sensor freshness:* Report interval, Maximum age, Startup grace, Freshness evidence, Sample time attribute, Sample counter attribute | empty | See [Choose a sensor freshness contract](#choose-a-sensor-freshness-contract). |
| | *Freeze floor and overheat ceiling:* Freeze floor / Overheat ceiling | empty (range 35–65 / 2–18 and 75–100 / 24–38) | See [Freeze floor and overheat ceiling](FEATURES.md#freeze-floor-and-overheat-ceiling). |
| | *Dual setpoint:* Use separate heat and cool setpoints | off | See [Dual setpoint](FEATURES.md#give-a-room-separate-heat-and-cool-setpoints-dual-setpoint). |

The outdoor-unit name and **Notify on drift** service are set in setup and Reconfigure.

## How saving works

- Submitting a page saves that page and closes the dialog. Open Configure again for the
  next page.
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
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.1-configure-menu.png" alt="Configure's Settings menu listing six pages with subtitles: Comfort, Fan and idle, Seasons, Away and setpoint limits, Standby hold, and Rooms."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.1-configure-comfort.png" alt="Comfort page with Allowed drift 1 °F, Mode switch threshold 3 °F, and Resting mode Last used (default), and the Advanced section open showing Minimum time between mode switches 600 s, then Submit."></td>
</tr>
<tr>
<td>The menu: one page per group of settings.</td>
<td>Comfort, with its <b>Advanced</b> section opened.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.1-configure-needs-attention.png" alt="Settings menu headed by a Needs attention line: the saved lowest setpoint is above the highest setpoint; open Away and setpoint limits to fix it."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.1-configure-room-studio.png" alt="Room page titled Studio with empty Airflow sensor, Vertical vane, and Horizontal vane pickers, a collapsed Sensor freshness section, and Submit."></td>
</tr>
<tr>
<td><b>Needs attention</b> names an older saved value and the page that fixes it.</td>
<td>A room page, titled by the room's name.</td>
</tr>
</table>

<details>
<summary>Screenshots of the 3.4.0 single form (the menu replaced it)</summary>

These show 3.4.0's one-form Configure. The fields are the same; their labels, grouping,
and order changed in 3.4.1.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-comfort-c1007a.png" alt="3.4.0 Configure form, top: comfort thresholds, setpoint range, resting mode, lockout safety limits, and seasonal changeover fields in °F."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-idle-fan-c1007a.png" alt="3.4.0 Configure form: standby hold, idle action, coil-dry minutes, and fan boost fields."></td>
</tr>
<tr>
<td>3.4.0: comfort, setpoint range, resting mode, lockouts, and seasonal changeover.</td>
<td>3.4.0: standby hold, idle action, coil drying, and fan boost.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-primary-vane-freshness-c1007a.png" alt="3.4.0 Configure form: Primary vertical and horizontal vane, airflow sensor, and six freshness fields."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-secondary-vane-freshness-c1007a.png" alt="3.4.0 Configure form: Secondary vertical and horizontal vane, airflow sensor, and six freshness fields."></td>
</tr>
<tr>
<td>3.4.0: the priority 1 room's vanes, airflow, and freshness fields.</td>
<td>3.4.0: the priority 2 room's fields. Each room now has its own page.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-zone3-vane-freshness-c1007a.png" alt="3.4.0 Configure form: Zone 3 vertical and horizontal vane, airflow sensor, and six freshness fields, then Submit."></td>
<td></td>
</tr>
<tr>
<td>3.4.0: the priority 3 room's fields, then Submit.</td>
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
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-reject-clamp-field-c1007a.png" alt="3.4.0 Configure error on both Firmware minimum setpoint 80 and Firmware maximum setpoint 70: the lowest accepted setpoint cannot be above the highest."></td>
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-reject-freshness-top-c1007a.png" alt="3.4.0 Configure form-level error asking to check every sensor freshness profile, ending Nothing was saved."></td>
<td width="33%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.4.0-configure-saved-c1007a.png" alt="Success dialog reading Options successfully saved. with a Finish button."></td>
</tr>
<tr>
<td>3.4.0: a minimum setpoint above the maximum marked both fields. In 3.4.1 the error shows above the Head setpoint range section.</td>
<td>3.4.0: an invalid freshness profile rejected the form. In 3.4.1 it rejects that room's page.</td>
<td>A valid page saves and closes.</td>
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
