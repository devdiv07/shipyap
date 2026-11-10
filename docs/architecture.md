# Architecture

Shipyap is a single-owner, local tool. One command, `shipyap` (alias `pros`), drives everything:
`init`, `repo`, `run`, `review`, `eval`, `demo` and `publication migrate`. Code lives in
`src/shipyap`; each package below is one stage. Nothing runs in the background except an optional
Windows scheduled task that calls `shipyap run`.

## Stages

**Configuration** (`automation/config.py`, `automation/initialise.py`, `preferences/`). `init`
creates a workspace and a secret-free `pros.toml` without overwriting anything. The config holds
paths, the model, `max_cost_usd`, `weekly_draft_cap`, channel ids, posting slots and optional
author statements. `repo set` records each repository as `focus`, `occasional` or `off`; a
repository never set is never read.

**Selection and ingestion** (`automation/selection.py`, `ingestion/`). For each enabled repository
the run asks GitHub (public, unauthenticated) for the current revision and picks a bounded set of
files by heuristic: plans, later records, code and tests. Ingestion fetches those files at that
one pinned commit, checks every returned id against what was asked, and publishes a snapshot
(manifest with SHA-256 digests plus a completion record) through a staging directory and a single
rename. Commit messages never become evidence.

**Story discovery** (`drafting/story_prompt.py`, `drafting/story_service.py`, `drafting/normalise.py`,
`stories/`). A model reads a bounded, line-numbered slice of the snapshot and proposes story
findings. The normaliser only narrows or drops bad references; each finding must then cite real
files, real line ranges and quotes found there, with every number inside the quote. Passing
findings enter the story backlog as `available`. The raw reply is kept in the automation state
so an interrupted run can recover it without paying again; it is never evidence by itself.

**Drafting** (`editorial/`, `drafting/story_draft.py`, `drafting/service.py`). The top-ranked
available story becomes a deterministic brief (snapshot text, author statements, rules). The
model writes one LinkedIn draft; the tool adds the brief hash and generator label itself. The
editorial checks in `editorial/grounding.py` locate every cited quote, check numbers, first
person, comparisons, hashtags and links, and `editorial/supersession.py` flags sentences that
repeat a plan a later record overrode. The story is marked `offered` only after its draft bundle
and link exist.

**Fact check and repair** (`automation/factcheck.py`, `automation/repair.py`). A second model reads
all snapshot files and labels each sentence SUPPORTED, OUTDATED, CONTRADICTED, OVERSTATED or
UNVERIFIABLE. Repair first applies free deterministic fixes (drop uncited tool text or an
unsupported hex id aside, append the record's own scope line), then up to a fixed number of
model rounds that may only narrow or remove claims. A draft that is still not clean is shelved and
never shown for approval.

**Review** (`review/`, `automation/review.py`). `shipyap review` serves plain HTML forms on
127.0.0.1 only. The owner reads flagged sentences with their cited lines, edits (re-checked by the
same editorial checks), approves or rejects. `review/gate.py` decides what blocks Approve: a
blocking editorial finding, an OUTDATED sentence, or a CONTRADICTED verdict. The X text is derived
from the approved LinkedIn draft by `review/x_post.py`, words only from the draft.

**Publication** (`publication/`). An approval binds the exact text, channel id and UTC time.
Scheduling is a separate confirmation on an approved draft; `publication/service.py` writes a
send intent to the ledger before `publication/buffer.py` makes one Buffer request, never retried.
After each run, `automation/posted.py` reads back unfinished posts (read-only).

**Evaluation** (`automation/evaluation.py`, `evals/`). `shipyap eval` replays stored model replies
through the current checks, offline and free; `--check` fails when results drift from
`evals/expected.json`. `shipyap demo` serves a synthetic workspace with scheduling disabled.

## Safety gates

- **Paid calls** need `--allow-paid` on each run. Every model request is token-counted first and
  refused if the run's reservation would pass `max_cost_usd`; `weekly_draft_cap` stops a run
  before anything is read. Nothing is retried, and an uncertain call is recorded, not repeated.
- **Credentials** are read in two places only: `drafting/provider.py` (`PROS_ANTHROPIC_API_KEY`)
  and `publication/buffer.py` (`PROS_BUFFER_API_KEY`, `PUBLISHING_DISABLED`). Tests enforce it.
- **Network** code is confined to `ingestion/transport.py` (fixed host `api.github.com`),
  `drafting/provider.py` (the Anthropic SDK) and `publication/buffer.py` (fixed host
  `api.buffer.com`). No redirects are followed.
- **Approval** is the owner's explicit confirmation on the page, bound to a server-held ticket.
  READY_FOR_REVIEW, zero warnings or a matching digest are never approval, and an approval is not
  authentication.
- **Scheduling** is off unless `PUBLISHING_DISABLED=false` and the Buffer key are both present
  when `review` starts. The ledger allows one send intent per approval; a story already submitted
  needs a typed Repost.
- **STOP**: a file named `STOP` in the workspace root stops runs, approval and scheduling. It
  does not cancel anything already sent to Buffer.
- Repository text is untrusted data. It is never executed, and errors carry fixed codes, never
  input text, paths or keys.

## Where state lives

Everything is in the workspace folder chosen at `init` (paths set in `pros.toml`):

| Path | Holds |
| --- | --- |
| `repository-preferences.sqlite` | repository modes (`focus`, `occasional`, `off`) |
| `story-backlog.sqlite` | stories, their evidence observations, `available`/`offered` |
| `snapshots/` | pinned, verified repository snapshots |
| `drafts/` | brief and draft bundles, story links, repair records |
| `automation/automation.sqlite` | per-repository cursors, generation attempts and replies, spending |
| `automation/run-lock.sqlite` | the lock held for one run |
| `automation/review-ledger.sqlite` | approvals, review decisions, send intents (schema 3) |
| `automation/fact-checks/` | fact-check verdicts, one JSON file per story |

The ledger is append-only for approvals and intents. An older ledger is upgraded only by
`shipyap publication migrate --apply`; without `--apply` it previews a copy. `workspace_id` is a
label, not access control.
