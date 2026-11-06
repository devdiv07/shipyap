"""Rebuild independently written SYNTHETIC fixtures. No private inputs, model or network.

Run from the repository root. Expectations are written explicitly, never inferred from a run.
Existing snapshot directories must already have the exact intended bytes.
"""

import copy
import hashlib
import json
from pathlib import Path

from shipyap.editorial.brief import build_brief, render_brief_json
from shipyap.editorial.models import (
    NOT_LEARNED,
    PROVENANCE,
    AuthorContribution,
)
from shipyap.editorial.sources import load_verified_snapshot
from shipyap.evidence.report import canonical_json
from shipyap.ingestion.assemble import build_plan
from shipyap.ingestion.github import FetchedFile, FetchedSnapshot
from shipyap.ingestion.request import IngestionRequest
from shipyap.ingestion.writer import publish_snapshot

ROOT = Path(__file__).resolve().parents[1] / "evals" / "public"
MODEL = "claude-sonnet-5-5"
TEXTS = (
    "The toy queue recovered 3/10 jobs in the supplied exercise. Its log records the outcome "
    "for each job separately, so a reader can distinguish a completed action from a response "
    "that merely reports that action as complete.",
    "Retries are capped at 2 per job in this exercise. A worker consults the saved journal "
    "before sending another request, and the journal entry records the attempted action as "
    "well as the response that was actually observed by the worker.",
    "A worker writes an intent before contacting the toy service. When a response is lost, "
    "the saved intent remains available to the next worker; the later worker reads that "
    "record instead of treating silence as a completed response.",
    "The caller reuses the job token when retrying an unchanged action. A different action "
    "requires a different token, and the exercise keeps the original journal entry available "
    "so that a reviewer can compare the attempted actions directly.",
)
SCOPE = "Scope: this is synthetic evidence only and does not measure production reliability."
PLAN = "docs/EXPERIMENT_PROTOCOL.md"
CHANGE = "evidence/PROTOCOL_DEVIATION_001.md"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def ref(path: str, start: int, end: int | None = None, quote: str | None = None) -> dict:
    return {"path": path, "line_start": start, "line_end": end or start, "quote": quote}


def segment(sid: str, text: str, refs=(), *, basis="SOURCE", authors=()) -> dict:
    return {
        "id": sid,
        "text": text,
        "basis": basis,
        "source_refs": list(refs),
        "author_refs": list(authors),
        "paragraph_break_before": sid in ("S03", "S05"),
    }


