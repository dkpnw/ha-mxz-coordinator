# Upgrading and migrating MXZ Coordinator

## Upgrading to 3.4.0

3.4.0 is the normal minor candidate after 3.3.0. It includes reviewed product changes
for setup/reconfigure validation, room rename/reorder and registry continuity, optional
sensor freshness, Follow global drift, shared-mode choices, and command delivery with
manual-intent and restore corrections. The manifest version is 3.4.0; config-entry
schema version remains 2. The declared HA floor remains 2024.12.0. Final source/version
and exact final release-artifact/rollback qualification are still pending. The README's
setup, Reconfigure and Configure screenshots were rendered on HA 2026.10.0 only.
Earlier invented-entry checks executed actual older-artifact rollback with the limits below;
they do not qualify this final candidate's artifact.
This document is upgrade guidance, not a claim that 3.4.0 has been published.

For an existing integration entry:

1. Preserve a pre-upgrade HA backup that includes configuration, config entries,
   entity registry and restore state. Record the current integration version, HA
   version, exact installed release artifact and its hash. Keep that older artifact.
2. Record room/head/sensor mappings, priority, targets, enables, drift overrides,
   fan holds, shared mode, lockouts, idle action and standby settings. Include
   automation references to priority slots as well as entity IDs.
3. When 3.4.0 is released and validated, update its download in HACS (or replace only
   `custom_components/mxz_coordinator` from that release), then restart HA. Confirm
   the installed manifest reads 3.4.0. Do not delete and recreate the entry to upgrade.
4. Check the entry, registry identities and restored room settings. Inspect the plan
   sensor and underlying heads before relying on control. An existing enabled entry
   may resume control after restart; a fresh entry starts with control disabled.
5. Review Configure. Freshness remains opt-in. Use Reconfigure only when changing the
   room list, names, sensor mapping or priority. Review slot-based automations after
   a reorder. See the behavior changes below before turning control back on.

A v1 flat primary/secondary entry migrates to an ordered v2 `zones` list, retaining
its first two slot identities. A v2 entry needs no schema-version bump for 3.4.0.
Future entry versions above 2 are refused, not silently downgraded. Existing IDs
are retained for kept registry records; removed rooms are pruned and new records
receive new identities. This does not promise behavior is unchanged on upgrade.

### Roll back to an older integration release

A configuration snapshot restored on this same candidate has been exercised with
invented entries, migration, unload/re-setup and registry identity checks. That is
**not** evidence that an older released integration or HA artifact can read the new
state. No older-artifact rollback is qualified by that result.

Separate checks executed the actual v3.3.0 tag source artifact and a local 3.4.0
candidate archive in fresh HA processes, using real invented `.storage` files.
On Python 3.13.16 / HA 2026.2.3, ordinary upgrade, restart/reload and direct old-code
rollback retained the tested targets, enables, lockouts, active drift override and
clean on/off fan hold. That does **not** mean v3.3.0 accepts every newer entry unchanged:

- A Fan auto hold saved as `unavailable` with newer extra restore data stayed held
  under the same 3.4.0 candidate, but became automatic under v3.3.0 on the identical
  snapshot. Code replacement alone lost that hold.
- Loading the old artifact with the compatible complete **pre-upgrade** snapshot
  recovered the earlier hold in that case. Newer changes were absent, as expected.
- The older artifact removes the new Follow global drift buttons. It can retain
  unknown freshness keys in entry data without enforcing the new freshness behavior.
- On Python 3.12.14 / HA 2024.12.0, old code pruned/recreated drift and Fan auto
  records, losing tested names/areas and a drift record's user-disabled flag. It did
  this on its own old-version reload too. The three old-FLOOR checks also failed
  teardown on a lingering debounce timer; their product observations are not blanket
  passing rollback receipts. A snapshot cannot prevent the old implementation from
  repeating its registry loss.
- Returning to 3.3.0 also returns to its older issue 25 behavior; restoring settings
  cannot preserve the newer control correction in old code.

These bounded checks precede the final diagnostic wording change. They did not test
HA's backup UI, old HA-core artifact rollback, reordered-room rollback or arbitrary
crashes. Final exact artifact/version/CI and rollback acceptance still require review
and release checks; a successful check of known loss is not a preservation success.

The rollback target needs an exact previously installed integration release/artifact,
its manifest version/hash and a compatible HA version, together with the matching
pre-upgrade configuration/registry/restore snapshot. Restoring only entry data is
insufficient after room reorder, registry changes, removed entities or restore writes.
Replacing only the code may leave newer fields and state that older code does not
interpret safely. Keeping schema version 2 does not establish downgrade compatibility.

For an authorized rollback, stop MXZ control and allow pending calls to settle; then
restore the older integration artifact and its matching pre-upgrade HA backup through
HA's supported backup/recovery process. Restart with the compatible HA version and
check identities, mappings, priority, targets, enables, holds and raw-head state before
resuming. Reapply later intentional changes only after checking their compatibility.
A restored backup loses changes made since it was taken and may affect other HA
configuration. Heads are external equipment: backup recovery does not roll back
commands they already received. Verify the final release artifact and its specific
upgrade/rollback outcomes before making a release claim.

## Moving from the YAML package

The integration replaces the package's fixed two-zone helper/automation arrangement
with integration-owned entities and 2–8 rooms. It retains the shared decide/act/self-heal
approach, but newer fan, sensor, delivery and UI behavior differs. Automations and
dashboards that reference the old `input_*` IDs need updates. Run only one controller
for a set of heads.

## Per-room sensor freshness profiles

Initial setup does not ask for a reporting cadence. After setup, open
**Configure** and enter a room's documented report interval, maximum age and
optional startup grace in the advanced per-room fields. Maximum age must be at
least the interval. If maximum age is empty, MXZ uses three expected report
intervals; an explicit maximum overrides that default. Startup grace defaults
to the resulting maximum age. Choose `ha_state_write` only when every write is
a current acquisition; choose `sample_timestamp` only with exactly one trusted
sample timestamp or sequence attribute. When the source has no trustworthy
heartbeat, clear all three duration fields and both marker fields before
selecting `unknown`: MXZ then shows the room as cadence unknown and applies no
timeout. Saving reloads the integration, so a valid profile takes effect on
that reload. An invalid submission changes neither that profile nor any other
setting in the form.

