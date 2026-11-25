"""Public eval selection and the hand-authored regression corpus, offline."""

import json
import shutil
from pathlib import Path

import pytest
from public_fake import directory_link

from shipyap.automation import evaluation
from shipyap.automation.cli import main
from shipyap.automation.eval_profiles import public_cases
from shipyap.drafting.service import checked_input
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.publication.x import x_weight
from shipyap.review.x_post import x_post

EVALS = Path(__file__).resolve().parents[1] / "evals"
PUBLIC = EVALS / "public"


@pytest.fixture(scope="module")
def replay():
    stories, drafts = evaluation.load_cases(PUBLIC)
    return evaluation.evaluate(stories, drafts)


def test_public_profile_passes_without_any_private_files(tmp_path, capsys):
    base = tmp_path / "cases with spaces"
    shutil.copytree(PUBLIC, base / "public")
    assert main(["eval", "--cases", str(base), "--profile", "public", "--check"]) == 0
    out = capsys.readouterr().out
    assert "stories=2" in out and "drafts=28" in out and out.rstrip().endswith("GATE PASS")


def test_public_profile_pins_the_intended_defects(replay):
    stories = {s.name: s for s in replay.stories}
    assert stories["normalisation"].accepted == 3
    changes = [c.kind for f in stories["normalisation"].findings for c in f.changes]
    assert {"NARROWED", "SPLIT", "REF_DROPPED"} <= set(changes)
    assert stories["invalid-core"].accepted == 0
    assert any("CORE_CLAIM_DROPPED" in (f.reason or "") for f in stories["invalid-core"].findings)
    assert sum(s.outside_quote for s in replay.stories) == 0
    drafts = {d.name: d for d in replay.drafts}
    assert len(drafts) == 28  # + six C2b p5 cases (three controls, three blocked), one C2c
    assert drafts["overlong-reference"].repaired
    assert not drafts["author-angle"].approvable
    assert not drafts["outdated-option"].approvable and not drafts["outdated-status"].approvable
    assert not drafts["contradicted"].approvable
    assert drafts["warning-control"].approvable
    assert drafts["tool-text-short"].repair.status == "SHELVED"
    assert drafts["tool-text-short"].repair.texts.get("S06") is None
    assert drafts["known-hash"].approvable
    assert drafts["hash-aside"].repair.status == "CLEAN"
    assert drafts["missing-scope"].repair.status == "CLEAN"
    assert drafts["fixture-repair"].repair.status == "CLEAN"
    assert len(drafts["fixture-repair"].repair.rounds) == 1
    assert sum(d.false_alarms for d in replay.drafts) == 0


def _blocks(draft, segment_id):
    return {n.split(" ", 1)[0] for g in draft.gate if g.segment_id == segment_id for n in g.blocks}


def test_public_profile_pins_the_code_claim_defects(replay):
    """C1, SYNTHETIC: the same three patterns as the private compli drafts, and a control."""
    drafts = {d.name: d for d in replay.drafts}
    control = drafts["code-control"]
    assert control.control and control.approvable and not any(g.blocks for g in control.gate)
    assert _blocks(drafts["code-outcome"], "S02") == {"CODE_OUTCOME_UNSUPPORTED"}
    assert not _blocks(drafts["code-outcome"], "S01")  # "calls send with ..." is code-level
    assert drafts["code-outcome"].repair.status == "SHELVED"  # no model: it cannot be narrowed
    narrowed = drafts["code-outcome-repair"].repair
    assert narrowed.status == "CLEAN" and narrowed.approvable and len(narrowed.rounds) == 1
    assert _blocks(drafts["code-label"], "S02") == {"CODE_MEANING_INCOMPLETE"}
    shared = drafts["code-shared-flag"]
    assert _blocks(shared, "S02") == _blocks(shared, "S03") == {"CODE_MEANING_INCOMPLETE"}
    assert not _blocks(shared, "S01")
    label = next(g for g in drafts["code-label"].gate if g.segment_id == "S02")
    assert any("api/review.ts:16-16" in note for note in label.blocks)  # the `source: 'model'` line


def test_public_missing_expected_case_fails(tmp_path, capsys):
    shutil.copytree(PUBLIC, tmp_path / "public")
    path = tmp_path / "public/drafts.json"
    listing = json.loads(path.read_text("utf-8"))
    listing["cases"] = listing["cases"][1:]
    path.write_text(json.dumps(listing), encoding="utf-8")
    assert main(["eval", "--cases", str(tmp_path), "--profile", "public", "--check"]) == 1
    assert "expected draft case missing" in capsys.readouterr().out


