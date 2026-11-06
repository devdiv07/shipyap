# Shipyap working agreement

Read README.md before setup or changes. Use Git, Python 3.12 or later, and uv; run commands from
the repository root. Use the locked dependencies. For setup, run:

```text
uv run --locked --no-dev shipyap init --workspace "<path>" --repo "<owner/name>"
```

Read the generated pros.toml. Preserve existing workspace files and data.
The owner supplies credentials and decides whether to run paid generation or publishing.

Never ask for, read, print or commit API keys, private drafts or personal data. Do not make paid
calls, approve, schedule or publish while setting up or testing. Use synthetic data in tests and
examples. Model judgments and matching citations are not proof that a claim is true.

For changes, run `uv sync --locked --extra dev`, `uv run --no-sync python -m pytest -q tests`,
`uv run --no-sync shipyap eval --cases evals --profile public --check`, and the Ruff lint and format
checks documented in CONTRIBUTING.md. Preserve the separation between drafting, review and
publishing. Report uncertainties, validation results and security issues clearly.
