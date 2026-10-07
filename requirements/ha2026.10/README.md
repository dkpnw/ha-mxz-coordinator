# Exact Home Assistant 2026.10 test environment

This is an additional qualification target. The existing three Python/Home
Assistant lanes and their locks remain unchanged. It does not change the
integration's manifest, minimum supported Home Assistant version, or product code.
A successful setup is not a successful integration test or hosted qualification.

## Run locally

Use Linux x86-64 with glibc **2.41 or newer**, Bash, Git, HTTPS access to public
GitHub/PyPI, and a bootstrap Python **3.12 or newer**. The bootstrap Python only
orchestrates setup. Tests use the exact downloaded Python **3.14.8**.

Choose a new directory outside the checkout. All runtime, source clones, builds,
downloads, HOME, temporary files, caches and command receipts stay there. The
recipe checks free space before each workload: 2 GiB remaining growth plus 1 GiB
concurrent-growth allowance and a 1.5 GiB reserve; at least 350,000 free inodes
includes 100,000 prospective entries and a 250,000-inode reserve. These are planning
estimates, not a claim of continuous capacity. The subprocess monitor terminates
its own process group near the reserve. Keep the directory after a failure to
retain its receipts.

```bash
python3 -B tools/ha202610.py schema
python3 -B tools/ha202610.py prepare "$HOME/mxz-ha202610-work"
```

Use an absolute path for the generated interpreter. Its wrapper disables bytecode
and binds the downloaded libpython, including for `sys.executable -I` children.
It does not install into a global tool cache or run the archive's `setup.sh`.

```bash
work="$HOME/mxz-ha202610-work"
export HOME="$work/home" TMPDIR="$work/tmp"
export XDG_CACHE_HOME="$work/cache" XDG_CONFIG_HOME="$work/config"
export XDG_DATA_HOME="$work/data" PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$work/bytecode"
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$work/venv/bin/python" -m pytest \
  -p tools.pytest_phases tools/tests/test_ha202610.py -q -s -p no:cacheprovider
"$work/venv/bin/python" -I tools/ha202610.py verify "$work"
```

The first command tests infrastructure controls with invented fixtures and plugin
autoload disabled. Ordinary integration tests retain genuine plugin loading:

```bash
timeout --signal=TERM --kill-after=5s 180s "$work/venv/bin/python" -m pytest \
  -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
```

Do not interpret a collection, import, sentinel, or partial log as a full pass.
For release qualification, settle the implementation, version, documentation and
assets before the combined independent review and final test run. Bind the evidence
to that final source SHA. Development commands above are not release acceptance.

Backend setup, reconfigure and Configure APIs have local synthetic execution evidence,
including complete original full/API runs on the accepted product implementation.
This recipe's newer Python archive and generated plugin build are distinct artifacts;
shared package versions alone do not make them identical to earlier local runtimes.
Genuine hosted .10 validation and final frontend rendering remain pending. HA .10
supplies the central `reconfigure_successful` abort text; the retained floor has local
`already_configured` fallback text. Available translation dictionaries do not prove
which text a frontend renders. Normal RestoreEntity and Voluptuous are exercised;
HA 2026.11 restore changes are outside this target. If your platform cannot run these
exact artifacts, use the README's locked local floor recipe and label the result
2024.12.0. A floor run does not qualify .10.

`build` and `install` are explicit continuation commands for an already prepared
owned directory: `build` requires the prepared Python/venv and no plugin clone;
`install` requires the checked generated plugin. They never recreate a completed
runtime or automatically retry a failed command. Existing log directories cause
refusal, so retain and identify any failed receipt before a deliberate continuation.

## Public inputs and generated outputs

`bundle.json` and the two matching locks identify actual artifacts, not broad
version ranges or a private registry. There are **157 public input artifacts**:
the stable core wheel, 150 other runtime dependencies, and six additional
build/generation tools. Adding the generated plugin gives the same **158**
distribution set used for this target's dependency setup. Of those, **152** belong
to the runtime/plugin closure. Seven tools are installed before generation;
`packaging` is both a build tool and a runtime dependency. This recipe also
installs **pip 26.2.1** from the exact Python archive's hashed ensurepip wheel,
so its final checked inventory is **159** distributions. No unconstrained pip
upgrade occurs.

