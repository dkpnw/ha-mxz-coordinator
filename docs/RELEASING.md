# Releasing

## Release cadence

- **Batch releases.** Merging to `main` doesn't mean releasing. Let finished changes
  gather under "Unreleased" in the [CHANGELOG](../CHANGELOG.md) and release them together,
  not one release per merge.
- **Betas are GitHub pre-releases.** A beta build is published with `--prerelease` (step 8),
  so it isn't marked as the latest release. See [Cutting a beta](#cutting-a-beta).
- **Every release has two gates, beta or not:** a house hardware check on the Mitsubishi
  heads, then the owner's one-tap OK (step 6). Passing CI replaces neither.

## Cutting a release

A release is cut by hand, in this order. Don't tag until every step above the tag is done.

1. **Version bump.** Set `"version"` in
   `custom_components/mxz_coordinator/manifest.json` to the new version. In the same
   commit, add the release to [CHANGELOG.md](../CHANGELOG.md), update the README's short
   "What's new" section, and add a section to
   [MIGRATION.md](MIGRATION.md) when behavior or options change.
2. **Full suite and ruff.** On each lane you can run, from the checkout:

   ```bash
   python -m pytest -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
   FAKE_UPTIME=120 python -m pytest -p tools.fakeboot -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
   ruff check --no-cache custom_components/ tests/ tools/
   ```

   Both suite runs must end `PHASES_VALID=true` with exit 0, and ruff must report no
   errors. The second runs on a clock that reads as if the host had booted 120 s ago
   (`tools/fakeboot.py`) and prints `FAKEBOOT_UPTIME=120` once.
   CI pins `ruff==0.17.4`.
3. **CI green on main.** Push the release commit to `main` and wait until every job in
   `CI` and `HA 2026.10 qualification` passes on that exact commit.
4. **Credits.** This is a manual step before tagging. It does **not** run in CI, because
   CI has no `gh` token for reading issues. With an authenticated `gh`:

   ```bash
   python3 tools/credits_check.py            # since the latest tag
   python3 tools/credits_check.py --since v3.4.0
   ```

   It lists every outside commit author, `Co-authored-by` co-author, and issue or PR
   author (closed or merged since the tag) and marks each as credited or MISSING. It
   skips the owner, agents, and bots. It exits 0 when everyone is credited, 1 when
   anyone is missing, and 2 on a git, `gh`, or README error. A commit whose email has no
   GitHub account shows up as `name <email>`. A co-author's email is matched to a login
   by its GitHub noreply form, the small map in `tools/credits_check.py`
   (`KNOWN_LOGINS`), or a commit of their own in the range; one that none of these
   matches shows up as `name <email>` too. Find out who wrote it before you credit
   them, and add their email to the map only once you know their login.

   For every MISSING contributor, add them to the README's
   [Credits & prior art](../README.md#credits--prior-art) section:
   - Every handle, issue, PR, and forum mention is a full link.
   - Note the release the contribution shipped in.
   - Credit work that hasn't shipped yet as "in progress".

   Rerun until it exits 0. If someone reported the problem on the Home Assistant forum,
   add a line naming them to the release notes too.
5. **Release notes.** Write them for users: what changed, what it means for their heads,
   how it was tested, anything still pending, and upgrade notes with links into
   MIGRATION.md.
6. **House hardware check and owner's OK.** Run the release commit on the house's Mitsubishi
   heads and check them. Then get the owner's one-tap OK for this release.
7. **Tag.** Create an annotated tag on the green commit and push it:

   ```bash
   git tag -a vX.Y.Z -m vX.Y.Z
   git push origin vX.Y.Z
   ```

8. **Publish.** Create the GitHub release from that tag with the notes:

   ```bash
   gh release create vX.Y.Z --title vX.Y.Z --notes-file notes.md
   ```

   For a beta, add `--prerelease`.

## Cutting a beta

A beta follows the steps above, including both gates, with these differences:

| | Release | Beta |
| --- | --- | --- |
| `version` in `manifest.json` | `3.21.0` | `3.21.0b1`: `X.Y.ZbN`, with no dash, and `N` counting from 1 |
| Tag | `v3.21.0` | `v3.21.0b1` |
| GitHub release | `gh release create v3.21.0 ...` | `gh release create v3.21.0b1 --prerelease ...` |
| CHANGELOG | Its own section | Stays under "Unreleased": no section per beta |
| README "What's new", MIGRATION | Updated | Wait for the release |

Before tagging, check the tag is the manifest's version:

```bash
test "v$(python3 -c 'import json; print(json.load(open("custom_components/mxz_coordinator/manifest.json"))["version"])')" = v3.21.0b1
```

When the betas are done, set `version` to `3.21.0` and cut the release as usual. Its
CHANGELOG section takes what's under "Unreleased". A beta is older than its release, so
testers on a beta are offered the release when it comes out.

### The guard

The `release-provenance` workflow refuses to build or attach the zip unless all of these
hold, and fails visibly if they don't:

- The tag is `v` plus the `version` in `manifest.json` at that tag.
- The version is `X.Y.Z` or `X.Y.ZbN`.
- The release is marked pre-release exactly when the version has `bN`.

It runs after publishing, because that's what starts the workflow, so a failure means a
wrong release is already public. Delete that release and its tag, then cut it again. A tag
from before this format (`v3.0.0-beta.19`) fails the guard if the workflow is run on it by
hand.

### How testers opt in

HACS offers a pre-release only to someone who asks for it:

1. In **Settings > Devices & services > Entities**, turn on the **Pre-release** switch of
   the MXZ Coordinator repository's HACS entry. HACS keeps this switch disabled until
   you enable it. If your HACS shows a "Show beta versions" option for the repository
   instead, turn that on.
2. MXZ's update entity then offers the newest pre-release, and installs like any update.
3. To leave the betas, turn the switch off. HACS goes back to offering full releases only,
   and the next full release that's newer than the beta installs as usual.

The source for steps 1 and 2 is HACS's
[switch entity docs](https://github.com/hacs/documentation/blob/main/source/docs/use/entities/switch.md).
A pre-release never reaches anyone who hasn't turned this on. `hacs.json` doesn't change
for a beta.

## Verifying a release download

From 3.18.0 on, each release has `mxz_coordinator.zip` (the `custom_components/mxz_coordinator` folder at that tag) with a GitHub build-provenance attestation. The `release-provenance` workflow builds and attaches it; it never creates releases or tags. To check a download:

```sh
gh release download v3.18.0 -R dkpnw/ha-mxz-coordinator -p mxz_coordinator.zip
gh attestation verify mxz_coordinator.zip -R dkpnw/ha-mxz-coordinator
```

The zip is built with `git archive` from the tag, so `git archive --format=zip --mtime="$(git log -1 --format=%cI v3.18.0)" v3.18.0:custom_components/mxz_coordinator` gives the same bytes on a recent git. HACS still installs from the repository as before. Tags aren't signed yet.