def main() -> None:
    lines = ["# SYNTHETIC retry exercise", ""]
    for text in TEXTS:
        lines += [text, ""]
    lines += [SCOPE, "", "The recorded adapter label is 1234abcd.", ""]
    lines += [
        f"Synthetic note {index}: no production result is asserted." for index in range(1, 12)
    ]
    files = {
        "README.md": ("\n".join(lines) + "\n").encode(),
        PLAN: b"# Synthetic protocol\n\nStatus: no live requests have been made.\n"
        b"The protocol specifies jitter for toy request spacing.\n",
        CHANGE: b"# Correction: jitter removed\n\nThe `jitter` option was removed before the next toy run.\n",
    }
    request = IngestionRequest(
        request_version="1",
        workspace_id="synthetic_workspace",
        owner="sample-owner",
        repo="retry-lab",
        revision_sha="c" * 40,
        paths=tuple(files),
    )
    fetched = FetchedSnapshot(
        commit_sha="c" * 40,
        tree_sha="d" * 40,
        files=tuple(
            FetchedFile(
                path=p,
                mode="100644",
                blob_sha=hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
                size=len(data),
                data=data,
            )
            for p, data in files.items()
        ),
    )
    snapshot_dir = ROOT / "snapshots" / "retry-lab"
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot_dir.exists():
        publish_snapshot(snapshot_dir, build_plan(request, fetched))
    snapshot = load_verified_snapshot(snapshot_dir)
    assert {s.path: "\n".join(s.lines) + "\n" for s in snapshot.sources} == {
        p: data.decode() for p, data in files.items()
    }, "existing synthetic snapshot differs"
    contribution = AuthorContribution(
        contribution_version="1",
        workspace_id="synthetic_workspace",
        author_name="Sample Author",
        relationship="primary_author",
        channel="linkedin",
        audience="Readers of synthetic examples",
        insight="Unverified backlog angle: the toy journal separates an attempt from a response.",
        attempted=("I built a fictional queue to explain journaled retries.",),
        learned=(NOT_LEARNED,),
        why_it_matters=(
            "Unverified backlog support: a missing response leaves an unresolved attempt.",
        ),
        limitations=(
            PROVENANCE,
            "Cited evidence at revision " + "c" * 40 + ": README.md:3-9.",
            "Only the author's own items (attempted, learned) are personal statements. "
            "The angle, its support and every result must come from the cited source lines.",
        ),
        do_not_claim=("Real-world reliability or adoption.",),
    )
    brief = build_brief(
        snapshot,
        contribution,
        hashlib.sha256(canonical_json(contribution.model_dump(mode="json")).encode()).hexdigest(),
    )
    brief_bytes = render_brief_json(brief)
    (ROOT / "drafts").mkdir(parents=True, exist_ok=True)
    (ROOT / "drafts" / "retry.brief.json").write_bytes(brief_bytes)
    base = {
        "response_version": "1",
        "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
        "outcome": "DRAFT",
        "no_post_reason": None,
        "generator": {
            "kind": "external_model",
            "label": "SYNTHETIC hand-authored model-shaped fixture",
        },
        "segments": [
            segment(f"S{i:02d}", text, [ref("README.md", 2 * i + 1)])
            for i, text in enumerate(TEXTS, 1)
        ]
        + [segment("S05", SCOPE, [ref("README.md", 11)])],
        "verification_needed": [],
    }
    cases = []
    expected = {
        "stories": {"normalisation": {"min_accepted": 3}, "invalid-core": {"min_accepted": 0}},
        "drafts": {},
        "max_false_alarms": 0,
        "repair_totals": {"min_clean": 3},
    }

    def draft(name, edit=None, *, want=None, verdicts=None, fixture=None, control=False):
        response = copy.deepcopy(base)
        if edit:
            edit(response)
        write(ROOT / "drafts" / f"{name}.response.json", response)
        entry = {
            "name": name,
            "brief": "drafts/retry.brief.json",
            "response": f"drafts/{name}.response.json",
        }
        if control:
            entry["control"] = True
        if verdicts:
            entry["fact_check"] = f"drafts/{name}.fact-check.json"
            write(ROOT / entry["fact_check"], {"draft": name, "verdicts": verdicts})
        if fixture:
            entry["repair_fixture"] = f"drafts/{name}.repair-fixture.json"
            write(ROOT / entry["repair_fixture"], fixture)
        cases.append(entry)
        expected["drafts"][name] = want or {"status": "READY_FOR_REVIEW", "approvable": True}

    def blocked(sid, code, status="NEEDS_REVISION"):
        return {"status": status, "approvable": False, "blocked": {sid: code}}

    def replace(response, index, **fields):
        response["segments"][index].update(fields)

    draft("valid-control", control=True)
    draft(
        "missing-number",
        lambda r: replace(r, 0, text=TEXTS[0].replace("3/10", "9/10")),
        want=blocked("S01", "NUMBER_NOT_IN_EVIDENCE"),
    )
    draft(
        "narrow-quote",
        lambda r: replace(
            r, 0, source_refs=[ref("README.md", 3, quote="Its log records the outcome")]
        ),
        want=blocked("S01", "NUMBER_NOT_IN_EVIDENCE"),
    )
    draft(
        "overlong-reference",
        lambda r: replace(r, 2, source_refs=[ref("README.md", 1, 24, TEXTS[2])]),
        control=True,
    )
    for name, line, text in (
        ("outdated-option", 4, "The protocol specifies jitter for toy request spacing."),
        ("outdated-status", 3, "The protocol's status says no live requests have been made."),
    ):
        draft(
            name,
            lambda r, n=line, t=text: replace(r, 0, text=t, source_refs=[ref(PLAN, n)]),
            want={"status": "READY_FOR_REVIEW", "approvable": False, "outdated": {"S01": CHANGE}},
        )
    draft(
        "author-angle",
        lambda r: replace(
            r,
            3,
            text="I learned that the journal proves the action completed.",
            basis="AUTHOR",
            source_refs=[],
            author_refs=["insight"],
        ),
        want=blocked("S04", "AUTHOR_NOT_STATEMENT"),
    )

    def leaked(response):
        response["segments"] = [
            response["segments"][0],
            segment(
                "S04",
                "I learned that the journal proves the action completed.",
                basis="AUTHOR",
                authors=["insight"],
            ),
            segment(
                "S06",
                "The story backlog says semantic support has not been verified.",
                basis="AUTHOR",
                authors=["limitations.1"],
            ),
        ]

    draft(
        "tool-text-short",
        leaked,
        want={
            **blocked("S06", "TOOL_PHRASE"),
            "repair": {"status": "SHELVED", "approvable": False, "dropped": ["S06"]},
        },
    )

    def verdict(sid, value):
        return {
            "segment_id": sid,
            "verdict": value,
            "reason": "SYNTHETIC planted judgment.",
            "refs": ["README.md:5-5"],
        }

    draft(
        "warning-control",
        verdicts=[verdict("S02", "OVERSTATED"), verdict("S03", "UNVERIFIABLE")],
        control=True,
    )
    draft(
        "contradicted",
        verdicts=[verdict("S02", "CONTRADICTED")],
        want=blocked("S02", "FACT_CHECK_CONTRADICTED", "READY_FOR_REVIEW"),
    )
    draft(
        "known-hash",
        lambda r: replace(r, 0, text=TEXTS[0] + " Adapter label: 1234abcd."),
        control=True,
    )
    draft(
        "hash-aside",
        lambda r: replace(r, 0, text=TEXTS[0] + " (1235abcd)"),
        want={
            **blocked("S01", "NUMBER_NOT_IN_EVIDENCE"),
            "repair": {"status": "CLEAN", "approvable": True},
        },
    )
    draft(
        "missing-scope",
        lambda r: r["segments"].pop(),
        want={
            "status": "READY_FOR_REVIEW",
            "approvable": True,
            "repair": {"status": "CLEAN", "approvable": True, "cites": ["README.md:11-11"]},
        },
    )
    corrected = TEXTS[1].split(". ")[0] + "."
    fixture = {
        "fixture": "SYNTHETIC",
        "about": "Handwritten narrowing and re-check; never a live model result.",
        "replies": [
            {
                "stop_reason": "end_turn",
                "texts": [json.dumps({"rewrites": [{"segment_id": "S02", "text": corrected}]})],
            },
            {
                "stop_reason": "end_turn",
                "texts": [json.dumps({"verdicts": [verdict("S02", "SUPPORTED")]})],
            },
        ],
    }
    draft(
        "fixture-repair",
        verdicts=[verdict("S02", "OVERSTATED")],
        fixture=fixture,
        want={
            "status": "READY_FOR_REVIEW",
            "approvable": True,
            "repair": {
                "status": "CLEAN",
                "approvable": True,
                "max_model_rounds": 1,
                "texts": {"S02": corrected},
            },
        },
    )
    write(ROOT / "drafts.json", {"cases": cases})
    write(ROOT / "expected.json", expected)
    finding = {
        "story_key": "journal-before-request",
        "angle": "A worker writes an intent before contacting the toy service.",
        "support": "The toy record describes how the worker preserves an unresolved attempt.",
        "source_refs": [ref("README.md", 7, quote=TEXTS[2])],
    }
    normal = copy.deepcopy(finding)
    normal["story_key"] = "long-reference"
    normal["source_refs"] = [ref("README.md", 1, 24, TEXTS[2])]
    split = copy.deepcopy(finding)
    split["story_key"] = "split-reference"
    split["source_refs"] = [ref("README.md", 1, 24)]
    invalid = copy.deepcopy(finding)
    invalid["story_key"] = "unsupported-core"
    invalid["angle"] = "The queue saved 999 hours. A worker preserves the attempted action."
    invalid_ref = copy.deepcopy(finding)
    invalid_ref["story_key"] = "missing-path"
    invalid_ref["source_refs"] += [ref("ABSENT.md", 1)]
    for name, findings in (
        ("normalisation", [normal, split, invalid_ref]),
        ("invalid-core", [invalid, {"story_key": "bad"}]),
    ):
        write(
            ROOT / "stories" / f"{name}.json",
            {
                "case_version": "1",
                "source": "SYNTHETIC hand-authored fixture",
                "repo": "sample-owner/retry-lab",
                "revision": "c" * 40,
                "mode": "focus",
                "snapshot": "retry-lab",
                "supplied": {p: len(data.decode().splitlines()) for p, data in files.items()},
                "reply": {
                    "model": MODEL,
                    "request_id": None,
                    "stop_reason": "end_turn",
                    "texts": [json.dumps({"findings": findings})],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
    print(f"Wrote {len(cases)} synthetic drafts and 2 synthetic story cases")


if __name__ == "__main__":
    main()
