# Contributing to Shipyap

Thanks for helping. Small fixes are welcome: a clearer message, a missing test, a docs
correction. [docs/good-first-issues.md](docs/good-first-issues.md) lists real starting points.

## How a pull request gets in

This repository is published from the maintainer's private development repository. Every
release replaces the files here with a fresh export. A pull request merged here directly would be
erased by the next release, so changes come in like this:

1. **Open your pull request on this public repository** (the one you cloned for the demo).
   Explain the problem, what changes, and which checks you ran.
2. **The maintainer reviews it there.** Discussion and requested changes happen on the pull
   request as usual.
3. **The maintainer ports it, with credit.** Once accepted, your change is copied into the
   private repository as its own commit with a `Co-authored-by: Your Name <your email>` trailer.
   Your pull request is then merged or closed with a link to the release that carries it.
4. **It ships in the next release.** Your change appears in this repository inside the release
   commit (for example "Shipyap v0.1.6"), not as a separate commit, and GitHub credits you as a
   co-author of that commit.

The trailer uses the name and email of your pull request's commits. If you prefer your GitHub
noreply address (`ID+username@users.noreply.github.com`), say so in the pull request.

## Run the tests

Use Python 3.12 or later and [uv](https://docs.astral.sh/uv/). Everything below is offline and
free: no API key, no account, no network after the first install.

```powershell
uv sync --locked --extra dev
uv run --no-sync python -m pytest -q tests
uv run --no-sync shipyap eval --cases evals --profile public --check
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

If you add, rename or remove a test, CI also compares the test list against
`scripts/expected_test_inventory.json`; mention it in the pull request and the maintainer will
update the inventory when porting.

## Try the demo

```powershell
.\scripts\demo.ps1
```

or, without opening a browser, `uv run --locked --no-dev shipyap demo`. It is fictional, offline
and needs no key. See the README for what to click.

## Ground rules

- Tests and evaluation use synthetic fixtures only: no real drafts, channel ids, credentials or
  personal paths.
- Never run paid requests (`--allow-paid`) or publish to Buffer while testing.
- Add tests for the failure path and the safety boundary, not just the happy path.
- Keep changes small and keep drafting, review and publishing separate (see
  [docs/architecture.md](docs/architecture.md)).
- Report security issues privately as described in [SECURITY.md](SECURITY.md).

The Python package is `shipyap`; the `pros` command is an alias of the `shipyap` command.

## Maintainers: publishing a release

In the private repository (Windows PowerShell):

1. Port every public commit made since the last release: one private commit per accepted pull
   request, with the `Co-authored-by:` trailer. Add `<public sha> <private sha>` for each public
   commit (merge commits too) to `docs/ported-public-commits.txt`.
2. Check: `.\scripts\publish_public.ps1 -PublicClone <clone> -DryRun`. It fetches the clone and
   lists every commit on `origin/main` after the newest `v*` tag. Any commit not in
   `docs/ported-public-commits.txt` is refused and printed; nothing is changed.
3. Publish: run it again without `-DryRun`. It repeats the guard, builds the export with every
   gate (leak scans, public replay, the public test suite against its inventory), then makes the
   clone match the export and prints `git status` and `git diff --stat`.
4. In the clone, commit as `Shipyap vX.Y.Z`, tag that commit `vX.Y.Z` (or `vX.Y.Z-N` for a
   republish without a version bump) and push the commit and the tag. **The tag is the publish
   marker**: an untagged publish commit is reported as unported by the next run.
