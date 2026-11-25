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


def publication_case(expected: dict) -> None:
    """Synthetic repeated-story history, independent of any private incident inputs."""
    from shipyap.editorial.grounding import render_post
    from shipyap.editorial.models import DraftResponse
    from shipyap.publication.approval import approval_id, text_sha256

    raw = (ROOT / "drafts/valid-control.response.json").read_bytes()
    text = render_post(DraftResponse.model_validate_json(raw).segments)
    approvals, events = [], []
    story = "a" * 64
    for seq, approved, submitted, due in (
        (2, "2026-10-29T11:53:28Z", "2026-10-29T12:03:37Z", "2026-11-02T04:00:00Z"),
        (9, "2026-11-03T12:36:52Z", "2026-11-03T12:37:16Z", "2026-11-06T04:00:00Z"),
        (10, "2026-11-03T12:49:36Z", "2026-11-03T12:49:44Z", "2026-11-09T04:00:00Z"),
    ):
        aid = approval_id(text, "a1" * 12, due)
        approvals.append(
            dict(
                approval_id=aid,
                text=text,
                text_sha256=text_sha256(text),
                channel_id="a1" * 12,
                due_at=due,
                workspace_id="sample_workspace",
                bundle_record_sha256="b" * 64,
                approved_at=approved,
            )
        )
        row = dict(
            seq=seq,
            story_id=story,
            decision="APPROVED",
            approval_id=aid,
            text=text,
            text_sha256=text_sha256(text),
            draft_bundle="sample-draft",
            draft_record_sha256="b" * 64,
            origin_record_sha256="b" * 64,
            link_sha256="c" * 64,
            decided_at=approved,
        )
        if seq == 2:
            revision = {
                **row,
                "seq": 1,
                "decision": "REVISED",
                "approval_id": None,
                "decided_at": "2026-10-29T11:47:56Z",
            }
            events.append(dict(kind="review", at=revision["decided_at"], row=revision))
        events.append(dict(kind="review", at=approved, row=row))
        events.append(
            dict(
                kind="submit",
                at=submitted,
                approval_id=aid,
                recorded_post_id="c3" * 12,
                recorded_final_state={2: "SENT", 9: "ACCEPTED", 10: "SCHEDULED"}[seq],
                recorded_updated_at={
                    2: "2026-11-02T04:58:05Z",
                    9: submitted,
                    10: "2026-11-06T18:47:02Z",
                }[seq],
            )
        )
        if seq == 2:
            events.append(
                dict(kind="state", at="2026-11-02T04:58:05Z", approval_id=aid, state="SENT")
            )
    events.sort(key=lambda e: e["at"])
    write(
        ROOT / "publications/repeated-story.history.json",
        dict(
            version=1,
            story_id=story,
            original_response="drafts/valid-control.response.json",
            brief="drafts/retry.brief.json",
            original_response_sha256=hashlib.sha256(raw).hexdigest(),
            original_text_sha256=text_sha256(text),
            recorded_sends=3,
            approvals=approvals,
            events=events,
        ),
    )
    expected["publications"] = {
        "repeated-story.history": dict(
            recorded_sends=3,
            create_calls=1,
            outcomes=["ACCEPTED", "STORY_ALREADY_SUBMITTED", "STORY_ALREADY_SUBMITTED"],
        )
    }


