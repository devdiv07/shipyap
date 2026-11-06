# Contributing to Shipyap

Use Python 3.12 or later and uv. Install the locked development environment with:

```powershell
uv sync --locked --extra dev
uv run --no-sync python -m pytest -q tests
uv run --no-sync shipyap eval --cases evals --profile public --check
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

Tests and public evaluation must run offline using synthetic fixtures; do not use real drafts,
channel identifiers, credentials or personal workspace paths. Explain the problem, the behavior
change, and the checks run in your pull request. Include tests for failure paths and safety boundaries.
Use small changes and preserve the separation between drafting, review and publishing. Do not run
paid requests or publish while testing. Report security issues privately as described in [SECURITY.md](SECURITY.md).
The Python package is `shipyap`; the `pros` command is an alias of the `shipyap` command.

## Maintainers: updating this repository

This repository is generated from the maintainer's private development repository; changes are
not made here directly. To publish a new version (Windows PowerShell, in the private repository):

1. Make sure the private repository has no uncommitted changes, and that your local clone of
   this repository is clean and up to date (`git pull`).
2. Run `.\scripts\publish_public.ps1 -PublicClone <path to your clone>`. It builds the public
   export in a fresh temporary folder, running every gate (leak scans, public replay, the full
   public test suite against its reviewed inventory); any failure stops it before the clone is
   touched. It then makes the clone's files match the export exactly (adding, updating and
   deleting files; `.git` and ignored files are left alone) and prints `git status` and
   `git diff --stat`.
3. The script never stages, commits or pushes. Review the changes in the clone, then commit and
   push them yourself.
