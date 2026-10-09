# Releasing

A release is cut by hand, in this order. Don't tag until every step above the tag is done.

1. **Version bump.** Set `"version"` in
   `custom_components/mxz_coordinator/manifest.json` to the new version. In the same
   commit, update the README's "What's new" section and add a section to
   [MIGRATION.md](MIGRATION.md) when behavior or options change.
2. **Full suite and ruff.** On each lane you can run, from the checkout:

   ```bash
   python -m pytest -p tools.pytest_phases tests/ -q -s -p no:cacheprovider
   ruff check --no-cache custom_components/ tests/ tools/
   ```

   The suite must end `PHASES_VALID=true` with exit 0, and ruff must report no errors.
   CI pins `ruff==0.16.0`.
3. **CI green on main.** Push the release commit to `main` and wait until every job in
   `CI` and `HA 2026.10 qualification` passes on that exact commit.
4. **Credits.** This is a manual step before tagging. It does **not** run in CI, because
   CI has no `gh` token for reading issues. With an authenticated `gh`:

   ```bash
   python3 tools/credits_check.py            # since the latest tag
   python3 tools/credits_check.py --since v3.4.0
   ```

   It lists every outside commit author, `Co-authored-by` co-author and issue or PR
   author (closed or merged since the tag) and marks each as credited or MISSING. It
   skips the owner, agents and bots. It exits 0 when everyone is credited, 1 when
   anyone is missing and 2 on a git, `gh` or README error. A commit whose email has no
   GitHub account shows up as `name <email>`. Find out who wrote it before you credit
   them.

   For every MISSING contributor, add them to the README's
   [Credits & prior art](../README.md#credits--prior-art) section:
   - Every handle, issue, PR and forum mention is a full link.
   - Note the release the contribution shipped in.
   - Credit work that hasn't shipped yet as "in progress".

   Rerun until it exits 0. If someone reported the problem on the Home Assistant forum,
   add a line naming them to the release notes too.
5. **Release notes.** Write them for users: what changed, what it means for their heads,
   how it was tested, anything still pending, and upgrade notes with links into
   MIGRATION.md.
6. **Tag.** Create an annotated tag on the green commit and push it:

   ```bash
   git tag -a vX.Y.Z -m vX.Y.Z
   git push origin vX.Y.Z
   ```

7. **Publish.** Create the GitHub release from that tag with the notes:

   ```bash
   gh release create vX.Y.Z --title vX.Y.Z --notes-file notes.md
   ```