# C1: claims about code. A fictional refund desk (UI) and review route, written so that each
# defect class has one sentence: a requested action stated as a completed record, a label whose
# value is also set where the sentence does not cite, and a flag tied to a value that a second
# call reassigns. The control says only what the code is written to do.
DESK = "app/desk.tsx"
ROUTE = "api/review.ts"
CODE_FILES = {
    "README.md": b"# SYNTHETIC refund desk\n\nA fictional desk for code-claim examples.\n",
    DESK: b"""'use client';

import { useState } from 'react';
import { useSender, useReceipt } from './sender';

export function RefundDesk({ amount }: { amount: number }) {
  const { send, data: ticket } = useSender();
  const { isSuccess: isDone } = useReceipt({ ticket });
  const [noticeSent, setNoticeSent] = useState(false);
  const [review, setReview] = useState(null);

  const requestRefund = () => {
    send({ action: 'refund', amount });
  };

  const sendNotice = () => {
    send({ action: 'notice' });
    setNoticeSent(true);
  };

  const load = async () => {
    const reply = await fetch('/api/review').then((r) => r.json());
    setReview({ score: reply.score, source: reply.source });
  };

  return (
    <div>
      <button onClick={requestRefund}>Request refund</button>
      {isDone && <button onClick={sendNotice}>Send notice</button>}
      {noticeSent && <p>Notice sent</p>}
      {review && (
        <span>{review.source === 'model' ? 'Model Review' : 'Rule Check'}</span>
      )}
      <button onClick={load}>Load review</button>
    </div>
  );
}
""",
    ROUTE: b"""// SYNTHETIC toy review route.
function ruleCheck(text: string) {
  return { score: text.includes('refund') ? 40 : 10 };
}

export async function review(text: string, callModel?: (t: string) => Promise<string>) {
  if (!callModel) {
    return { score: ruleCheck(text).score, source: 'rules' };
  }
  let result;
  try {
    result = JSON.parse(await callModel(text));
  } catch {
    result = ruleCheck(text);
  }
  return { score: result.score, source: 'model' };
}
""",
}
CODE_OK = {
    "calls": (
        "The desk's refund handler calls send with the refund action and the amount.",
        DESK,
        12,
        14,
    ),
    "fallback": ("On a parse error the review route falls back to ruleCheck.", ROUTE, 11, 15),
    "flag": (
        "The notice handler sets noticeSent right after it calls send, without waiting for a result.",
        DESK,
        16,
        19,
    ),
    "badge": (
        "The badge text comes from review.source: Model Review when it equals model, otherwise "
        "Rule Check.",
        DESK,
        31,
        33,
    ),
    "unconfirmed": ("Nothing in these lines waits for the refund to be confirmed.", DESK, 12, 14),
}


def code_snapshot() -> None:
    request = IngestionRequest(
        request_version="1",
        workspace_id="synthetic_workspace",
        owner="sample-owner",
        repo="refund-desk",
        revision_sha="e" * 40,
        paths=tuple(CODE_FILES),
    )
    fetched = FetchedSnapshot(
        commit_sha="e" * 40,
        tree_sha="f" * 40,
        files=tuple(
            FetchedFile(
                path=p,
                mode="100644",
                blob_sha=hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
                size=len(data),
                data=data,
            )
            for p, data in CODE_FILES.items()
        ),
    )
    snapshot_dir = ROOT / "snapshots" / "refund-desk"
    if not snapshot_dir.exists():
        publish_snapshot(snapshot_dir, build_plan(request, fetched))


