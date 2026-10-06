# Issue 25 restore comparison

This diagnostic uses invented heads and sensors with actual HA climate service
handlers. It compares these immutable products using **identical** test bytes:

- Released: `3a9863896f8affb6f71cbd1e495df21b17a69ff3`
- Main: `009b6b42326252ee633f2288573a8052f4e8c1e8`

Both use Python 3.12.14 and the main HA 2024.12.0 constraints, SHA-256
`0d0031ddca8bb870560ca4d4bcafd34233e23e9dd6f0470dc31f7bd888eb7403`.
No product implementation or per-base test helper is overlaid. The harness owns
its entry data, sensors, head implementations, schedules and expected outcomes.

| Cell on each base | Intervention and independent oracle |
| --- | --- |
| 01 | Two clean store round trips: idle A retains automatic ownership; active B controls for fixed-point adoption. |
| 02 | Twice repeat with an actual dependency handler error and both heads absent at shutdown. Naturally unavailable switches are required; unreached is UNKNOWN. |
| 03 | Public Fan-auto OFF at the identical idle/high token: clean then unavailable round trip, held through demand, no automatic A fan writes. No ON reset. |
| 04 | Actual options reload, public manual pick/OFF, retained hold, explicit ON with delayed auto report. |
| 05 | Labelled missing-restore negative: conservative idle hold and active fixed-point control, then explicit ON recovery. |
| 06 | Active B demand changes before restore; idle A remains the valid-ON control. |
| 07 | Provisional auto before setup, then a controlled old high report after setup. |
| 08 | Missing current fan speed, recovery, explicit ON; public state, pending restore and actual later delivery. |

Targets are 70 °F. Both rooms start at 75 °F with coordinator-written high fan
speed. A satisfies at 70 °F while its completed auto command's report is delayed;
B stays at 75 °F. Later 73 °F demand expects a completed middle fan handler.
Fresh invocation gates and copied arguments distinguish entry, return and report.
The manual control must receive no automatic fan command at all.

The first three cells dump and reload HA's real `core.restore_state` store twice,
using the same entry, creation time and entity identities and fresh coordinators.
Each automatic cycle starts with public ON handback and newly created residue.
Snapshots retain actual Fan-auto availability/state, public plan, logical hold,
head mode/fan/capabilities, store timestamps/extra data, pending restore and command
history. The sole injected missing record is explicitly confined to cell 05.
This is an in-process restore-store lifecycle, not a physical reboot or hardware test.

`ci/issue25-discriminator` is the separate diagnostic launch branch. Before its
single push, review the exact public source and ordinary locked-suite admission,
confirm public standard hosted execution and the workflow permission boundary.
The driver must obtain complete logs, including the platform's action/image/token
permission records. A previous source review does not accept runtime behavior.
Do not create a simultaneous PR or dispatch a duplicate run.

The floor job reuses its environment after the ordinary sentinel and full suite
complete. The workflow uses ordinary setup/call/teardown, and `run.sh` permits only two
180-second packs in the same floor environment, with fresh fixture state. It
stops after infrastructure, prerequisite or phase failure. No absent cell passes.
A separately approved trace-only invocation may address one concrete ambiguity;
the entire comparison is bounded to 90 minutes and three HA processes. It is not
an automatic retry. Routine suites belong to ordinary CI outside that diagnostic
allowance. All three locked suites and all five CI families remain required.

`OwnershipFailure` means an ordinary completed cell violated an independent
expected outcome. It stays red in pytest and CI. Setup/teardown errors, timeouts,
missing cells, unavailable recovered state and unreached prerequisites mean
UNKNOWN, never issue-red. Read the individual shapes and mandatory controls:

- Released red/main green permits regression retention and precise documentation
  of the demonstrated invented shape; it does not establish a reporter's cause.
- Both red requires demonstrated cause and a scoped correction.
- Both green means unreproduced: stop, with no fix claim or new scenario hunt.
- Main-only red, mixed controls or UNKNOWN permits no fix/already-corrected claim.

A failing manual twin blocks acceptance even if the automatic cells are green.
The other schedules characterize competing shapes; they do not replace the
idle/active discriminator. Keep the same oracle for any later correction.

Evidence uses ordinary job logs only: no dependency cache saves or artifact
uploads. Each test job is bounded to 16 MiB of evidence, including provenance;
other jobs to 2 MiB each. Missing/truncated logs, final phase/pack/export markers,
outer exit or cleanup evidence block acceptance. Retrieve and hash the complete
public run logs before retention expiry. The ordinary platform logs are also
required: the test log cannot certify effective token scopes or repository settings.