## Entity ID mapping

| YAML package (`input_*` helper)        | Integration entity                  |
| -------------------------------------- | ----------------------------------- |
| `input_number.hvac_primary_target`     | `number.*_<zone>_target`            |
| `input_number.hvac_secondary_target`   | `number.*_<zone>_target`            |
| `input_boolean.hvac_primary_enable`    | `switch.*_<zone>_enable`            |
| `input_boolean.hvac_secondary_enable`  | `switch.*_<zone>_enable`            |
| `input_boolean.hvac_coordinator_enable`| `switch.*_coordinator_enable`       |
| `input_boolean.hvac_eco_idle`          | `switch.*_eco_idle`                 |
| `input_select.hvac_shared_mode`        | `select.*_shared_mode`              |
| `input_datetime.hvac_last_mode_change` | *(internal — no entity)*            |
| `sensor.mxz_plan`                       | `sensor.*_plan`                     |

The `*` prefix is generated by Home Assistant from the integration's device name, and
`<zone>` from the zone's own name — setup pre-fills each zone's name from the head you
picked and lets you change it before saving, so a head called "Bedroom" gives
`number.mxz_coordinator_bedroom_target` unless you name that room something else. (Entries carried
over from an integration install predating v3.0.0 keep their original
`..._primary_target` / `..._secondary_target` IDs for the first two zones — see
"v2.x → v3.0.0" below.) A zone's name is set on setup's room-names step and is edited
on Reconfigure (see "Renaming and reordering rooms" below); renaming the head itself
does not relabel these entities — and entity IDs are fixed at creation either way, so
nothing you rename later moves them. The plan sensor exposes the same
top-level attributes as before (`primary_demand`, `secondary_engage`, `standoff`, …),
plus a `zones` list — one dict per zone with `name`, `demand`, `engage`, `temp`,
`target`, `enabled`, and `fan_hold` (true while that zone's fan is manually held).
`fan_hold` is what a "who's driving the fan" dashboard reads.

Since **v2.1.0** the integration also creates a native single-target thermostat per
zone (`climate.*_<zone>_thermostat`) — see "Single-target HomeKit/Google tile" below.

### YAML transition steps

1. **Note your current setup** — screenshot your target values, which rooms are
   enabled, and any automations that read/write the old helpers.
2. **Stop the package's head-writing automations** before setup. Install the
   integration via HACS (see the README) and select 2–8 heads and a sensor per room.
   Leave the new coordinator and rooms disabled while checking the mapping.
3. **Remove the YAML package** (or comment out its `!include`) and reload/restart as
   its helpers and automations require. Confirm its head-writing automations are gone
   before enabling the integration.
4. **Re-point automations/dashboards** at the new entity IDs (table above).
5. **Set targets and room enables**, then turn on `switch.*_coordinator_enable`.
   Tunable constants (S, D, hysteresis, eco extremes, clamp, resting-mode bias) live in
   the integration's **Configure** → options dialog.

### Return to the legacy YAML package

Keep a copy of the package and its previous helpers/automation settings before the
transition. Disable and remove the integration entry, then restore the package include
and its saved configuration. Reload/restart HA as the YAML components require. Check
its helper values and raw heads before enabling its automations. This does not transfer
integration targets, registry identities, freshness profiles or fan restore data into
legacy helpers. It is distinct from an older integration-artifact rollback.

## Single-target HomeKit/Google tile (v2.1.0+)

The integration now ships a **native single-target thermostat per room** —
one `climate.*_<zone>_thermostat` per zone.

Each is a clean one-number "Auto" tile (`off` / `heat_cool`, a single setpoint — never a
dual heat/cool threshold) that binds directly to HomeKit/Google. It's a thin facade over
the room's `number.*_target` and `switch.*_enable` entities: setting the temperature drives
the target, turning it off disables the room, and the coordinator stays the sole writer to
your real heads. **Expose only these `climate.*` thermostats** to HomeKit/Google — not
the raw head `climate` entities (two tiles per room would fight over the same heads).

Optional **vane control**: if your heads expose vertical/horizontal vane `select` entities,
pick them in the config flow and they appear as swing modes on the thermostat tile.

## Resting-mode bias (v2.2.0+)

When **no room is calling**, the coordinator has to pick a shared mode to idle in. By default
it holds whatever was **last called** — but that can leave the system sitting in the wrong mode
for the season (e.g. resting in `heat` on a summer day until a real cool demand finally arrives).

The **Resting mode** option (Configure → options) lets you bias the idle mode:

| Setting | Idle behavior |
| --- | --- |
| `last` (default) | Hold the last called mode. Unchanged from earlier versions. |
| `cool` | Always settle on `cool` when no room is calling. |
| `heat` | Always settle on `heat` when no room is calling. |

The bias changes **only the neutral resting mode** — a genuine opposite demand (a room past its
demand threshold) still flips the shared mode, and the flip obeys the same mode hysteresis as any
other change. The default `last` preserves existing behavior, so upgrading needs no action.

**YAML package:** the same control is `input_select.hvac_resting_bias` (options `last` / `cool` /
`heat`). It has no `initial:`, so your choice persists across restarts; an unset value reads as `last`.

### No longer need the echavet proxy for the tile

Earlier versions relied on the echavet `mitsubishi_climate_proxy`
`coordinator_single_target` mode for the single-target surface. That mode is hardcoded to
write the legacy package's `input_*` helpers, so it pairs with the YAML package, **not** this
integration's entities. With the native thermostats you don't need the proxy at all. (The
`mxz_recompute` event is still honored, so any existing proxy/automation nudge keeps working.)

## v2.x → v3.0.0 (N-zone)

**Fan boost defaults ON from v2.10.0/v3.0.0.** If you never saved the option, the
delta-proportional fan is now active; opt out anytime under **Configure → Fan boost**.
An explicitly-saved off stays off.

**Manual fan holds no longer release themselves (v3.0.0-beta.18; already in the v2
stable line since v2.18.0–v2.20.0).** Coming from v2.20.0? You have this and the restart
fix below already — nothing changes for you. Two rules used to end a
hold without you asking: one folded a slider hold at the head's top speed back into `auto`
whenever the fan boost would have commanded that speed anyway, and one read *sliding to* max
as handing control back. Both are gone — every hold, at every speed, stays until you release
it with the **Fan auto** switch or by setting the fan to `auto`. If you relied on either, use
the switch instead. (beta.17 removed the first only partially: a slider hold could still
release itself on drift. beta.18 is the complete change.)

**Fan-hold restore introduced in v3.0.1.** The **Fan auto**
switch stores held/not-held intent for reconciliation with the head's report at
startup. That distinguishes a saved hold from automatic residue, but earlier releases
still failed the idle/delayed-report shape corrected in 3.4.0 below. A fan speed set from
a wall remote while HA itself was down, on a room that was not held, reads as leftover
and is cleared; and the first restart after upgrading (before the switch has stored
anything) can fall back to interpreting the reported speed. See the 3.4.0 correction
below for the bounded idle/delayed-report case and missing-record limits.

**A restart no longer parks a boost-driven head as "held" (beta.18, completed in
beta.19; ported to the stable line as v2.19.0/v2.20.0).**
The fan latch seeds from whatever speed each head reports at startup, and a head the boost
had been driving reports *our* speed — which used to seed as a manual hold, turning
**Fan auto** off and stopping the boost for that room until someone flipped it back on.
beta.18 fixed this only for a head sitting at the lowest speed the ladder would pick;
mid-ramp-down — where hysteresis deliberately holds the fan a rung higher — it still parked
the head as held. beta.19 accepts any speed the ladder would hold at the current delta. A
genuine manual hold (a speed the boost wouldn't be using) still survives the restart.

Automatic — nothing to do. On first startup the config entry migrates from the flat
primary/secondary shape to an ordered `zones` list. Zones 0/1 keep the `primary`/`secondary`
entity unique_ids. Existing registry records keep their entity IDs, preserving those
references in history and dashboards. Other runtime changes can still affect what a
dashboard reports. To add more
zones to an existing entry, use **⋮ → Reconfigure** on the config entry (see the README's
"Reconfiguring" section) — no need to remove and re-add.

**Display names follow the zone.** Every zone's entities are labelled with that zone's
name ("Bedroom target", "Bedroom enable"), including the first two — they used to read
"Primary"/"Secondary" no matter what you called the head. Display only: entity IDs,
history, and dashboards are untouched. An entry migrated from the flat v1 shape names
its first two zones "Primary"/"Secondary", so those labels don't move at all — override
them per entity in HA if you want something friendlier. A fresh install lands on the
zone-named IDs from the start.

**Per-zone Fan auto switch.** Every zone also gains a registered
`switch.*_<zone>_fan_auto` entity — a live mirror of that zone's manual-fan hold (ON = boost
drives, OFF = a manual speed is held) and the discoverable way to hand fan control back; see
the README's "Who drives the fan". The entity is available only while the head advertises fan
control with the exact `auto` token. It keeps the same registry identity through a temporary
capability loss or late head startup. The switch restores one held/not-held value across a
restart so the coordinator can distinguish your hold from its own leftover boost speed.
Purely additive: no existing entity IDs change.

**Per-room drift (v3.2.0).** Each room gains a `number.*_<zone>_drift` — its own
re-engage band, for presence automations (tight when occupied, wide when empty). Purely
additive: it defaults to the global drift and nothing changes until you write it. The
entity is config-category, so it sits on the device page and stays out of auto-populated
dashboards.

**Live airflow display.** If a head publishes its actual blower speed (CN105/ESPHome
heads expose a `stage` text_sensor), I auto-detect it at setup and mirror it onto that
zone's thermostat tile, so the fan dial tracks real airflow while the firmware runs its
own `auto` ramp instead of freezing on the last commanded token. Display only — it never
changes what I command. Auto-detected, with a per-zone override under **Configure**
("… airflow sensor"). Purely additive: no entity IDs change, and heads that don't publish
a `stage` sensor just keep showing the commanded token.

**New plan-sensor attributes.** The `zones` list above is the additive surface: each
zone dict now carries `fan_hold` (true while that zone's fan is manually held) — the same
value the Fan auto switch mirrors. Existing top-level attributes are unchanged.

## External inhibit / low-power standby hold

**Purely additive, opt-in — nothing to do on upgrade.** A new optional **standby hold**
lets an external signal drop the whole coordinator into a low-power state and bring it
back on its own. Under **Configure → options**, set:

- **Standby hold entity** (`inhibit_entity`) — a `binary_sensor` / `switch` /
  `input_boolean` to watch: a grid-status sensor, a load-shed switch, a "vacation" boolean.
- **Hold when the standby entity reads this state** (`inhibit_active_state`) — default
  `on`. Many grid sensors are inverted (they read `off` when the grid is down); set this
  to `off` for those.
- **What held heads do** (`inhibit_action`) — `eco` (default: software protection band),
  `off` (commands power-off; no temperature protection), or `fan_only`.
  Neither protection nor zero electrical draw is certified by those commands.

While the watched entity is in its active state, normal coordination is suspended and
every coordinated head is parked at the chosen action; when it clears, normal coordination
resumes on its own. The hold is a **separate gate from the kill-switch**
(`switch.*_coordinator_enable`) — it never changes your enable setting, so there is no
prior state to snapshot or restore. This replaces the common grid-down automation pattern
of snapshotting the master switch, disabling it for the outage, and restoring it after.

**Fail-safe direction, on purpose:** if the watched entity is missing or reads
`unavailable` / `unknown`, the coordinator treats it as **not held** and keeps
coordinating normally. A stuck or dropped sensor must never park the house indefinitely —
even at the cost that a grid sensor which *itself* loses power during an outage will let
coordination resume. Point `inhibit_entity` at a signal that stays reported through the
event (e.g. a UPS-backed grid sensor).

**Interactions.** During a hold the fan machinery is frozen (a manual fan hold is
preserved and reconciled on release, the same way a restart reconciles it); vane kicks are
suppressed (a kick would wake a parked head); and the off-while-enabled self-heal stands
down (a head the hold parked is not "drift"). The plan sensor gains a top-level
`inhibited` attribute (true while held).

## Idle action (v3.3.0)

**Purely additive, opt-in — nothing to do on upgrade.** A new **Idle action** option
(Configure → options, `idle_action`) picks how a satisfied head (or a standoff loser)
parks: `fan_only` (default), `off` (requests power-off), or `off_after_dry` (fan_only for a coil-dry period after active cooling —
`coil_dry_minutes`, default 10 — then off; heating parks off at once). The `off` choices
request that the indoor fan stop. They do not establish the cause of a smell or the
position of a refrigerant valve. Parked-head refrigerant behavior depends on model and
hardware settings. See the README's "How a satisfied head idles".

**Interactions.**

- The off-drift self-heal is now **plan-aware**: a head the plan parked off never arms
  it, while a head someone turned off during an active call (or mid coil-dry dwell)
  still heals. With the default `fan_only`, a head intentionally parked in that mode is not
  treated as off drift.
- On the way into an `off` park the coordinator first returns a boost-driven fan to
  `auto`, while the head is still awake. Missing/delayed reports still need restore
  reconciliation; the request alone proves no delivery. A genuine hold is not given
  that automatic fan write and survives the
  park and the restart, restored by the **Fan auto** switch as before.
- A vane change on a parked-off head wakes it briefly (the vane kick) and parks it
  again, the same way an eco-off head always has.
- The coil-dry dwell is a timestamp comparison re-derived on every recompute, so a
  restart mid-dwell can start a fresh dwell. Its expiry schedules another decision;
  a pending or failed external handler can still delay physical parking. A head
  observed `off` at startup owes no dwell.
- Eco/away and the standby hold are unchanged and take precedence as before — an
  eco-satisfied head parks off at once, with no coil-dry dwell.
- The plan sensor gains a top-level `idle_action` attribute.

## Fan ownership and command delivery (3.4.0)

The issue 25 correction distinguishes automatic fan residue from a manual hold in the
invented idle/delayed-auto-report restore shape. The frozen comparison retains the
released failure and candidate success, with a manual OFF twin that must stay held.
It does not identify a reporter's household cause or prove every crash/reboot shape.
An injected handler failure also exercises unavailable-switch persistence while heads
remain registered; it is not a head-disappearance test.

An in-flight `cool`/`heat` run resumes only when the coordinator and that room both
restore enabled, the head supplies usable observed mode evidence and the room sensor
is valid and not stale under an enforced freshness profile. If the head loads late,
startup intent can wait for that evidence. Construction and live enables adopt no prior
run. 3.3.0's construction-time compute could consume
the seed while rooms were still disabled; the newer restored-intent path fixes that.
Turning a room ON or OFF clears its latch. A target change while the coordinator is
OFF, the room disabled or startup unresolved also clears the pending run, so the next
enabled decision starts coasting and uses the ordinary re-engage band. Live retargeting
of an enabled resolved room retains its head-mode reseeding behavior.

Clean restored switch states use ordinary RestoreEntity state; unavailable states use
extra restore data for held/not-held intent and echo baselines. A recognized hold
survives. For a room that was not held, a speed the boost could never have set (outside
its ladder or above the boost ceiling) is held. A room idling under idle action
`fan_only` that reports any speed the boost could have set is read as the coordinator's
own idle, whether or not a record exists: such a head can report a speed of its own
while idling, and a hold there would never clear (issue 25). Without a usable record
this also needs an entry at least ten minutes old whose coordinator switch restored ON,
so a fresh install, re-added entry or first enable still honors the reported speed. Any
other room with a record reads a speed matching its remembered commands as residue and
holds any other; missing, invalid or stale records fall back conservatively to the
reported speed. The cost: a speed changed by hand on an automatic room while HA was
down may be read as residue and handed back, and on a `fan_only`-idle room any speed
the boost could have set is. Explicit Fan auto ON can wait for a usable report, and a
later OFF cancels that handback. Restore data is not a durable command-delivery journal.

No saved record is normal on a fresh install or for a newly added room; it adds no
fan-hold reason. A stale record says it belongs to an older entry; a present but
unusable record says it is missing or malformed. Valid hold restoration or explicit
Fan auto ON clears the restore reason. These are diagnostic wording and disclosure
changes, not new ownership or restoration rules. A head advertising fan
control but reporting no current `fan_mode` receives no fan write. ON may be saved as
`fan_on_pending` until a usable speed report; OFF cancels it.

Head calls are serialized per head. A slow handler leaves newer intent deferred for
that head while independent room delivery can proceed. After a return, current inputs
are recomputed rather than blindly replaying an old plan. Manual intent and unload
retire follow-on automatic work; an external call already accepted can still finish.
The plan's per-room command status, timestamps and control reasons expose software
observations, not physical receipt. A global update failure can still make coordinator
entities unavailable. Normal RestoreEntity and Voluptuous remain in use; HA 2026.11
restore API changes are outside this release's tested scope.

Manual fan picks from the room thermostat use that same head lock and wait behind
the current call, including a hung call. Peer heads can progress concurrently. A coast
follow-up refresh lets an echo-less head advance after a park. Sensor-event refreshes
are coalesced when a newer decision already covered the input; pending delivery can
request a follow-up outside the ordinary debounce. Multiple requests can coalesce;
this is not a promise of one compute per sensor write.

### Added diagnostics and state events

Room thermostats and plan `zones[]` add `command_attempted_at`, `command_returned_at`,
`command_failed_at`, `command_retired_at`, `command_status`, `command_error`,
`command_ownership_retired`, `command_deferred`, `command_timestamp_basis`,
`plan_target_basis`, `vane_retirement_cleanup` and `control_reasons`. Plan `zones[]`
alone adds `head_state_updated_at`. Fan auto adds `last_fan_command`, `prior_fan_command`,
`fan_control_reason` and `fan_on_pending`. See the
[diagnostic surface map](ENTITY-MAP.md#integration-diagnostic-surfaces-340).
Last/prior tokens are echo/adopted-observation baselines, not a command receipt log.
Missing observations say `not yet recorded`; a service return proves only an HA
handler return. Delivery listeners publish immediately outside decision completion.

These attributes are volatile. Fan-state, reason and command-progress changes can
emit a thermostat `state_changed` event with unchanged visible mode/temperature.
Recorded entities may therefore add history/storage, and bare state-trigger
automations may run more often. Select explicit transitions or relevant attributes
for your automation. `head_state_updated_at` is kept off the room thermostat so a head
attribute-only update it does not display adds no thermostat event or recorder row;
the plan carries it as of its last update and the head entity shows its own live time.
Recorder database rows, bytes and one-hour growth remain unmeasured. No recording
policy changed for this release.

## Configure value validation and older saved values (3.4.0)

Configure and setup's advanced tuning reject non-finite numeric comfort values
(`nan`, `inf`, `-inf`). This includes Demand threshold S, Re-engage drift, Mode
hysteresis, Coil-dry minutes, both Eco extremes, both Firmware setpoint limits,
both Lockout safety limits and both forecast Changeover thresholds. The first four
are magnitudes/durations and cannot be negative. Demand threshold, Mode hysteresis
and Coil-dry minutes may be zero. Re-engage drift additionally keeps its selector
bounds, 0.5–5 °F / 0.25–2.5 °C. Temperature thresholds may be negative, including
ordinary negative Celsius temperatures; the validation does not ban them.

| Actual Configure labels (temperature fields use HA's unit) | Rule / error key |
| --- | --- |
| Eco heat extreme / Eco cool extreme | Heat ≤ cool; `eco_band_inverted` if reversed. |
| Firmware minimum setpoint / Firmware maximum setpoint | Minimum ≤ maximum; `clamp_inverted` if reversed. |
| Heat-lockout safety floor / Cool-lockout safety ceiling | Floor ≤ ceiling; `lockout_inverted` if reversed. |
| Cool-lockout when forecast daily high ≤ / Heat-lockout when forecast daily high ≥ | Cool < heat; `changeover_inverted` for equality or reversal. |

Equality is allowed for eco, clamp and lockout bands. Only changeover requires a
nonzero shoulder band. Ordering errors mark both fields. Non-finite values show
“Enter a finite number” (`not_a_number`); negative magnitudes/durations show
“This value cannot be negative” (`must_not_be_negative`). Selector bounds also apply.

3.3.0 could save combinations now rejected. Upgrade/migration does not silently
rewrite or newly validate those stored values. The entry continues to use them;
retention is not evidence they are safe or useful. The next Configure submission
validates the submitted form, including saved values supplied as defaults, so even
an unrelated edit can be refused. Repair the indicated numeric values and both
members of any inverted/equal-changeover pair, review the whole form and resubmit.
A refused submission saves none of its other settings and does not reload the entry.
Freshness has separate finite **positive-minute** and whole-profile requirements in
[Stale room sensors](#stale-room-sensors-340); comfort's zero allowances do not apply there.

## Head capability validation (3.4.0)

New setup requires each selected climate entity to advertise both `heat` and `cool`. Its
advanced tuning step lists only parking choices supported by every selected head:
`fan_only` requires `fan_only`, `off` requires `off`, and `off_after_dry` requires both.
When the backward-compatible `fan_only` default is unavailable, setup requires an explicit
supported choice instead of silently storing a different default.

Reconfigure validates new head selections against the entry's saved idle action. An
incompatible submission leaves the entry, options, identity, and head ownership unchanged
and names the supported alternatives. The options flow applies the same rule when you change
**Idle action**. Existing entries are not migrated or rewritten; if an older entry stores an
action its current heads do not advertise, Configure lists the compatible alternatives and
requires you to choose one explicitly.

If a stored head has no state or unknown/malformed `hvac_modes`
(`head_capabilities_unavailable`), advertises modes missing either `heat` or `cool`
(`head_missing_heat_cool`), or the heads have no common parking mode
(`head_missing_idle_modes`),
Configure shows a recovery error before it builds the idle selector. It keeps the entry and
its saved defaults unchanged. Restore the missing capabilities or reconfigure compatible
heads, then retry. You cannot save other tunables in Configure until the configured heads
resolve.

This blocks every Configure save, including unrelated comfort or freshness edits.
An `unavailable` state that retains the required capability attributes can pass;
the gate reads capabilities rather than requiring a particular availability state.
Recover the head integration's metadata or select compatible heads in Reconfigure,
then reopen Configure and review its complete form before saving.

The room thermostat now advertises fan control only when the underlying climate entity has
Home Assistant's fan-mode feature and a non-empty option list. It passes those option strings
through exactly. The separate **Fan auto** handback entity stays registered, but it is
available only when that exact list contains `auto`. If the head loads late, the entity returns
under the same identity and reconciles its restored held/not-held value. Heads that spell or
model automatic fan differently retain their own manual fan options but are not presented as
supporting the coordinator's `auto` handback.

HomeKit/Google bridge timing when a head's fan capabilities arrive after accessory
creation is untested. Conditional entity-feature tests do not establish that a bridge
adds its fan control without reloading. No live household or sign-in test is implied.

## Room records and holds across reload and restart (3.4.0)

Each room's `number.*_<zone>_drift` record is no longer removed and recreated on every
setup. Home Assistant 2026.x carried a deleted record's name, area and disabled flag
back, so the loss showed only on older cores: on Home Assistant 2024.12 a drift number
you renamed, filed into an area or switched off came back plain and enabled after each
reload and each restart. Rooms you drop are still pruned, and a pruned room does not
come back.

A fan hold now also survives a restart that caught the head's integration unloaded. The
**Fan auto** switch is unavailable while its head is missing from the state machine, so
Home Assistant stored `unavailable` rather than the hold, and the hold was lost. The
switch now carries the held-or-not value beside its state, so your hold returns when the
head does. A hold you released before the restart stays released, and a head that is
still missing after the restart still reads unavailable.

## Invalid room sensors (3.4.0)

**No configuration change — one behavior change to know about.** A room sensor reading
the coordinator cannot trust now steps aside *before* both the normal and the eco demand
test. That room supplies no automatic temperature demand until its reading is valid. Invalid
means: the sensor is missing, `unknown` or `unavailable`, its state does not parse as a
number, it parses as `nan` or `±inf`, or it declares a unit that is not `°C`, `°F` or `K`.

**Interactions.**

- A sensor whose unit attribute is **absent or `null`** is still read in your HA
  system unit. An empty-string unit (`""`) is an explicit unsupported string, not
  absence, and is rejected just like `%` or `W`. Other unsupported strings and
  non-string unit values are also invalid. Setup/reconfigure cannot save these
  unsupported units; at runtime the room contributes no automatic demand.
- A sensor whose state carries a different supported unit than the system is converted
  once. Home Assistant already normalizes most temperature sensors to the display unit
  before storing them, so this only bites a per-entity unit override.
- No plausibility range was added: a finite supported-unit temperature passes the
  reading check. Age is a separate, opt-in freshness check described below. Without
  that contract, a stale-but-valid number is still eligible.
- An invalid room parks by the configured idle action (`fan_only` by default), or `off`
  when eco/away is holding it. Other rooms are untouched: the shared mode is decided by
  the healthy rooms alone, and the plan's `sensors_ok` attribute goes false.
- The room's `temp` in the plan sensor's `zones` list is now `null` while the reading is
  invalid, where v3.3.0 showed the target default (70 °F / 21 °C) as if it had been
  measured. **A dashboard or template reading `zones[i].temp` must tolerate `null`.** The
  room's own thermostat tile likewise reports no current temperature rather than a
  fabricated one.

## Stale room sensors (3.4.0)

**Opt in per room; nothing changes until you do.** An entry saved by an older version
configures no reporting cadence, so every room keeps an unknown cadence and no reading is
rejected for its age — exactly v3.3.0's behavior. There is no default cutoff, and none is
derived from another room, from history or from Home Assistant metadata.

A room is checked for age only when it says **both** how often its sensor reports **and**
what makes one of that sensor's writes a reading. Either half alone leaves the room's
cadence unknown. The keys live in each room's entry (`zones[i]`); the three durations are
in **minutes**:

| Key | Meaning |
| --- | --- |
| `report_interval` | how often this sensor reports. The maximum age becomes three of these: the room goes stale when the third due report has not arrived. |
| `max_age` | an explicit maximum age, used instead of three intervals. Enough on its own. It must be at least `report_interval`: a shorter one is not raised to fit, it makes the profile invalid (below). |
| `startup_grace` | how long a room with a valid reading is provisionally eligible after a restart or reload. Defaults to the maximum age. A room that starts healthy on a sample time already present does not use it, and a room whose reading is unusable at load gets none: it is rejected at once. |
| `evidence_basis` | what this source can prove. `unknown` (the default) is never checked for age. `ha_state_write` says the source's own contract guarantees every write it makes — including the first one after a restart or reload — is a current reading. `sample_timestamp` says the source publishes a trustworthy marker. |
| `sample_timestamp_attribute` | with `evidence_basis: sample_timestamp`, the entity attribute carrying the time the sample was taken. It must be a time with a zone, no later than now, and later than the last one accepted. |
| `sample_sequence_attribute` | the alternative marker: an attribute carrying a number that increases with each new reading. A sequence has no clock, so the room's window runs from when Home Assistant received the increase. Name this **or** `sample_timestamp_attribute`, not both. |

A profile with a duration that is not a finite positive number, or with a maximum age
shorter than its interval, is invalid as a whole. **Configure** rejects the whole
submission with one explanation and keeps the last valid profile and every other setting;
nothing in the invalid submission reaches the entry. Nothing is read as unset, raised or
rounded into a cutoff. A hand-edited invalid profile already in the entry is still logged
once at load and enforces nothing, exactly like no profile. A maximum age with no trusted
basis is logged too, once, and enforces nothing — a duration says how often a source
promises to write, not whether its writes are readings.

Only an **advancing** marker is a report. Two cache flushes carrying the same sample time
leave the deadline where it was, and a marker from the future, or older than the last one
accepted, is shown but never used. So a cloud integration that writes to Home Assistant
on its own schedule goes stale on its device's clock, not on its own.

A marker write is looked at once, when it arrives, and what it proved is kept on record
for the room; later recomputes read that record rather than the entity again. So a
sequence's window runs from the instant Home Assistant received the increase, and a
later write carrying the same number does not move it. A marker rejected on arrival — from
the future, or out of order — stays rejected: a recompute an hour later, when the clock
has caught up with a future sample time, does not turn the same write into a report. Only
a new write can. Every write is looked at, changed or not, against the last marker
accepted: a source that sends the same sample again once its time has passed, and it is
still inside the maximum age, is reporting, and the room recovers on that write.

A source can still misstate its contract. Declaring `ha_state_write` for a source that
replays a restored or cached value makes that replay look current for one maximum age,
and repeated cache writes can keep extending the deadline. Home Assistant's timestamps
cannot detect that mistake; only the sample-marker basis can.

After initial setup, configure these keys through **Configure**. The coordinator reads them
from the entry's data or options.

**Interactions.**

- Invalid readings are unchanged and still come first: a missing, `unavailable`,
  non-numeric, non-finite or wrong-unit value is rejected immediately, whatever its age.
  Age is only ever asked about a reading that is otherwise valid.
- A stale room leaves automatic demand, drops its engagement latch, and its head parks
  through the same idle path a satisfied room uses — `fan_only` by default, `off` (after
  the usual fan handback) or `off_after_dry` if you chose those, and `off` while eco or
  away is holding the room. A standby hold still parks every head its own way. Healthy
  rooms are unaffected: the shared mode is decided by the rooms that still vote.
- A recognized manual fan hold is not touched by any of this. The room's automatic demand
  is suspended; the speed you are holding is not written over.
- The room keeps publishing its last real reading, with `sensor_health` and `sensor_age`
  (seconds since the last write) beside it in the plan sensor's `zones` list. No value is
  substituted and no other sensor is read in its place. The plan's `sensors_ok` attribute
  now also goes false for a stale room, as it already did for an invalid one.
- Under `ha_state_write`, an unchanged write counts as a report: a sensor that keeps
  publishing the same temperature stays healthy, and only silence expires. Under
  `sample_timestamp`, a write counts only when its marker advances: a cached value
  written again with the same marker is not a report, and the room expires on its sample
  clock however often Home Assistant is written.
- Recovery needs one report that is both valid and still inside its own maximum age. A
  replayed or backlogged value older than the window does not recover the room. On a
  marker basis the report must also be NEW since the room became unhealthy: a sensor that
  goes `unavailable` and comes back with the same marker has re-sent the reading it had,
  and the room stays parked until the marker advances. Under `ha_state_write` the return
  write is itself a new reading, by the source's contract, and recovers the room.
- The room is reconsidered at the deadline itself, with no added allowance. A report that
  arrives at that instant moves the deadline and the room stays healthy; a report that
  arrives after it recovers the room, but does not erase the episode it already missed.
  The head write that follows a park or a recovery goes through the ordinary refresh
  path, so it lands within its debounce rather than in the same instant.
- One `warning` opens an unhealthy episode and one `info` closes it. A stale room that
  then goes `unavailable` does not log a second failure.
- **Reload and restart lose sensor health.** Reloading the entry builds a new coordinator,
  and nothing about health is stored, so a value already sitting in Home Assistant cannot
  be told apart from a restored one. What each room starts as depends on its reading
  first, then on its evidence basis. A room whose reading is missing or unusable at load
  is rejected at once, with no grace, exactly as the first bullet says. A
  `sample_timestamp` room whose sensor already carries a valid reading and a sample time
  inside its maximum age starts `healthy`: the sample dates itself, its window runs from
  that time, and no grace applies to it. A sample sequence found at load is a baseline
  only, and an `ha_state_write` room needs a write made after the load, so those rooms,
  given a valid reading, reopen as `awaiting_report` — eligible, and flagged — until the
  first witnessed report or the end of one `startup_grace`, whichever comes first. A room
  that was unhealthy before the reload gets that same grace when its reading is valid at
  load: nothing is retained to say otherwise. A room still without a usable reading stays
  rejected. The coordinator logs once per load how many rooms are provisional, how many
  started on their own sample time and how many were rejected for their reading. What is
  re-derived from Home Assistant is the reading's validity, the age of its last write and
  the sample marker it carries; what is retained is nothing.
- A source that replays a cached or restored value as a fresh write cannot be told from a
  real one by Home Assistant's timestamps. Declare `ha_state_write` only for a sensor
  whose writes really are new readings; where the source publishes a sample marker,
  `sample_timestamp` is the stronger evidence.
- Every unhealthy entry opens the episode with the reason it entered on — past its
  maximum age, or no usable reading — and the room's return to eligibility closes it. A
  room with an unknown cadence comes back to `cadence_unknown`, not `healthy`: nothing
  about it was proved, and nothing about it is enforced.
- The line that opens a stale episode says what expired: the age of the last qualifying
  report against the maximum age, or the startup grace with no report witnessed inside
  it. The age of Home Assistant's last write is given beside it, in parentheses, as a
  diagnostic only — for a cached source the two differ, and a write from this second can
  sit beside a report that is fifteen minutes old.

## Renaming and reordering rooms (3.4.0)

**No configuration change — two behavior changes to know about.**

Reconfigure's second step now carries a **Room name for head N** field per room,
pre-filled with the room's current name. The name it saves lands in the room's existing
`name` key, and it relabels that room's target, drift, **Follow global drift** button,
enable switch, **Fan auto** and room thermostat. Clear the field to fall back to the head's own name. A name you typed
into an entity's own settings still wins: Home Assistant shows your name, not the room's.
Entity IDs, unique IDs and the registry records themselves are untouched by a rename, so
history, dashboards and automations keep working.

Reordering the head list used to move each room's **settings** as well as its priority.
The room entities are keyed by priority slot (position 1 is `primary`, position 2
`secondary`, then `zone_3`…), so swapping two rooms handed each of them the other's
target, enable, drift override and fan hold. Nothing warned, because every value on its
own looked plausible. A reorder now moves each room's registry records with the room, so
its settings — and the record's own name, area and disabled flag — arrive wherever you
put it. Entity IDs remain attached to the room; slot-derived unique IDs and the entry's
ordered-head unique ID change. Plan slot attributes follow the new priority.

**Interactions.**

- The plan sensor's slot-keyed attributes still describe **priority**: after a reorder
  `primary_demand` and `zones[0]` are the room you moved to the top, as before.
- Removing a room is unchanged: its entities are pruned on the setup that follows, and a
  room promoted into the freed slot keeps its own settings rather than inheriting the
  removed room's.
- Adding a room is unchanged: it starts disabled, with a fresh target seeded from its
  head and no drift override or fan hold.
- Reordering is a reconfigure action, not a schema migration on ordinary load. It
  re-keys existing room registry records; removed or never-registered records cannot
  be carried. Other 3.4.0 runtime changes still apply on an ordinary upgrade.

## Shared mode offers `cool` and `heat` only (3.4.0)

`select.*_shared_mode` used to list four choices: `cool`, `heat`, `fan_only`, and `off`.
Only two of them are directions the plan can run. Arbitration resolves any value that is
not `cool` or `heat` to `cool` (the same resting fallback a cold start uses), so choosing
`fan_only` or `off` did not stop or park anything on purpose — the coordinator kept
conditioning, in `cool`. When the coordinator was enabled and not held in a fixed-mode
standby park, the apply that followed also wrote `cool` back into the selector, so the
choice usually vanished within a refresh.

They were unreliable stop/park controls, and what they actually did depended on the rest
of the system:

- With the coordinator running, the fallback to `cool` **could park a head that had been
  heating**: that room stops being heated and is left in `fan_only` when it does not want
  cooling. That is a real effect on the heads — just not the one the label promised.
- With **Coordinator enable** off, or while a fixed-mode standby park was holding the
  heads, no apply reached the writeback, so a stored `fan_only` or `off` was simply
  retained on the selector until something else changed it.

Those two choices are gone. Picking one is now rejected by Home Assistant, exactly as any
other unsupported option is.

**Your stored value migrates to `cool`.** An entry that restarts holding `fan_only` or
`off` comes back reading `cool` — the direction arbitration resolves those values to — and
a one-time repair message says so. The stored value does not record which direction the
system had last been running, so the migration does not try to restore one: it lands on
that same `cool` fallback. No hold, override, or persistent state is created by the
migration, and nothing else about the entry changes. A stored `cool` or `heat` restores
unchanged.

Picking `cool` or `heat` by hand is a request for that direction, handled by the same
arbitration as before. A changed direction stamps the mode-flip dwell exactly as an
automatic flip does, and automatic arbitration is free again once that dwell elapses.
Picking the direction already showing changes nothing: the dwell clock stays where it
was, so a repeat choice buys no fresh dwell and creates no hold. This release adds no
expiry, renewal, time-based release or durable hold of its own. The choice picks a
direction and nothing else:
it does not start the coordinator, wake a disabled room, or lift a lockout, the eco band,
a standby hold, or a setpoint clamp.

### What actually stops or holds the system

| To do this | Use this |
| --- | --- |
| Stop new coordinated control, retaining the last commanded state | `switch.*_coordinator_enable` off (the kill-switch); an accepted external call may still return |
| Hold the heads at the protection band, still conditioning a room at the extremes | the [standby hold entity](#external-inhibit--low-power-standby-hold) (`inhibit_entity`) with its default `eco` action |
| Request a fixed park during an active hold | the same standby hold with **What held heads do** set to `off` or `fan_only`, while coordination is enabled |
| Take one room out of coordination | that room's `switch.*_<zone>_enable` off |
| Stop a satisfied head's fan instead of circulating | **Idle action** → `Off` (or `Off after a coil-dry period`) |
| Turn one head off right now | the head's own controls, with the kill-switch off |

**YAML package:** `input_select.hvac_shared_mode` still lists all four options and keeps its
own behavior. It is the legacy package and is not changed by this release.

## Setup and reconfigure screens (3.4.0)

The config-entry schema stays at version 2 and safety defaults are retained. Setup
stores an ordered `zones` list, room names and comfort values; sensor freshness is
configured later. Reconfigure can change the head-order-derived entry unique ID and
move existing room registry unique IDs to new priority slots while keeping entity IDs.
New validation can reject submissions older screens accepted. A saved room name can
change display labels. These are real behavior and identity-management changes, even
though the stored keys and schema version are retained.

Setup was three screens, and the third was every comfort tunable. It is now four:

1. **Heads** — the same picker, in the same priority order, plus an optional name for the
   outdoor unit. That name is the entry's title. It is not an entity id and not an option
   key, so naming or renaming it moves nothing.
2. **Rooms** — new. Each box is pre-filled with the head's name; distinct non-empty
   names are required. The result goes in the zone's existing `name` key. The form field
   names are not stored. Reconfigure uses the saved names and allows clearing back to
   the head's current name.
3. **Sensors** — the same pickers, now labelled and listed by room, showing what each
   chosen sensor is reporting and how long ago.
4. **Summary** — new, and the only point at which anything is written. It lists every
   room and offers **Save and finish**, **Change advanced settings** and **Go back to
   heads and rooms**.

Comfort tunables moved behind **Change advanced settings**. Skipping it validates
an empty submission against the same unit-specific default schema. The optional
changeover and standby entities have no default to store. If the heads cannot support
the default idle action, an explicit supported choice is required before saving.

Setup now refuses one temperature sensor shared by two rooms, a sensor Home Assistant has
no state for, and a sensor reporting a unit it cannot convert. It does **not** refuse a
sensor that is merely unavailable or momentarily non-numeric: the summary names that room
and says it makes no automatic demand until a valid number arrives, which is what the
coordinator already does at runtime. A sensor with an absent or `null` unit is still
read as your system unit; empty-string and other unsupported units are rejected.
Kelvin is still accepted.

The flow shows each sensor's reporting age as information. It stores no maximum age, no
expected interval and no startup grace, and it times no sensor out.

Reconfigure gained the same shape — heads, room names, sensors, summary — and its room
names moved onto their own step. Clearing a name there still falls back to the head's own
name. Its summary reports each effective stored freshness profile and leaves only
unconfigured rooms at cadence unknown. Comfort and freshness settings stay in
**Configure**: one form, the existing per-room override fields plus the six freshness
fields per room, and the same merge-and-mirror save.

## A room can return to the global drift (3.4.0)

**No configuration change. One new entity per room, and no migration.**

Each room gains a `button.*_<zone>_follow_global_drift` — **Follow global drift**. It
does one thing: it drops that room's drift override, so the room follows the global
`engage_deadband` again, live, the way it did before anyone wrote its drift number.

This is the way back that per-room drift never had. Every write to
`number.*_<zone>_drift` makes an override, and no value released it — including today's
global value. **An override that equals the global is still an override**, and it keeps
its number when you change the global under it. Following is stored as the absence of an
override, not as a copy, which is why re-typing the global is not the same as pressing
the button.

The press clears the room's stored override. Its drift number republishes at once with the
global value and `override: false`, then the press requests the ordinary debounced
recompute. Applying the global band can change that room's demand, the shared mode and the
head, exactly as typing the global value would; the recompute is not promised complete at
the moment the number republishes.

**Restore.** No new stored key, and nothing to migrate. The room's drift number already
persists `override` beside its value, and that flag is still the only thing restore
reads: a room you returned to following comes back following (and shows whatever the
global is *then*), an override comes back as that override, clamped into the profile
band. A usable older state with no `override` attribute restores as a follower;
missing or stale records still follow the number's ordinary restore rules. The drift number
owns restoration of following versus override. The button carries no drift setting,
though Home Assistant restores its last-pressed time like any button.

Because the drift number owns that saved state, disabling or removing a room's drift number
also makes its **Follow global drift** button unavailable. After re-enabling the number,
the button stays unavailable until that number has loaded and published its state.

**Scope.** Per room, and drift only. The press clears no target, **enable**, fan hold,
lockout or engage latch, and it touches no other room. The ordinary recompute may change
control demand, shared mode and head output after the global band takes effect. There is no
all-room or "reset everything" control, and no default changed. Purely additive: no
existing entity ID moves.

A reorder carries the new record with its room, like the room's other entities, so its
own name, area and disabled flag arrive wherever you put the room.