def code_cases(cases: list, expected: dict) -> None:
    code_snapshot()
    snapshot = load_verified_snapshot(ROOT / "snapshots" / "refund-desk")
    assert {s.path: "\n".join(s.lines) + "\n" for s in snapshot.sources} == {
        p: data.decode() for p, data in CODE_FILES.items()
    }, "existing synthetic code snapshot differs"
    contribution = AuthorContribution(
        contribution_version="1",
        workspace_id="synthetic_workspace",
        author_name="Sample Author",
        relationship="primary_author",
        channel="linkedin",
        audience="Readers of synthetic examples",
        insight="Unverified backlog angle: the toy desk's flags describe calls, not outcomes.",
        attempted=("I wrote a fictional refund desk to explain code-level claims.",),
        learned=(NOT_LEARNED,),
        why_it_matters=("Unverified backlog support: send is not awaited.",),
        limitations=(
            PROVENANCE,
            "Cited evidence at revision " + "e" * 40 + ": app/desk.tsx:12-14.",
            "Only the author's own items (attempted, learned) are personal statements. "
            "The angle, its support and every result must come from the cited source lines.",
        ),
        do_not_claim=("Real-world refunds or adoption.",),
    )
    brief = build_brief(
        snapshot,
        contribution,
        hashlib.sha256(canonical_json(contribution.model_dump(mode="json")).encode()).hexdigest(),
    )
    brief_bytes = render_brief_json(brief)
    (ROOT / "drafts" / "refund.brief.json").write_bytes(brief_bytes)

    def response(sentences) -> dict:
        return {
            "response_version": "1",
            "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
            "outcome": "DRAFT",
            "no_post_reason": None,
            "generator": {
                "kind": "external_model",
                "label": "SYNTHETIC hand-authored model-shaped fixture",
            },
            "segments": [
                segment(f"S{i:02d}", text, [ref(path, start, end)])
                for i, (text, path, start, end) in enumerate(sentences, 1)
            ],
            "verification_needed": [],
        }

    def draft(name, sentences, want, *, control=False, fixture=None) -> None:
        write(ROOT / "drafts" / f"{name}.response.json", response(sentences))
        entry = {
            "name": name,
            "brief": "drafts/refund.brief.json",
            "response": f"drafts/{name}.response.json",
        }
        if control:
            entry["control"] = True
        if fixture:
            entry["repair_fixture"] = f"drafts/{name}.repair-fixture.json"
            write(ROOT / entry["repair_fixture"], fixture)
        cases.append(entry)
        expected["drafts"][name] = want

    ok = CODE_OK
    draft(
        "code-control",
        [ok[k] for k in ok],
        {"status": "READY_FOR_REVIEW", "approvable": True},
        control=True,
    )
    # (a) a requested action stated as a completed record (CODE lines only).
    outcome = ("The refund recorded by that call holds the action and the amount.", DESK, 12, 14)
    draft(
        "code-outcome",
        [ok["calls"], outcome, ok["fallback"]],
        {
            "status": "NEEDS_REVISION",
            "approvable": False,
            "blocked": {"S02": "CODE_OUTCOME_UNSUPPORTED"},
            "unblocked": ["S01", "S03"],
            "repair": {"status": "SHELVED", "approvable": False},
        },
    )
    narrowed = "The call passes the action and the amount to send; these lines do not wait for it."
    draft(
        "code-outcome-repair",
        [ok["calls"], outcome, ok["fallback"]],
        {
            "status": "NEEDS_REVISION",
            "approvable": False,
            "blocked": {"S02": "CODE_OUTCOME_UNSUPPORTED"},
            "repair": {
                "status": "CLEAN",
                "approvable": True,
                "max_model_rounds": 1,
                "texts": {"S02": narrowed},
                "fact_check": "REQUIRED",
            },
        },
        fixture={
            "fixture": "SYNTHETIC",
            "about": "Handwritten narrowing to code-level wording; never a live model result.",
            "replies": [
                {
                    "stop_reason": "end_turn",
                    "texts": [json.dumps({"rewrites": [{"segment_id": "S02", "text": narrowed}]})],
                }
            ],
        },
    )
    # (b) a label whose value the route also sets where the sentence does not cite.
    label = (
        "The badge shows Model Review or Rule Check from the source field, so a reader can see "
        "which path produced the score.",
        DESK,
        31,
        33,
    )
    draft(
        "code-label",
        [ok["fallback"], label],
        {
            "status": "READY_FOR_REVIEW",
            "approvable": False,
            "blocked": {"S02": "CODE_MEANING_INCOMPLETE"},
            "unblocked": ["S01"],
        },
    )
    # (c) a flag tied to a value that a second call of the same hook reassigns, and a flag
    # described from lines that miss where its function comes from.
    shared = (
        "isDone reflects the refund request, so a failed notice would still show as sent.",
        DESK,
        7,
        8,
    )
    described = (
        "A flag set right after calling send describes the request, not the outcome.",
        DESK,
        16,
        19,
    )
    draft(
        "code-shared-flag",
        [ok["flag"], shared, described],
        {
            "status": "READY_FOR_REVIEW",
            "approvable": False,
            "blocked": {"S02": "CODE_MEANING_INCOMPLETE", "S03": "CODE_MEANING_INCOMPLETE"},
            "unblocked": ["S01"],
        },
    )


