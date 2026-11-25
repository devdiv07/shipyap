"""C1 gate and fact-check prompt f2, on SYNTHETIC public fixtures (no model, no network).

- A CODE_MEANING_INCOMPLETE sentence unblocks only on a SUPPORTED verdict that was sent its
  extra lines; OVERSTATED, UNVERIFIABLE (and CONTRADICTED, OUTDATED) keep it blocked. Every
  other sentence keeps today's rules: OVERSTATED and UNVERIFIABLE are warnings.
- The per-value procedure is only inside the ALSO CHECK block, so a request with no extra lines
  is unchanged; every stored result records the prompt version.
"""

import hashlib
import json
from pathlib import Path

import pytest

from shipyap.automation import evaluation, factcheck
from shipyap.drafting.service import checked_input
from shipyap.editorial.grounding import check_draft
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.review.gate import judge

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public"
F1_HEADER = (
    "These lines also set a value the sentence describes; judge the sentence against them too:\n"
)


def _draft(brief: str, response: str):
    brief_bytes = (PUBLIC / "drafts" / brief).read_bytes()
    parsed = parse_brief(brief_bytes)
    checked = checked_input((PUBLIC / "drafts" / response).read_bytes(), parsed)
    draft = parse_response(checked.data)
    amap = check_draft(
        parsed,
        draft,
        hashlib.sha256(brief_bytes).hexdigest(),
        hashlib.sha256(checked.data).hexdigest(),
    )
    return amap, draft


def _snapshot(name: str):
    return evaluation._snapshot(PUBLIC / "snapshots" / name)


def _check(amap, verdicts: dict[str, str], *, supplied: bool = True) -> dict:
    lines = factcheck.meaning_lines(amap.model_dump(mode="json")["segments"])
    return {
        "verdicts": [
            {
                "segment_id": s.id,
                "verdict": verdicts.get(s.id, "SUPPORTED"),
                "reason": "planted",
                "refs": [],
                **({"supplied": list(lines[s.id])} if supplied and s.id in lines else {}),
            }
            for s in amap.segments
        ]
    }


@pytest.fixture
def badge():
    return _draft("score.brief.json", "substring-label.response.json")


def _codes(gate, sid, kind="blocks"):
    segment = next(s for s in gate.segments if s.segment_id == sid)
    return {note.code for note in getattr(segment, kind)}


def test_a_meaning_claim_unblocks_only_on_supported(badge):
    amap, draft = badge
    gate = judge(amap, draft, (), _check(amap, {}))
    assert gate.approvable
    assert _codes(gate, "S02", "warnings") == {"CODE_MEANING_INCOMPLETE"}


@pytest.mark.parametrize("verdict", ["OVERSTATED", "UNVERIFIABLE"])
def test_any_other_verdict_keeps_a_meaning_claim_blocked(badge, verdict):
    amap, draft = badge
    gate = judge(amap, draft, (), _check(amap, {"S02": verdict}))
    assert not gate.approvable
    assert "CODE_MEANING_INCOMPLETE" in _codes(gate, "S02")
    note = next(n for n in gate.segments[1].blocks if n.code == "CODE_MEANING_INCOMPLETE")
    assert note.detail == f"the fact check was sent these lines and judged it {verdict}"


def test_a_supported_verdict_without_the_lines_does_not_unblock(badge):
    amap, draft = badge
    gate = judge(amap, draft, (), _check(amap, {}, supplied=False))
    assert not gate.approvable and "CODE_MEANING_INCOMPLETE" in _codes(gate, "S02")


def test_a_malformed_verdict_does_not_unblock(badge):
    amap, draft = badge
    check = _check(amap, {})
    check["verdicts"][1]["verdict"] = "LOOKS_FINE"
    assert not judge(amap, draft, (), check).approvable


def test_other_sentences_keep_overstated_and_unverifiable_as_warnings(badge):
    amap, draft = badge
    for verdict in ("OVERSTATED", "UNVERIFIABLE"):
        gate = judge(amap, draft, (), _check(amap, {"S01": verdict}))
        assert gate.approvable  # S01 has no C1 finding
        warnings = _codes(gate, "S01", "warnings")
        # Voice and readability notes (C2a) are style warnings beside it, never blocks.
        assert {c for c in warnings if not c.startswith(("VOICE_", "READ_"))} == {
            f"FACT_CHECK_{verdict}"
        }


def test_the_public_case_pins_the_new_rule():
    """Before the change this case was approvable (GATE OPEN): the planted OVERSTATED verdict
    was sent the lines, which used to lift the block."""
    expected = json.loads((PUBLIC / "expected.json").read_text("utf-8"))["drafts"]
    assert expected["code-substring-label-overstated"]["approvable"] is False
    assert expected["code-substring-label-overstated"]["blocked"] == {
        "S02": "CODE_MEANING_INCOMPLETE"
    }


def test_the_per_value_procedure_is_only_inside_the_also_check_block(badge):
    amap, draft = badge
    segments = [{"id": s.id, "text": s.text} for s in draft.segments]
    supplied = factcheck.meaning_lines(amap.model_dump(mode="json")["segments"])
    request = factcheck.build_request(
        "claude-sonnet-5-5", _snapshot("score-panel"), segments, supplied=supplied
    )
    text = request.user_text
    assert text.count(factcheck.SUPPLIED_HEADER) == 1
    before, block = text.split("<<<ALSO CHECK id=S02>>>\n", 1)
    assert block.startswith(factcheck.SUPPLIED_HEADER)
    assert (
        "one value at a time" not in before and "one value at a time" not in request.system_prompt
    )
    assert "Fallback (Acme unavailable)" in block.split("<<<END ALSO CHECK>>>")[0]
    # The same request under f1 differs only in that header.
    f1 = text.replace(factcheck.SUPPLIED_HEADER, F1_HEADER)
    assert f1 != text and f1.replace(F1_HEADER, "") == text.replace(factcheck.SUPPLIED_HEADER, "")


def test_a_request_without_extra_lines_is_unchanged_by_f2():
    """No ALSO CHECK block, no f2 text: the request is what f1 built (pinned by hash on real
    stored requests in tests/private/test_factcheck_f2_real.py)."""
    amap, draft = _draft("retry.brief.json", "valid-control.response.json")
    segments = [{"id": s.id, "text": s.text} for s in draft.segments]
    supplied = factcheck.meaning_lines(amap.model_dump(mode="json")["segments"])
    assert supplied == {}
    request = factcheck.build_request(
        "claude-sonnet-5-5", _snapshot("retry-lab"), segments, supplied=supplied
    )
    assert "<<<ALSO CHECK" not in request.user_text
    assert factcheck.SUPPLIED_HEADER not in request.user_text


def test_every_stored_result_records_the_prompt_version(tmp_path):
    draft_dir = tmp_path / "x-draft"
    draft_dir.mkdir()
    verdict = factcheck.Verdict("S01", "SUPPORTED", "ok", ())
    path = factcheck.save(tmp_path, "x", draft_dir, [verdict])
    assert json.loads(path.read_text("utf-8"))["prompt_version"] == factcheck.PROMPT_VERSION == "f2"


@pytest.mark.parametrize("verdict", ["CONTRADICTED", "OUTDATED"])
def test_a_blocking_verdict_blocks_a_meaning_claim_through_the_verdict(badge, verdict):
    amap, draft = badge
    gate = judge(amap, draft, (), _check(amap, {"S02": verdict}))
    assert not gate.approvable
    assert _codes(gate, "S02") == {f"FACT_CHECK_{verdict}"}  # unchanged from before f2
