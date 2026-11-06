# SYNTHETIC public regression cases

Every source, claim, author statement, response and verdict here is independently hand-written
sample data about a fictional queue. No model was called and no upstream repository was copied.
The external-model generator kind exercises the model-response wire format; its label explicitly
identifies the handwritten fixture. Repair replies are scripted examples, not measured model quality.

Run `pros eval --cases evals --profile public --check`. This selects only this directory and
refuses `--workspace`. The default private profile remains the independent real-reply gate in
the dev repository. This directory can be copied and tested without the private files.

| Cases | Defect or invariant |
| --- | --- |
| normalisation | Narrow/split long references; discard an invalid path without losing valid support |
| invalid-core | Refuse a dropped main claim and an invalid item independently |
| valid-control, overlong-reference | Supported claims stay reviewable; repair a model-shaped oversized citation |
| missing-number, narrow-quote | Numbers absent from selected evidence block |
| outdated-option, outdated-status | Later change records supersede a plan and its dated status |
| author-angle | A backlog angle is not the author's own statement |
| tool-text-short | Tool provenance cannot become post text; dropping it can shelve a short draft |
| warning-control, contradicted | Model warnings do not block; a contradiction does |
| known-hash, hash-aside | A known label is not a number claim; an unsupported aside is removed |
| missing-scope | Append the evidence's own scope statement verbatim |
| fixture-repair | Script one narrowing rewrite and re-check through the existing repair loop |

Expectations are explicit and checked in. Missing expected cases fail the gate. The private
release audit scans every byte here for personal markers using its shared policy.