# C1, per-value check: a badge chosen by a substring test, where one fallback's label also
# contains the matched word, so that fallback shows the model badge. The sentence's claim ("a
# reader can see which path produced the score") is wrong for exactly one value the route sets.
PANEL = "app/panel.tsx"
SCORE = "api/score.ts"
BADGE_FILES = {
    "README.md": b"# SYNTHETIC score panel\n\nA fictional panel for code-claim examples.\n",
    PANEL: b"""'use client';

import { useState } from 'react';

export function ScorePanel() {
  const [result, setResult] = useState(null);

  const load = async () => {
    const reply = await fetch('/api/score').then((r) => r.json());
    setResult({ score: reply.score, provider: reply.provider });
  };

  return (
    <div>
      {result && (
        <span>{result.provider.includes('Acme') ? 'AI Review' : 'Rule Check'}</span>
      )}
      <button onClick={load}>Load score</button>
    </div>
  );
}
""",
    SCORE: b"""// SYNTHETIC toy scoring route.
function ruleScore(text: string) {
  return text.includes('refund') ? 40 : 10;
}

export async function score(text: string, callAcme: (t: string) => Promise<Response>) {
  const reply = await callAcme(text);
  if (!reply.ok) {
    return { score: ruleScore(text), provider: 'Fallback (Acme unavailable)' };
  }
  try {
    const parsed = JSON.parse(await reply.text());
    return { score: parsed.score, provider: 'Acme Model' };
  } catch {
    return { score: ruleScore(text), provider: 'Fallback (parse error)' };
  }
}
""",
}


