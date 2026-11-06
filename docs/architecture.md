# Architecture baseline (ADR candidate)

## Scope
Personal alpha on one workspace. Future product for independent engineering teams and individuals. **The current scaffold implements data contracts, offline evidence checks, one read-only GitHub reader, offline draft checking, one operator-triggered model drafting command and default-deny publishing.** It has no API server, UI, active queue, external account or true multi-tenant isolation; the only LLM access is `draft-model`, one request per run with the operator's own API key.

## Data flow

```
Allowlisted public artifacts -> pinned snapshot (first slice: `ingest-github`; other sources future)
  -> CandidateClaim -> human-reviewed ApprovedClaim (workflow future)
  -> constrained editorial draft (`draft-brief` + external drafter + `draft-build`, or `draft-model`
     with one Anthropic request and the same checks; UNAPPROVED)
  -> deterministic checks / semantic UNKNOWN
  -> immutable ContentPackage -> authenticated approval (future)
  -> trusted publisher (future) -> Buffer GraphQL (future)
  -> reconciliation (future) -> reporting
```

## Trust boundaries
1. Untrusted research documents may be read as DATA but never executed or treated as instructions.
2. Drafter never receives social credentials. Runtime separation, not prompting, enforces this later.
3. Hash is content integrity only; **approval identity** requires a separate authenticated process.
4. Publisher must be deny-by-default for missing/mismatched approval, wrong workspace, changed media, expired intent, invalid channel, or ambiguous provider outcome.
5. No exact-once guarantee can be made without a provider idempotency mechanism; unknown outcomes require reconciliation and sometimes human intervention.
6. Public proof pages must distinguish self-reviewed claims from independent verification; hashing data alone is not evidence of truth.

## Domain packages
- `evidence`: Pydantic records; strict numeric/provenance checks.
- `editorial`: offline LinkedIn authoring (Assignment 03). `draft-brief` turns a COMPLETE snapshot and an author contribution into a deterministic brief. An external drafter returns a `DraftResponse` (the provider boundary; no model is called and no credential is read). `draft-build` maps every segment to source lines or contributor items, applies deterministic checks (located quotes, numbers, first person, comparisons, hashtags, links) and writes the post, a source-to-assertion map and `review.md`. Every bundle is UNAPPROVED with semantic support UNKNOWN, and `NO_POST` is a valid outcome. Bundles are published through the A02 staging writer (`ingestion.writer.publish_directory`). Editorial imports nothing from `publication`. Later: other channels, anti-slop evaluation against a labelled set.
- `drafting`: `draft-model` (Assignment 04). Verifies inputs offline exactly as `draft-build` does and requires `brief.md` to equal the rendering of the verified `brief.json`. Then it asks the Anthropic Messages API to count tokens (no generation), refuses if the estimated maximum cost exceeds `--max-cost-usd`, and sends exactly one generation request (`claude-opus-5-5` medium effort by default, `claude-sonnet-5-5` allowed; structured JSON output; no retries, no fallback). The model writes only the editorial fields; the tool adds `brief_sha256`, `response_version` and the `generator` label, and passes the bytes to `editorial.service.build_from_response`, the same path as `draft-build`. `drafting/provider.py` is the only module that imports the SDK or reads the environment (the API key, `PROS_ANTHROPIC_API_KEY`). Offline commands and `--dry-run` never load the SDK.
- `publication`: package schema, cryptographic fingerprint and fail-closed port; no provider calls in Phase 0.
- `ingestion`: one read-only GitHub reader (`ingest-github`): one allowlisted public repository at one pinned commit, 1-3 files, a deterministic v1 `SourceManifest` and a staged, verified snapshot. Network code is confined to `ingestion/transport.py` (and, for the model provider, the SDK in `drafting/provider.py`). Sensitive-data screening and other sources are later; downloaded content is never executed.

## Multi-user migration
`workspace_id` appears in entities immediately. This is **not** authorization. Before beta: authenticated accounts, workspace membership validation on every API/DB operation, per-workspace encryption/scoping, OAuth token encryption and refresh locking, quotas and isolation regression tests. Do not deploy as a multi-user service with only the current fields.

## Decision boundary
Phase 0: standard library + Pydantic schema tests. Phase 1: source snapshots and claim review. Phase 2: editorial engine and proofs. Phase 3: trusted authorizer and Buffer feeder, implemented only after live API feasibility and authorization design. See roadmap.
