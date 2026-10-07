# Issue 25 restore comparison

This diagnostic uses invented heads and sensors with actual HA climate service
handlers. It compares these immutable products using **identical** test bytes:

- Released: `3a9863896f8affb6f71cbd1e495df21b17a69ff3`
- Main comparison slot: tested candidate `c13114f94d18f26ed375f63d6a7df90376bf1134`
  (tree `853a6441c14f441f8ad7336dfd7f08c19ec9c9be`).

The slot named `main` exports this exact candidate until it is merged into public
main. The workflow/harness source commit is recorded separately from the exported
product commit and tree. Historical old-main comparisons do not validate this candidate.

Both use Python 3.12.14 and the floor HA 2024.12.0 constraints, SHA-256
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
| 06 | B must be visibly cooling at changed demand after restore; idle A remains the valid-ON control. |
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
history. After entry unload, storage is loaded again before setup. Observers call
the real HA restore getters and identify their returned records; they never supply
replacement state or extra data. Cache or getter mismatches are UNKNOWN. The sole
injected missing record is explicitly confined to cell 05.
This is an in-process restore-store lifecycle, not a physical reboot or hardware test.

The comparison has a dedicated floor job with a locked installation and ordinary
sentinel. The three full suites run in separate ordinary CI jobs, including
invented restore lifecycle and entity-add failure controls. Each product runs
with fresh fixture state. Infrastructure, prerequisite,
source-integrity or phase failure stops the comparison; no absent cell passes.
All three locked suites and all five CI families remain required.

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

Evidence includes complete test logs, source and inventory checks, final phase
and pack markers, outer exit and cleanup. Missing or oversized logs invalidate
the evidence. Test output does not establish effective token scopes or repository
settings. No dependency cache or artifact upload is used.

## Exported setup admission

The recovery harness has three distinct, push-only, attempt-1 routes:
`ci/issue25-harness-admission`, `ci/issue25-harness-replacement`, and
`ci/issue25-harness-repeat`. The admission route retains the seven ordinary CI
jobs; only its floor lane runs setup controls before its full suite. Replacement
and repeat use the dedicated floor job. The older diagnostic refs refuse.
Dispatch, PR, tag-shaped, missing-context and rerun routes do not authorize
comparison work. Source declarations do not establish hosted settings or public
publication permission.

Admission uses the same exported `test_restore` module, `trial` fixture, real
entity-add observation and finalizer as a full pack. It imports the switch class
in the ordinary module import block. Its single test checks initial real public
switch/plan and handler prerequisites without running a residue or restart
schedule. This setup choice does not explain the historical import failure.

The fixed sequence is P-release, P-main, N-missing, N-origin, N-config and
N-fixture. Each uses a separate export and a 60-second watchdog plus a five-second
TERM grace. Negatives respectively block the component import, redirect it to an
equal-byte sibling copy, select an equal-byte config at a wrong path, or register
a distinct no-op fixture. Product and installed dependency files are unchanged.
The expected native exits are 4, 4, 4 and 2. An earlier failure, different exit,
unreached intervention, positive call in a negative, missing cleanup or truncated
evidence invalidates the control. These outcomes and pinned plugin semantics are
first-execution predictions; authoring the controls is not runtime evidence.

The export uses explicit prepend import mode, root directory and generated config
(`[pytest]` and `asyncio_mode = auto`, each LF-terminated), with installed pytest
entry points enabled. Passive snapshots precede configuration, collection and
fixture gates. The post-import origin gate precedes item creation. Collection
failure reports retain their native text and ordinal independently of mixed
stdout/stderr ordering; the final collection gate follows the phase observer and
refuses failed, empty or wrong collections. Full packs still require the eight
complete ordered IDs, independently checked by their supervisor.

`run.sh` takes no arguments and requires explicit `ISSUE25_MODE`. It rejects
externally supplied child case/base/injection keys, unknown control keys and
Python/pytest overrides. `pack.sh` takes exactly seven positional inputs: source,
export, base label, full base commit, full base tree, baseline inventory and the
parent's `expected.json`. The separately frozen `expected.sha256` binds that
strict input object. A full pack refuses every injection selector, including an
empty one. There is no validation-only or test-success mode.

Each child has a closed environment with no HOME, token, proxy, Python path or
pytest override. Its root is
`RUNNER_TEMP/issue25-<A3|R2|I1>-<run_id>-a1/<case>`; reuse and symlink aliases
refuse. The parent owns the export, expected inputs, manifests, separate pre/post
integrity/inventory records, native output and exit. Only the origin negative has
a decoy tree, and only the config negative has an alternate config. New helpers
use `/usr/bin/python3` without importing dependencies; distribution metadata is
read explicitly from the floor venv. The venv interpreter runs only inventory
checks and the counted pytest child.

Package snapshots select nine named dependency source files and the two pinned
pytest-entry-point module files: eleven records, at most 8192 bytes per snapshot
or fixture-file manifest. A different membership/count or changed selected file
is UNKNOWN. Bytecode, cache and other mutable package files are excluded; this
is a selected-source drift check, not a whole-venv immutability claim. Actual
versions, selected fixture functions and four dependency lifecycle hashes remain
required evidence.

The ordinary controls preserve 318 existing direct subprocess calls and add 122
(G20, P16, B32, J24, V10, L20), for 440 per suite, plus 52 in-process helper rows.
Invented new Git fixtures use stdlib loose-object construction. Their endpoint
stand-ins never establish HA or import-origin correctness. The full suites keep
their 180-second and 4-MiB ceilings; the narrowest historical margin is py314's
360896 bytes. First execution fit remains unknown. No failed admission or
comparison authorizes a retry, extra process, changed oracle or enlarged cap.

Replacement and any independently preregistered repeat retain both complete
packs, with a 180-second watchdog each, the ordinary sentinel and no full-suite
replay. All sixteen public-state/store/handler conclusions, the manual twin and
unavailable prerequisites require independent interpretation. Both-green means
unreproduced and allows only the already bounded conditional repeat. Source
review, setup admission, comparison, repeat and product/release acceptance remain
separate decisions. Identity, settings, capability, rendered form, migration,
rollback, final CI and household obligations are not closed by these controls.
