# Setup, Reconfigure, and removal

The [README](../README.md#install) has the short install. This page has every step,
screenshot, and error.

- [Requirements](#requirements)
- [Install](#install)
- [Set up your outdoor unit](#set-up-your-outdoor-unit) ·
  [If setup stops or warns](#if-setup-stops-or-warns)
- [Reconfigure heads, rooms, and sensors](#reconfigure-heads-rooms-and-sensors)
- [Remove the integration](#remove-the-integration)

**About the screenshots.** Every screenshot shows MXZ 3.12.0 on Home Assistant 2026.10.0,
with invented heads (Studio, Loft, Den, Nook) and invented sensors on a test instance.
The setup, Reconfigure, and Configure screenshots use Home Assistant's dark theme; the
README's dashboard and Configure menu follow your GitHub theme.

## Requirements

| You need | Details |
| --- | --- |
| Home Assistant | **2024.12.0 or newer**. |
| Heads | **2–8 heads on one outdoor unit.** Add a separate coordinator entry for each outdoor unit. |
| Head modes | Each selected `climate` entity must advertise `heat`, `cool`, and a common idle action. |
| Idle action support | `fan_only` needs `fan_only`; `off` needs `off`; `off_after_dry` needs both. |
| Fan control | The advertised fan-mode feature. **Fan auto** requires the exact `auto` token. |
| Room sensors | A separate temperature `sensor` entity for each room. The picker uses device class **temperature**. |

A head's own temperature sensor can be selected. On the system MXZ was developed on, it
read warmer than the occupied room while idle; placement and firmware can differ.

**What it's built for and tried on.** MXZ is built against
[echavet/MitsubishiCN105ESPHome](https://github.com/echavet/MitsubishiCN105ESPHome).
Earlier releases have field experience on two- and three-zone hardware, with six-zone and
multiple-outdoor-unit reports. Other climate integrations, including Kumo Cloud and
MELCloud, remain unvalidated here. Compatible advertised features alone do not prove
equivalent timing or hardware behavior.

**Out of scope:** simultaneous heat/cool and branch-box VRF.

## Install

1. Open **HACS**, search for **MXZ Coordinator**, and select **Download**. You can also
   [open it in HACS directly](https://my.home-assistant.io/redirect/hacs_repository/?owner=dkpnw&repository=ha-mxz-coordinator&category=integration).
2. Restart Home Assistant to load the integration.
3. Open **Settings → Devices & services → Add integration → MXZ Coordinator**.
4. Complete the setup below. Installation alone does not enable control.

**Without HACS:** copy the release's `custom_components/mxz_coordinator` directory into
Home Assistant's `custom_components` directory, restart, then add the integration.

**Coming from the YAML package?** The shipped
[YAML package](../packages/mxz_coordinator.yaml) is a separate legacy option. To move from
it, follow [Migration](MIGRATION.md#moving-from-the-yaml-package). Stop its automations
before letting the integration control the same heads.

## Set up your outdoor unit

Setup has four steps. Only **Save and finish** creates the entry.

1. **Heads.** Select every head on this outdoor unit once, in priority order. Position 1
   wins a heat-versus-cool standoff. Give the outdoor unit an optional name and choose an
   optional drift-alert notification service (**Notify on drift**). The outdoor-unit name
   labels the entry; it does not form entity IDs.
2. **Rooms.** Check each pre-filled **Room N name**. Use distinct, non-empty names;
   comparison ignores case. These names label the room entities.
3. **Room temperature sensors.** Pick one sensor per room. Check the room/head mapping
   and the displayed reading. Vane and airflow entities are detected from the head's
   device when matching registry records exist. Missing records or no matching device
   leave those optional controls unset; edit them later in Configure.
4. **Check this before you save.** Read each priority, head, and sensor line, then
   choose:
   - **Save and finish** when it is right.
   - **Change advanced settings** to tune comfort on the same pages Configure uses, then
     **Back to summary**.
   - **Go back to heads and rooms** to correct the mapping.

The summary's reporting age is information only: setup adds no sensor timeout. Heads,
ownership, sensors, and the chosen idle action are checked again at the final save, so a
change while the summary is open can send you back to correct it.

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-heads.png" alt="Setup heads step with Studio, Loft, and Den heads in priority order, the outdoor unit named Upstairs outdoor unit, and an empty Notify on drift picker."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-rooms.png" alt="Rooms step listing the three heads by priority, with Room 1 to 3 names pre-filled as Studio head, Loft head, and Den head."></td>
</tr>
<tr>
<td>1. Heads: three heads in priority order and an outdoor-unit name.</td>
<td>2. Rooms: each name is pre-filled from its head's name and can be edited.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-sensors.png" alt="Room temperature sensors step listing Studio, Loft, and Den with their heads, and Studio, Loft, and Den temperature picked."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-summary.png" alt="Check this before you save: the outdoor unit and drift alerts, then each room's priority, head, sensor with its reading and age, and unknown cadence; default comfort settings for °F with Idle off_after_dry; then Save and finish, Change advanced settings, and Go back to heads and rooms."></td>
</tr>
<tr>
<td>3. Sensors: each room with its head, and one temperature sensor picked per room, before submit.</td>
<td>4. Summary: each room's priority, head, sensor, reading, and cadence, then the three choices.</td>
</tr>
</table>

**Advanced settings are optional.**

- Skip them and MXZ uses defaults for your HA temperature unit.
- **Change advanced settings** opens a menu of the Comfort, Fan and idle, Seasons, Away
  and setpoint limits, and Standby hold pages, pre-filled with those defaults.
- Submitting a page keeps its answers and returns to that menu. Nothing is saved until
  **Save and finish**.
- If the selected heads cannot all use the default Off after drying idle action (it needs
  both `fan_only` and `off`), **Save and finish** opens Fan and idle so you can choose a
  supported alternative. It does not silently replace that default.

<img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-advanced-menu.png" width="420" alt="Advanced settings menu headed All optional. You can change these later with Configure: Comfort, Fan and idle, Seasons, Away and setpoint limits, and Standby hold, each with a subtitle, then Back to summary.">

The advanced settings menu: [Configure](CONFIGURATION.md)'s pages without Rooms, and
**Back to summary** instead.

After **Save and finish**, Home Assistant shows its own device **Name and assign** step.

<img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-complete.png" width="420" alt="Home Assistant's Name and assign step for the new device, Multi-zone mini-split coordinator with the MXZ dial icon: Device name MXZ Coordinator, an empty Area, and Skip and finish.">

**Then turn it on.** New room enables and Coordinator enable start off.

1. Open the integration's device page.
2. Set the room targets.
3. Enable the rooms you want to coordinate.
4. Turn on **Coordinator enable**.

Expose the per-room thermostats to HomeKit/Google, or use them in HA. Avoid exposing a
second raw-head control for the same room.

### If setup stops or warns

| What you see | What to do; what MXZ saves |
| --- | --- |
| A missing head or unknown capabilities | Let the head integration load and publish its modes. Unknown support blocks saving. |
| A head lacks heat/cool, or there is no common idle action | Choose compatible heads. Setup does not invent a mode. |
| A head belongs to another entry | Reconfigure that entry first. Setup names the conflict and saves nothing. |
| A head is reserved by an open flow | Finish or cancel that setup/Reconfigure flow. Reservations have no timeout; HA restart clears an abandoned in-memory flow. |
| Duplicate heads or room names, or an empty room name | Correct the selection/name. Setup keeps you on that step. |
| Two rooms share a sensor, a sensor has no HA state, or its unit cannot convert | Pick a distinct, existing temperature sensor with a supported unit. Saving is blocked. |
| A sensor is `unknown`, `unavailable`, non-numeric, or non-finite | You may finish setup. The summary warns; that room makes no automatic demand until its reading is valid. |
| A sensor's unit attribute is absent or `null` | MXZ reads it in HA's system unit. Verify that interpretation. Celsius, Fahrenheit, and kelvin convert to the system unit. An empty string (`""`), another unsupported string, or a non-string unit is invalid and blocks saving. |

<details>
<summary>Screenshots of setup errors and warnings</summary>

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-capability.png" alt="Heads step with Studio head and Guest fan unit picked, and the error: climate.guest_fan_unit does not advertise both heat and cool."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-already-configured.png" alt="Heads step error: climate.nook_head, climate.loft_head, and climate.studio_head are already assigned to another MXZ Coordinator entry."></td>
</tr>
<tr>
<td>A fan-only guest unit is rejected because it lacks heat and cool.</td>
<td>The heads belong to another entry. The error names them, and the heads step stays open.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-reserved.png" alt="Heads step with Nook head and Studio head picked, and the error: climate.studio_head is selected in another open MXZ Coordinator dialog; finish or cancel that dialog, or restart Home Assistant, then try again."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-duplicate-room.png" alt="Rooms step with Room 1 and Room 2 both named Studio and Room 3 Den, and the error: two rooms have the same name, Studio."></td>
</tr>
<tr>
<td>A head is reserved by another open setup dialog.</td>
<td>Two rooms have the same name.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-shared-sensor.png" alt="Sensors step with Studio temperature picked for both Studio and Den, and the error: sensor.studio_temperature is used for more than one room."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-error-sensor-unit.png" alt="Sensors step with Garage probe picked for Den, and the error: MXZ Coordinator cannot read the temperature unit reported by sensor.garage_probe."></td>
</tr>
<tr>
<td>One sensor picked for two rooms is blocked.</td>
<td>A sensor whose unit cannot be converted is blocked.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-setup-summary-warning-unavailable.png" alt="Summary with Den's sensor sensor.porch_temperature unavailable and a warning that the room makes no automatic demand until it reports a valid number."></td>
<td></td>
</tr>
<tr>
<td>An unavailable sensor is accepted. The summary warns that the room makes no automatic demand until it reads a valid number.</td>
<td></td>
</tr>
</table>

</details>

## Reconfigure heads, rooms, and sensors

Open **Settings → Devices & services → MXZ Coordinator → ⋮ → Reconfigure**. Use it to:

- add or remove heads, or change their priority;
- rename rooms or change their sensors;
- change the outdoor-unit title and the drift-alert service.

It follows heads → rooms → sensors → summary, pre-filled from the entry. Select **Save
and finish** to apply it. Comfort and sensor-freshness settings stay in
[Configure](CONFIGURATION.md).

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-reconfigure-heads.png" alt="Reconfigure heads step with Nook, Loft, and Studio heads in priority order and the outdoor unit still named Upstairs outdoor unit."></td>
<td width="50%"><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-reconfigure-rooms.png" alt="Reconfigure room names step pre-filled with Nook, Loft, and Studio in the new priority order."></td>
</tr>
<tr>
<td>Heads: Nook added at priority 1, Loft kept at 2, Studio moved from 1 to 3, Den dropped.</td>
<td>Room names in the new priority order. An empty box uses the head's own name.</td>
</tr>
<tr>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-reconfigure-sensors.png" alt="Reconfigure sensors step: Loft and Studio keep their sensors and readings; Nook temperature picked for the new room."></td>
<td><img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-reconfigure-summary.png" alt="Reconfigure summary noting Studio moved from priority 1 with its name, sensor, vane wiring, target, drift, enable, and fan hold; climate.den_head removed with its room's entities; and that comfort settings live in Configure."></td>
</tr>
<tr>
<td>Sensors: kept rooms show their current sensors; Nook temperature is picked for the new room.</td>
<td>Summary: Studio's move with its settings, Den's removal, and a pointer to Configure for comfort settings. Only <b>Save and finish</b> and <b>Go back to heads and rooms</b> are offered.</td>
</tr>
</table>

**What carries over**

- For a kept head, submitted room names and sensors replace the old ones. Vane wiring and
  other stored room fields are retained.
- Reordering moves existing registry records with their room. That preserves entity IDs,
  restored target/enable/drift/fan-hold settings, and registry customizations. Their
  slot-derived unique IDs change to the new priority.
- Plan attributes such as `primary_demand` and `zones[0]` follow priority, so check any
  slot-based automation after a reorder.
- Missing or manually removed records cannot be carried.
- A removed room's records are pruned on setup; a newly added room starts disabled.

**Room names**

- Clear a room name to use the head's current name. Duplicate names are still rejected.
- An entity name you set in HA takes precedence over the room label.
- Renaming a head alone does not update a saved room name.
- A room rename does not change its entity ID.

**If the idle action no longer fits.** Reconfigure checks the entry's saved idle action
against the submitted heads. If it is incompatible, change **When a room reaches target**
(Configure → Fan and idle) while the current heads still support that choice, or select
compatible heads and retry.

**Overlapping entries.** Existing overlapping entries may keep or remove heads they
already stored; they cannot expand that overlap. Another open flow can still reserve
those heads. MXZ does not repair or disable the other entry for you.

**Reloads.** A save that changes entry data, title, or identity triggers one reload. A
save that leaves them unchanged triggers none. Reload rebuilds the coordinator and
restarts sensor-health observation. Reconfigure finishes by closing the flow after a
successful save with MXZ's own completion text.

<img src="https://raw.githubusercontent.com/dkpnw/ha-mxz-coordinator/main/images/mxz-3.12-reconfigure-complete.png" width="420" alt="MXZ Coordinator dialog reading Reconfiguration was successful. with a Close button.">

## Remove the integration

1. Delete the entry through **Settings → Devices & services → MXZ Coordinator → ⋮ →
   Delete**.
2. Then remove its download from HACS. Removing files first leaves a broken entry.

The shared recompute service is removed after the last loaded entry is gone. Heads retain
their last commanded state; use their controls if they remain parked.

**Correcting a room?** Prefer Reconfigure to delete/re-add: old HA restore-cache values
can linger, and MXZ filters records from before the new entry's creation.
