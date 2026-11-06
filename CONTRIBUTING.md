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
The internal package name remains `public_research_os`; `pros` is an alias of `shipyap`.
