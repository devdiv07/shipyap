"""C1: claims about code stay within what the cited lines do. SYNTHETIC inputs only."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipyap.automation import factcheck, repair
from shipyap.automation.engine import one_per_repository
from shipyap.automation.evaluation import FixtureDrafter
from shipyap.drafting.service import checked_input
from shipyap.editorial import code_claims
from shipyap.editorial.grounding import check_draft
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.models import FindingCode, Severity
from shipyap.editorial.sources import load_verified_snapshot
from shipyap.review.gate import judge

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public"


# --- CODE_OUTCOME_UNSUPPORTED -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The hash written on-chain covers only the text.",
        "Read together, the on-chain record ties a hash to a score.",
        "The refund recorded by that call holds the amount.",
        "The job succeeded and the result was persisted.",
        "Every request is processed and confirmed.",
    ],
)
def test_a_runtime_outcome_is_found(text):
    assert code_claims.outcome_claims(text)


@pytest.mark.parametrize(
    "text",
    [
        "The handler calls send with the amount.",  # what the code is written to do
        "On error it falls back to ruleCheck.",
        "It sets the minted flag right after the call.",  # a value's name
        'The toast reads "Refund confirmed! Check your wallet."',  # quoted UI text
        "The `confirmed` field is read by the page.",
        "Nothing in these lines waits for the refund to be confirmed.",
        "If the model proposes a tool call, it is recorded and not executed.",
        "The flag is set before the transaction is confirmed.",
        "",
    ],
)
def test_code_level_wording_is_not_an_outcome(text):
    assert code_claims.outcome_claims(text) == []


def test_only_code_only_evidence_is_judged_for_outcomes():
    assert code_claims.code_only(["app/page.tsx"], ())
    assert not code_claims.code_only(["app/page.tsx", "tests/test_page.py"], ())  # a test
    assert not code_claims.code_only(["app/page.tsx", "evidence/RESULTS.md"], ())  # a record
    assert not code_claims.code_only(["app/page.tsx"], ("attempted.1",))  # the author's word
    assert not code_claims.code_only(["README.md"], ())
    assert not code_claims.code_only([], ())


# --- CODE_MEANING_INCOMPLETE ------------------------------------------------------------------

DESK = (
    "const { send, data: ticket } = useSender();",  # 1
    "const { isSuccess: isDone } = useReceipt({ ticket });",  # 2
    "const [label, setLabel] = useState('none');",  # 3
    "const a = () => { send({ kind: 'refund' }); };",  # 4
    "const b = () => { send({ kind: 'notice' }); setLabel('sent'); };",  # 5
    "return <span>{isDone ? label : 'waiting'}</span>;",  # 6
)
ROUTE = ("export const out = { label: 'model' };",)  # 1


def test_a_flag_tied_to_a_reassigned_value_lists_the_other_call():
    files = {"app/desk.tsx": DESK}
    names, gaps = code_claims.meaning_gaps(
        "isDone reflects the refund request.", [("app/desk.tsx", 1, 2)], files
    )
    assert names == ("isDone",)
    assert gaps == ("app/desk.tsx:4-4", "app/desk.tsx:5-5")  # both calls of `send`


def test_a_label_set_in_another_cited_file_is_listed():
    files = {"app/desk.tsx": DESK, "api/route.ts": ROUTE}
    names, gaps = code_claims.meaning_gaps(
        "The span shows which label the page holds.", [("app/desk.tsx", 6, 6)], files
    )
    assert names == ("label",)
    assert set(gaps) == {"app/desk.tsx:3-3", "app/desk.tsx:5-5", "api/route.ts:1-1"}


def test_no_meaning_claim_or_everything_cited_gives_no_gap():
    files = {"app/desk.tsx": DESK}
    assert (
        code_claims.meaning_gaps("The span renders label.", [("app/desk.tsx", 6, 6)], files)[1]
        == ()
    )
    whole = [("app/desk.tsx", 1, 6)]
    assert code_claims.meaning_gaps("isDone reflects the request.", whole, files) == ((), ())
    # Words that are not set anywhere (English words, imports) are never identifiers here.
    assert code_claims.meaning_gaps("useReceipt reflects it.", [("app/desk.tsx", 2, 2)], files) == (
        (),
        (),
    )
    assert code_claims.meaning_gaps("isDone reflects it.", [], files) == ((), ())
    assert code_claims.meaning_gaps("isDone reflects it.", [("README.md", 1, 1)], files) == ((), ())


def test_a_long_gap_list_is_capped():
    lines = tuple(f"label = {n};" for n in range(30)) + ("show(label);",)
    _, gaps = code_claims.meaning_gaps(
        "It shows which label wins.", [("a.ts", 31, 31)], {"a.ts": lines}
    )
    assert len(gaps) == code_claims.MAX_GAP_LINES


@pytest.mark.parametrize("detail", [None, "", "label", "label; "])
def test_a_malformed_detail_lists_no_lines(detail):
    assert code_claims.gap_labels(detail) == ()


# --- the gate, the fact check and repair, on the SYNTHETIC refund desk --------------------------


def _case(name):
    brief = parse_brief((PUBLIC / "drafts/refund.brief.json").read_bytes())
    raw = (PUBLIC / f"drafts/{name}.response.json").read_bytes()
    response = parse_response(checked_input(raw, brief).data)
    return brief, response, check_draft(brief, response, "0" * 64, "0" * 64)


def test_the_meaning_finding_is_a_map_warning_and_a_gate_block():
    _, response, amap = _case("code-label")
    finding = next(f for f in amap.segments[1].findings)
    assert finding.code == FindingCode.CODE_MEANING_INCOMPLETE
    assert finding.severity == Severity.WARNING and amap.draft_status == "READY_FOR_REVIEW"
    gate = judge(amap, response)
    assert [n.code for n in gate.segments[1].blocks] == ["CODE_MEANING_INCOMPLETE"]
    assert "api/review.ts:16-16" in gate.segments[1].blocks[0].refs


def _verdicts(response, supplied=None, verdict="SUPPORTED"):
    ids = [s.id for s in response.segments]
    texts = (
        json.dumps(
            {
                "verdicts": [
                    {"segment_id": i, "verdict": verdict, "reason": "SYNTHETIC", "refs": []}
                    for i in ids
                ]
            }
        ),
    )
    return {
        "verdicts": [v.as_dict() for v in factcheck.parse_reply(texts, "end_turn", ids, supplied)]
    }


def test_only_a_fact_check_sent_the_lines_lifts_the_block_and_an_edit_restores_it():
    _, response, amap = _case("code-label")
    supplied = factcheck.meaning_lines([s.model_dump(mode="json") for s in amap.segments])
    assert set(supplied) == {"S02"}
    assert not judge(amap, response, fact_check=_verdicts(response)).approvable
    checked = _verdicts(response, supplied)
    assert "supplied" not in checked["verdicts"][0]  # unchanged format without lines
    gate = judge(amap, response, fact_check=checked)
    assert gate.approvable and gate.segments[1].warnings[0].code == "CODE_MEANING_INCOMPLETE"
    edited = response.model_copy(
        update={
            "segments": (
                response.segments[0],
                response.segments[1].model_copy(update={"text": "The badge shows which source."}),
            )
        }
    )
    assert not judge(amap, edited, fact_check=checked, checked=response).approvable


def test_the_fact_check_request_carries_the_lines_only_when_there_are_any():
    _, response, amap = _case("code-label")
    snapshot = load_verified_snapshot(PUBLIC / "snapshots/refund-desk")
    segments = [{"id": s.id, "text": s.text} for s in response.segments]
    plain = factcheck.build_request("claude-sonnet-5-5", snapshot, segments)
    assert "ALSO CHECK" not in plain.user_text
    supplied = {"S02": ("api/review.ts:16-16", "missing.ts:1-1", "not a label")}
    request = factcheck.build_request("claude-sonnet-5-5", snapshot, segments, supplied=supplied)
    block = request.user_text.split("<<<ALSO CHECK id=S02>>>", 1)[1]
    assert "source: 'model'" in block and "missing.ts" not in block


def test_a_rewrite_that_keeps_the_claim_is_re_checked_with_the_lines():
    brief, response, _ = _case("code-label")
    snapshot = load_verified_snapshot(PUBLIC / "snapshots/refund-desk")
    stored = _verdicts(response)  # a fact check that was never sent the lines
    rewrite = "The badge text shows which value source holds."
    drafter = FixtureDrafter(
        (
            {
                "stop_reason": "end_turn",
                "texts": [json.dumps({"rewrites": [{"segment_id": "S02", "text": rewrite}]})],
            },
            {
                "stop_reason": "end_turn",
                "texts": [
                    json.dumps(
                        {
                            "verdicts": [
                                {
                                    "segment_id": "S02",
                                    "verdict": "SUPPORTED",
                                    "reason": "x",
                                    "refs": [],
                                }
                            ]
                        }
                    )
                ],
            },
        )
    )
    outcome = repair.repair(brief, response, (), stored, snapshot, drafter, "claude-sonnet-5-5")
    assert outcome.status == repair.CLEAN, outcome.reasons
    assert "<<<ALSO CHECK id=S02>>>" in drafter.requests[1].user_text
    s02 = next(v for v in outcome.fact_check["verdicts"] if v["segment_id"] == "S02")
    assert "api/review.ts:16-16" in s02["supplied"]


def test_without_a_model_a_code_claim_is_shelved_not_rewritten():
    brief, response, _ = _case("code-outcome")
    snapshot = load_verified_snapshot(PUBLIC / "snapshots/refund-desk")
    outcome = repair.repair(brief, response, (), None, snapshot, None, "claude-sonnet-5-5")
    assert outcome.status == repair.SHELVED and outcome.response == response


def test_a_widening_rewrite_is_refused():
    brief, response, _ = _case("code-outcome")
    snapshot = load_verified_snapshot(PUBLIC / "snapshots/refund-desk")
    widened = "The refund is stored in Postgres with the amount."
    reply = {
        "stop_reason": "end_turn",
        "texts": [json.dumps({"rewrites": [{"segment_id": "S02", "text": widened}]})],
    }
    drafter = FixtureDrafter((reply, reply))
    outcome = repair.repair(brief, response, (), None, snapshot, drafter, "claude-sonnet-5-5")
    assert outcome.status == repair.SHELVED
    assert all(s.text != widened for s in outcome.response.segments)


# --- one draft per repository in a review batch ------------------------------------------------


def _story(sid, repo):
    return SimpleNamespace(
        story=SimpleNamespace(
            story_id=sid, repo_id=repo, latest=SimpleNamespace(manifest_sha256="m")
        )
    )


def test_one_per_repository_holds_only_busy_repositories():
    available = [_story("a", "o/one"), _story("b", "o/one"), _story("c", "o/two")]
    assert one_per_repository(available, frozenset()) == frozenset()
    assert one_per_repository(available, frozenset({"o/one"})) == {("a", "m"), ("b", "m")}
    assert one_per_repository([], frozenset({"o/one"})) == frozenset()