def badge_cases(cases: list, expected: dict) -> None:
    from shipyap.automation import factcheck
    from shipyap.editorial.grounding import check_draft
    from shipyap.editorial.inputs import parse_brief, parse_response

    request = IngestionRequest(
        request_version="1",
        workspace_id="synthetic_workspace",
        owner="sample-owner",
        repo="score-panel",
        revision_sha="a" * 40,
        paths=tuple(BADGE_FILES),
    )
    fetched = FetchedSnapshot(
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        files=tuple(
            FetchedFile(
                path=p,
                mode="100644",
                blob_sha=hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
                size=len(data),
                data=data,
            )
            for p, data in BADGE_FILES.items()
        ),
    )
    snapshot_dir = ROOT / "snapshots" / "score-panel"
    if not snapshot_dir.exists():
        publish_snapshot(snapshot_dir, build_plan(request, fetched))
    snapshot = load_verified_snapshot(snapshot_dir)
    assert {s.path: "\n".join(s.lines) + "\n" for s in snapshot.sources} == {
        p: data.decode() for p, data in BADGE_FILES.items()
    }, "existing synthetic badge snapshot differs"
    contribution = AuthorContribution(
        contribution_version="1",
        workspace_id="synthetic_workspace",
        author_name="Sample Author",
        relationship="primary_author",
        channel="linkedin",
        audience="Readers of synthetic examples",
        insight="Unverified backlog angle: the toy badge is chosen by a substring test.",
        attempted=("I wrote a fictional score panel to explain code-level claims.",),
        learned=(NOT_LEARNED,),
        why_it_matters=("Unverified backlog support: the badge reads the provider string.",),
        limitations=(
            PROVENANCE,
            "Cited evidence at revision " + "a" * 40 + ": app/panel.tsx:15-17.",
            "Only the author's own items (attempted, learned) are personal statements. "
            "The angle, its support and every result must come from the cited source lines.",
        ),
        do_not_claim=("Real-world scoring or adoption.",),
    )
    brief = build_brief(
        snapshot,
        contribution,
        hashlib.sha256(canonical_json(contribution.model_dump(mode="json")).encode()).hexdigest(),
    )
    brief_bytes = render_brief_json(brief)
    (ROOT / "drafts" / "score.brief.json").write_bytes(brief_bytes)
    sentences = [
        ("When the Acme call is not ok, the scoring route falls back to ruleScore.", SCORE, 7, 10),
        (
            "The badge shows AI Review or Rule Check from the provider field, so a reader can "
            "see which path produced the score.",
            PANEL,
            15,
            17,
        ),
    ]
    response = {
        "response_version": "1",
        "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
        "outcome": "DRAFT",
        "no_post_reason": None,
        "generator": {
            "kind": "external_model",
            "label": "SYNTHETIC hand-authored model-shaped fixture",
        },
        "segments": [
            segment(f"S{i:02d}", text, [ref(path, start, end)])
            for i, (text, path, start, end) in enumerate(sentences, 1)
        ],
        "verification_needed": [],
    }
    write(ROOT / "drafts" / "substring-label.response.json", response)
    data = (ROOT / "drafts" / "substring-label.response.json").read_bytes()
    amap = check_draft(
        parse_brief(brief_bytes),
        parse_response(data),
        hashlib.sha256(brief_bytes).hexdigest(),
        hashlib.sha256(data).hexdigest(),
    )
    supplied = factcheck.meaning_lines(amap.model_dump(mode="json")["segments"])
    assert set(supplied) == {"S02"} and f"{SCORE}:9-9" in supplied["S02"], supplied
    common = {
        "brief": "drafts/score.brief.json",
        "response": "drafts/substring-label.response.json",
    }
    cases.append({"name": "code-substring-label", **common})
    expected["drafts"]["code-substring-label"] = {
        "status": "READY_FOR_REVIEW",
        "approvable": False,
        "blocked": {"S02": "CODE_MEANING_INCOMPLETE"},
        "unblocked": ["S01"],
    }
    # The fact check was sent the lines but only called S02 OVERSTATED: still blocked, since
    # those lines decide whether a meaning claim is right (a warning for any other sentence).
    check = {
        "prompt_version": factcheck.PROMPT_VERSION,
        "verdicts": [
            {
                "segment_id": "S01",
                "verdict": "SUPPORTED",
                "reason": "SYNTHETIC planted judgment.",
                "refs": [f"{SCORE}:7-10"],
            },
            {
                "segment_id": "S02",
                "verdict": "OVERSTATED",
                "reason": "SYNTHETIC planted judgment: 'Fallback (Acme unavailable)' includes "
                "'Acme', so that fallback shows AI Review.",
                "refs": [f"{PANEL}:16-16", f"{SCORE}:9-9"],
                "supplied": list(supplied["S02"]),
            },
        ],
    }
    write(ROOT / "drafts" / "substring-label.fact-check.json", check)
    cases.append(
        {
            "name": "code-substring-label-overstated",
            **common,
            "fact_check": "drafts/substring-label.fact-check.json",
        }
    )
    expected["drafts"]["code-substring-label-overstated"] = {
        "status": "READY_FOR_REVIEW",
        "approvable": False,
        "blocked": {"S02": "CODE_MEANING_INCOMPLETE"},
        "unblocked": ["S01"],
    }


# C2b, voice dry-nerdy (prompt p5): three short drafts that pass every check, a sarcastic FRAMING
# line carrying a number, one carrying a quoted name and a code verb (both FRAMING_HAS_CLAIM),
# and a "Takeaway:" line, which is Must fix in a p5 draft. The label names p5, as the tool's
# generator label does; nothing else marks a draft as dry-nerdy.
P5_LABEL = "SYNTHETIC hand-authored model-shaped fixture via draft-model p5"
P5_WANT = {
    "p5-plain": {"status": "READY_FOR_REVIEW", "approvable": True, "unblocked": ["S05"]},
    "p5-code-punchline": {
        "status": "READY_FOR_REVIEW",
        "approvable": True,
        "unblocked": ["S01", "S05"],
    },
    "p5-author": {"status": "READY_FOR_REVIEW", "approvable": True, "unblocked": ["S01", "S05"]},
    "p5-sarcastic-number": {
        "status": "NEEDS_REVISION",
        "approvable": False,
        "blocked": {"S05": "FRAMING_HAS_CLAIM"},
    },
    "p5-sarcastic-claim": {
        "status": "NEEDS_REVISION",
        "approvable": False,
        "blocked": {"S05": "FRAMING_HAS_CLAIM"},
    },
    "p5-pronoun-claim": {
        "status": "NEEDS_REVISION",
        "approvable": False,
        "blocked": {"S05": "FRAMING_HAS_CLAIM"},
    },
    "p5-labelled": {
        "status": "READY_FOR_REVIEW",
        "approvable": False,
        "blocked": {"S04": "LABELLED_SECTION"},
    },
}


