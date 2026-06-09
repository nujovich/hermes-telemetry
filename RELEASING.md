# Releasing

How to cut a release of hermes-telemetry. Releases are tagged from `main`. The tag
must point at a commit that already has the version bumped and the CHANGELOG closed,
so order matters.

Replace `X.Y.Z` with the new version and `PREV` with the previous released version.

## Versioning

This project follows [Semantic Versioning](https://semver.org). Pre-1.0:

- **Minor** (`0.N.0`) — new, backward-compatible features (a new pricing source, a new
  slash command).
- **Patch** (`0.N.P`) — bug fixes with no new surface.

The release version is set by the highest-impact change in it: any new feature makes
the whole release a minor bump, even if it also ships fixes.

## Checklist

### 1. Pre-flight
- [ ] All PRs intended for this release are merged into `main`.
- [ ] `git checkout main && git pull` — on `main`, up to date.
- [ ] `git status` — working tree clean.

### 2. Bump the version
Update the version string everywhere it lives — they must all agree:
- [ ] `pyproject.toml` (`version = "X.Y.Z"`)
- [ ] `plugin.yaml` (`version:` field, if present)
- [ ] `__init__.py` (`__version__`, if present)

The classic release bug is a tag that says `X.Y.Z` while `pyproject.toml` was never
bumped. `grep -rn "PREV"` to confirm nothing is stale.

### 3. Close the CHANGELOG
- [ ] Promote `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` (today).
- [ ] Leave a fresh, empty `## [Unreleased]` section above it.
- [ ] Add/update the compare link at the bottom:
      `[X.Y.Z]: https://github.com/nujovich/hermes-telemetry/compare/vPREV...vX.Y.Z`
- [ ] Sanity-check entries: features under `Added`, fixes under `Fixed`, behavior
      changes under `Changed`. No duplicates from merged PRs.

### 4. Run the full suite (not just changed files)
- [ ] `pytest`
- [ ] `ruff check .`
- [ ] `ruff format --check .`

All green before tagging.

### 5. Commit the release
- [ ] `git commit -am "chore(release): X.Y.Z"` — version bump + CHANGELOG in one commit.

### 6. Tag and push
- [ ] `git tag -a vX.Y.Z -m "X.Y.Z"` — must point at the release commit from step 5.
- [ ] `git push origin main --tags`

### 7. Publish the GitHub release
- [ ] `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file -` and paste the
      `[X.Y.Z]` CHANGELOG section as notes.
- [ ] Confirm the issues this release closes show as closed.

### 8. Post-release
- [ ] Grep README and docs for stale version references and test counts (test counts
      change whenever tests are added or removed in a release).
- [ ] Note any known follow-ups in the release notes if relevant.