The stable core wheel has version `2026.10.0`, requires Python `>=3.14.2`, and
SHA256 `1d6a7dc8e55216425f61915c095cad99b768679d9a829e58d2334333ba5d4446`.
The generator reads the official core commit
`6a811d3359c7b2076dc9e1cf900843a129c044af` through its real Git objects. The source
checkout is shallow and sparse: its only available tag is `2026.10.0`, and all
24 generator input paths are pinned by Git blob and SHA256. This bounds the
stock generator's selection; it is not a claim about all upstream tags.

The unchanged [upstream generation mechanism](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/aacf4b5e11e16da8f6ccc67fe3729441a51c282e/README.md)
comes from plugin commit `aacf4b5e11e16da8f6ccc67fe3729441a51c282e`.
The recipe runs its stock `generate_phacc/generate_phacc.py --regen` and
`setup.py sdist bdist_wheel` with pinned generation/build tools. Local source is
exposed through `PYTHONPATH` for the generator's import prerequisite, instead of
first installing the older beta-pinned editable package. Generator logic,
fixture transformations, requirements, and stock version are not patched.
All 27 generated source/output files and the wheel's metadata are checked.

**The generated plugin retains version 0.13.370, but it is not the public
0.13.370 release.** That public wheel requires `2026.10.0b4` and is refused.
The generated package must require exactly `homeassistant==2026.10.0` and its
modules must report the stable version. Never replace this recipe with a bare
plugin version pin or override a beta dependency conflict.

Wheel/sdist timestamps can change rebuilt archive bytes. The recipe freezes
source/output content and then hashes each **actually produced** archive in
`generated-artifact.json` and `actual-artifacts.json`. It writes `install.lock`
from those hashes and enforces them during an ordinary dependency-resolving
installation. A previous build's archive hash is never assigned to a new build.
The two sdist inputs (`mock-open`, `PyRIC`) are built offline with the pinned
installed backend; their resulting wheel hashes are recorded separately.
No upstream published-release equivalence is claimed for these local builds.

## Runner and evidence

The additional workflow uses the existing public GitHub-hosted **ubuntu-26.04**
x64 runner. [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [image inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md)
document this platform. The official
[Python release](https://github.com/actions/python-versions/releases/tag/3.14.8-36806082737)
provides `python-3.14.8-linux-26.04-x64.tar.gz`, asset 602297001, SHA256
`53eb0aed7bfb9a3d28b0842ada81dc7b1432fd697cb35653b79156b1efd0d3fa`.
The recipe verifies the full archive and its extracted files, and checks the
actual executable, stdlib, libpython, site-packages, distribution metadata,
unrelocated installed wheel members and imported modules. Ordinary and isolated-child
binding receipts are separate.

`bluetooth-data-tools`, `fnv-hash-fast`, and `ulid-transform` select
`cp314-cp314-manylinux_2_41_x86_64` wheels. Ubuntu 24.04's glibc 2.39 does not
satisfy those artifacts; the recipe refuses that substitution. Python's version
banner alone is not proof of archive, ABI, or runner identity.

CI refuses private repositories, non-hosted/wrong-architecture/wrong-OS
runners, reruns, tags, unsupported events, and the existing dedicated diagnostic
branches. Checkout credentials are not persisted. The source SHA/tree and all
tracked file hashes are recorded, followed by actual artifact/build/runtime
receipts. Sentinel and full-suite exits are saved independently of export.
Complete ordinary phases and bounded complete logs are required; missing,
skipped, failed, malformed, or oversize evidence fails visibly. Existing CI
continues to own its five job families and three earlier targets.

A local run does not prove genuine GitHub-hosted execution. Qualification needs
a new run attached to the independently reviewed candidate SHA; later product,
version, documentation, UI and release checks remain separate.
