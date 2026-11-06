# Releases

How a version bump becomes a tagged GitHub release, and why the workflow is built this way.

Bump `version` in `pyproject.toml`, run `uv lock`, and merge that through a pull
request. `.github/workflows/release.yml` waits for CI to go green on that commit, then
tags `v<version>` and publishes a GitHub release with generated notes. Nothing
else is a trigger — not a tag you push, not an edit to `pyproject.toml` that
leaves the version alone.

The `uv lock` is not optional and it fails in the quiet direction. `uv.lock`
records the project's own version alongside every dependency, so a bare bump
makes `uv sync --locked` exit with *"The lockfile at `uv.lock` needs to be
updated"* in all six CI jobs — and a red CI run emits no `workflow_run` success,
so the release does not fail, it simply never happens.

The gate asks *"does the version at this commit have a tag yet?"*, not *"did this
push change pyproject.toml?"*. Every diff-shaped alternative breaks on some push
shape and breaks silently: `HEAD~1` misses a bump in the middle commit of a
multi-commit push, and `github.event.before` is unreachable after a force-push and
is replayed verbatim on a re-run. Asking about the tag is correct on all of them,
and makes re-runs, reverts, and no-op edits no-ops for one reason rather than three.

It is a separate workflow rather than a third job in `ci.yml` so that file keeps
`permissions: contents: read` and the README badge keeps reporting lint, types and
tests rather than release outcomes. The cost is `workflow_run`, whose edges are
recorded in the file: `github.sha` is the branch tip rather than the commit CI
tested, the workflow only ever runs from main's copy of itself, and a tag pushed
with `GITHUB_TOKEN` triggers no further workflows.

Releases carry no build artifacts. The reason recorded here — that an installed
wheel resolved its workspace to `<venv>/lib` and loaded no skills — is fixed: the
workspace ships inside the package and CI's `deploy-shape` job asserts an
installed copy resolves its skills. Attaching a wheel is now a decision rather
than a blocked one, and publishing to PyPI remains separate: the name is
unclaimed, a first upload takes it permanently, and README documents a
checkout-and-`uv run` workflow that nobody has tested as an installed package.

If you delete a release but leave its tag, the gate will say "already released"
for good; the workflow's `workflow_dispatch` trigger is the recovery path.