def voice_cases(cases: list, expected: dict) -> None:
    brief_bytes = (ROOT / "drafts" / "refund.brief.json").read_bytes()

    def ref(p, a, b, q=None):
        return {"path": p, "line_start": a, "line_end": b, "quote": q}

    def seg(i, text, basis, src=(), auth=(), brk=False):
        return {
            "id": i,
            "text": text,
            "basis": basis,
            "source_refs": list(src),
            "author_refs": list(auth),
            "paragraph_break_before": brk,
        }

    R, K = "api/review.ts", "app/desk.tsx"
    CASES = {
        "p5-plain": [
            seg(
                "S01",
                "On a parse error, the toy review route falls back to ruleCheck.",
                "SOURCE",
                [ref(R, 11, 15)],
            ),
            seg(
                "S02",
                "The return line after that still sets source to model, whichever path produced the score.",
                "SOURCE",
                [ref(R, 10, 16)],
            ),
            seg(
                "S03",
                "The badge text comes from review.source: Model Review when it equals model, otherwise Rule Check.",
                "SOURCE",
                [ref(K, 31, 33)],
                brk=True,
            ),
            seg(
                "S04",
                "So a score from the rule check can sit under a Model Review badge.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
            ),
            seg("S05", "Naming things remains the hard part.", "FRAMING", brk=True),
        ],
        "p5-code-punchline": [
            seg(
                "S01",
                "return { score: result.score, source: 'model' };",
                "SOURCE",
                [ref(R, 16, 16, "return { score: result.score, source: 'model' };")],
            ),
            seg(
                "S02",
                "That is the last line of the toy review route, and the parse-error fallback to ruleCheck comes before it.",
                "SOURCE",
                [ref(R, 11, 16)],
                brk=True,
            ),
            seg(
                "S03",
                "The badge on the desk shows Model Review whenever source equals model.",
                "SOURCE",
                [ref(K, 31, 33)],
            ),
            seg(
                "S04",
                "So the rule check can do the scoring while the badge names the model.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
            ),
            seg("S05", "Credit where credit is not due.", "FRAMING", brk=True),
        ],
        "p5-author": [
            seg(
                "S01",
                "I wrote a fictional refund desk to explain code-level claims.",
                "AUTHOR",
                auth=["attempted.1"],
            ),
            seg(
                "S02",
                "Its refund button calls send with the refund action and the amount.",
                "SOURCE",
                [ref(K, 12, 14)],
                brk=True,
            ),
            seg(
                "S03",
                "Nothing in those lines waits for the refund to be confirmed.",
                "SOURCE",
                [ref(K, 12, 14)],
            ),
            seg(
                "S04",
                "The notice handler sets noticeSent right after it calls send, without waiting for a result.",
                "SOURCE",
                [ref(K, 16, 19)],
            ),
            seg("S05", "Optimism, but make it a button.", "FRAMING", brk=True),
        ],
        "p5-sarcastic-number": [
            seg(
                "S01",
                "On a parse error, the toy review route falls back to ruleCheck.",
                "SOURCE",
                [ref(R, 11, 15)],
            ),
            seg(
                "S02",
                "The return line after that still sets source to model, whichever path produced the score.",
                "SOURCE",
                [ref(R, 10, 16)],
            ),
            seg(
                "S03",
                "The badge text comes from review.source: Model Review when it equals model, otherwise Rule Check.",
                "SOURCE",
                [ref(K, 31, 33)],
                brk=True,
            ),
            seg(
                "S04",
                "So a score from the rule check can sit under a Model Review badge.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
            ),
            seg("S05", "A solid 40 points of borrowed confidence.", "FRAMING", brk=True),
        ],
        "p5-sarcastic-claim": [
            seg(
                "S01",
                "On a parse error, the toy review route falls back to ruleCheck.",
                "SOURCE",
                [ref(R, 11, 15)],
            ),
            seg(
                "S02",
                "The return line after that still sets source to model, whichever path produced the score.",
                "SOURCE",
                [ref(R, 10, 16)],
            ),
            seg(
                "S03",
                "The badge text comes from review.source: Model Review when it equals model, otherwise Rule Check.",
                "SOURCE",
                [ref(K, 31, 33)],
                brk=True,
            ),
            seg(
                "S04",
                "So a score from the rule check can sit under a Model Review badge.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
            ),
            seg(
                "S05",
                "Bold move, returning 'model' no matter who did the work.",
                "FRAMING",
                brk=True,
            ),
        ],
        # C2c: a pronoun subject before an action verb is a claim about the code.
        "p5-pronoun-claim": [
            seg(
                "S01",
                "On a parse error, the toy review route falls back to ruleCheck.",
                "SOURCE",
                [ref(R, 11, 15)],
            ),
            seg(
                "S02",
                "The return line after that still sets source to model, whichever path produced the score.",
                "SOURCE",
                [ref(R, 10, 16)],
            ),
            seg(
                "S03",
                "The badge text comes from review.source: Model Review when it equals model, otherwise Rule Check.",
                "SOURCE",
                [ref(K, 31, 33)],
                brk=True,
            ),
            seg(
                "S04",
                "So a score from the rule check can sit under a Model Review badge.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
            ),
            seg("S05", "It refuses to cut a sentence.", "FRAMING", brk=True),
        ],
        "p5-labelled": [
            seg(
                "S01",
                "On a parse error, the toy review route falls back to ruleCheck.",
                "SOURCE",
                [ref(R, 11, 15)],
            ),
            seg(
                "S02",
                "The return line after that still sets source to model, whichever path produced the score.",
                "SOURCE",
                [ref(R, 10, 16)],
            ),
            seg(
                "S03",
                "The badge text comes from review.source: Model Review when it equals model, otherwise Rule Check.",
                "SOURCE",
                [ref(K, 31, 33)],
                brk=True,
            ),
            seg(
                "S04",
                "Takeaway: a score from the rule check can sit under a Model Review badge.",
                "INTERPRETATION",
                [ref(R, 13, 16), ref(K, 31, 33)],
                brk=True,
            ),
            seg("S05", "Naming things remains the hard part.", "FRAMING", brk=True),
        ],
    }
    for name, segments in CASES.items():
        write(
            ROOT / "drafts" / f"{name}.response.json",
            {
                "response_version": "1",
                "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(),
                "outcome": "DRAFT",
                "no_post_reason": None,
                "generator": {"kind": "external_model", "label": P5_LABEL},
                "segments": segments,
                "verification_needed": [],
            },
        )
        case = {
            "name": name,
            "brief": "drafts/refund.brief.json",
            "response": f"drafts/{name}.response.json",
        }
        if P5_WANT[name]["approvable"]:
            case["control"] = True
        cases.append(case)
        expected["drafts"][name] = P5_WANT[name]


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
    code_cases(cases, expected)
    badge_cases(cases, expected)
    voice_cases(cases, expected)
    write(ROOT / "drafts.json", {"cases": cases})
    for name in ("hash-aside", "missing-scope", "fixture-repair"):
        expected["drafts"][name]["repair"]["fact_check"] = "REQUIRED"
    publication_case(expected)
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