@pytest.mark.parametrize("workspace", [False, True])
def test_public_requires_cases_and_refuses_workspace_before_loading(monkeypatch, workspace):
    def forbidden(*args):
        pytest.fail("public selection read a workspace")

    monkeypatch.setattr(evaluation, "load_workspace", forbidden)
    args = ["eval", "--profile", "public"]
    if workspace:
        args += ["--cases", str(EVALS), "--workspace", "unreadable-workspace"]
    with pytest.raises(SystemExit) as raised:
        main(args)
    assert raised.value.code == 2


@pytest.mark.parametrize("reference", ["../private.json", "/outside.json", "..\\private.json"])
def test_public_references_cannot_escape(tmp_path, reference):
    shutil.copytree(PUBLIC, tmp_path / "public")
    path = tmp_path / "public/drafts.json"
    listing = json.loads(path.read_text("utf-8"))
    listing["cases"][0]["brief"] = reference
    path.write_text(json.dumps(listing), encoding="utf-8")
    with pytest.raises(ValueError, match="PUBLIC_CASES_INVALID"):
        public_cases(tmp_path)


def test_public_rejects_a_link_even_when_it_stays_inside_root(tmp_path):
    shutil.copytree(PUBLIC, tmp_path / "public")
    directory_link(tmp_path / "public/link", tmp_path / "public/snapshots")
    with pytest.raises(ValueError, match="PUBLIC_CASES_INVALID"):
        public_cases(tmp_path)


def test_public_rejects_empty_or_unreadable_cases(tmp_path):
    with pytest.raises(ValueError, match="PUBLIC_CASES_INVALID"):
        public_cases(tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "public/drafts.json").write_text('{"cases": []}', encoding="utf-8")
    (tmp_path / "public/expected.json").write_text(
        '{"drafts": {}, "stories": {}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="PUBLIC_CASES_INVALID"):
        public_cases(tmp_path)


def test_synthetic_x_preserves_opening_and_appends_scope_verbatim():
    brief = parse_brief((PUBLIC / "drafts/retry.brief.json").read_bytes())
    response = parse_response(
        checked_input((PUBLIC / "drafts/missing-scope.response.json").read_bytes(), brief).data
    )
    post = x_post(brief, response)
    assert post.text.startswith(response.segments[0].text) and x_weight(post.text) <= 280
    stories, drafts = evaluation.load_cases(PUBLIC)
    repaired = next(
        d for d in evaluation.evaluate(stories, drafts).drafts if d.name == "missing-scope"
    )
    value = response.model_dump(mode="json")
    value["segments"].append(
        {
            "id": "S05",
            "text": repaired.repair.texts["S05"],
            "basis": "SOURCE",
            "source_refs": [{"path": "README.md", "line_start": 11, "line_end": 11}],
            "author_refs": [],
            "paragraph_break_before": True,
        }
    )
    after = x_post(brief, parse_response(json.dumps(value).encode()))
    assert after.text.endswith(repaired.repair.texts["S05"]) and x_weight(after.text) <= 280


def test_public_profile_pins_the_p5_voice_cases(replay):
    """C2b, SYNTHETIC: three dry-nerdy drafts that pass every check, a sarcastic FRAMING line with
    a number and one with a quoted name and a code verb (both FRAMING_HAS_CLAIM), and a
    "Takeaway:" line, Must fix in a p5 draft."""
    drafts = {d.name: d for d in replay.drafts}
    for name in ("p5-plain", "p5-code-punchline", "p5-author"):
        assert drafts[name].control and drafts[name].approvable, name
        assert not any(g.blocks for g in drafts[name].gate), name
    assert "FRAMING_HAS_CLAIM" in _blocks(drafts["p5-sarcastic-number"], "S05")
    assert _blocks(drafts["p5-sarcastic-claim"], "S05") == {"FRAMING_HAS_CLAIM"}
    assert _blocks(drafts["p5-pronoun-claim"], "S05") == {"FRAMING_HAS_CLAIM"}  # "It refuses"
    assert _blocks(drafts["p5-labelled"], "S04") == {"LABELLED_SECTION"}
    blocked = ("p5-sarcastic-number", "p5-sarcastic-claim", "p5-pronoun-claim")
    assert not any(drafts[n].approvable for n in blocked)
