"""C2b: drafting prompt p5 for voice dry-nerdy, its length range, FRAMING_HAS_CLAIM and the
structure checks. Offline, synthetic data only (the p4 byte-identity replay on real stored
requests is in tests/private/test_c2b_voice_real.py)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from drafting_fake import model_text
from editorial_fake import ref, seg
from test_automation import REPO, Model, execute
from test_automation import work as automation_work  # noqa: F401  (fixture)

from shipyap.automation import repair, rotation
from shipyap.automation.config import AutomationError, load_config
from shipyap.drafting.prompt import (
    LENGTH_P4,
    LENGTH_P5,
    STORY_SYSTEM_PROMPT,
    length_range,
    prompt_version,
    story_system_prompt,
)
from shipyap.drafting.story_draft import _prefer_other_repo, drafted_format, pick_format
from shipyap.editorial import voice
from shipyap.editorial.grounding import check_draft, framing_claims
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.models import FindingCode, Segment
from shipyap.review.gate import judge

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public" / "drafts"
STYLE = (
    "Short sentences. One dry line at the end.\n\nExample: The cache was fine. The clock was not."
)
P5 = "SYNTHETIC fixture via draft-model p5"
P4 = "SYNTHETIC fixture via draft-model p4"


# --- the prompt --------------------------------------------------------------------------------


def test_plain_is_p4_byte_for_byte_whatever_else_is_passed():
    assert story_system_prompt() is STORY_SYSTEM_PROMPT
    assert story_system_prompt("plain", STYLE, "code-punchline") is STORY_SYSTEM_PROMPT
    assert prompt_version(STORY_SYSTEM_PROMPT) == "p4"


@pytest.mark.parametrize("post_format", ["plain", "code-punchline"])
def test_p5_carries_the_style_file_the_format_and_every_p4_evidence_rule(post_format):
    prompt = story_system_prompt("dry-nerdy", STYLE, post_format)
    assert prompt_version(prompt) == "p5"
    assert prompt.endswith("<<<AUTHOR STYLE\n" + STYLE + "\nAUTHOR STYLE>>>\n")
    assert f"Format for this post: {post_format}." in prompt
    assert "300 to 800 characters" in prompt and "800 to 1,200" not in prompt
    assert "FRAMING_HAS_CLAIM" in prompt and '"Takeaway:"' in prompt
    start = STORY_SYSTEM_PROMPT.index("- The brief's limitations list each source file's role.")
    assert STORY_SYSTEM_PROMPT[start:] in prompt  # the evidence rules, unchanged


@pytest.mark.parametrize(
    ("voice_name", "style", "post_format"),
    [
        ("dry-nerdy", "", "plain"),
        ("dry-nerdy", "x" * 6001, "plain"),
        ("dry-nerdy", STYLE, "thread"),
        ("loud", STYLE, "plain"),
    ],
    ids=["no-style", "style-too-long", "unknown-format", "unknown-voice"],
)
def test_p5_refuses_a_missing_or_oversized_style_file_and_unknown_names(
    voice_name, style, post_format
):
    with pytest.raises(ValueError):
        story_system_prompt(voice_name, style, post_format)


# --- config ------------------------------------------------------------------------------------


def _dry_nerdy(path: Path, style_name: str | None = "author-style.md", text: str = STYLE):
    extra = "\nvoice = 'dry-nerdy'\n"
    if style_name is not None:
        (path.parent / style_name).write_bytes(text.encode("utf-8"))
        extra += f"author_style_file = '{style_name}'\n"
    path.write_text(path.read_text("utf-8") + extra, encoding="utf-8")
    return load_config(path)


def test_dry_nerdy_reads_the_style_file_from_the_workspace(automation_work):  # noqa: F811
    _, path = automation_work
    cfg = _dry_nerdy(path, text="  " + STYLE.replace("\n", "\r\n") + "\n\n")
    assert cfg.voice == "dry-nerdy" and cfg.author_style == STYLE


@pytest.mark.parametrize(
    "case", ["missing setting", "missing file", "outside workspace", "empty", "too long", "key"]
)
def test_a_bad_style_setting_is_refused(automation_work, tmp_path, case):  # noqa: F811
    _, path = automation_work
    with pytest.raises(AutomationError, match="CONFIG_INVALID"):
        if case == "missing setting":
            _dry_nerdy(path, None)
        elif case == "missing file":
            path.write_text(
                path.read_text("utf-8") + "\nvoice = 'dry-nerdy'\nauthor_style_file = 'no.md'\n",
                encoding="utf-8",
            )
            load_config(path)
        elif case == "outside workspace":
            _dry_nerdy(path, "../outside-style.md")
        elif case == "empty":
            _dry_nerdy(path, text="   \n")
        elif case == "too long":
            _dry_nerdy(path, text="x" * 6001)
        else:
            path.write_text(path.read_text("utf-8") + "\nauthor_style = 'x'\n", encoding="utf-8")
            load_config(path)


# --- drafting through the engine ---------------------------------------------------------------


def test_a_dry_nerdy_run_sends_p5_and_records_the_rotation(automation_work):  # noqa: F811
    cfg, path = automation_work
    cfg = _dry_nerdy(path)
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.drafts_created == 1, result.failures
    draft_request = [r for r in model.generations if r not in model.discovery_calls][0]
    assert prompt_version(draft_request.system_prompt) == "p5"
    assert STYLE in draft_request.system_prompt
    assert "Format for this post: plain." in draft_request.system_prompt
    response = json.loads((result.draft_paths[0] / "response.json").read_text("utf-8"))
    assert "via draft-model p5" in response["generator"]["label"]
    last = rotation.load(cfg.state).last
    assert (last.repo, last.format, last.voice) == (REPO, "plain", "dry-nerdy")


def test_a_plain_run_sends_p4_and_keeps_no_rotation(automation_work):  # noqa: F811
    cfg, _ = automation_work
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.drafts_created == 1, result.failures
    draft_request = [r for r in model.generations if r not in model.discovery_calls][0]
    assert draft_request.system_prompt == STORY_SYSTEM_PROMPT
    assert not (cfg.state / rotation.ROTATION_FILE).exists()


def _story(*paths, repo="owner/one", sid="a"):
    refs = [SimpleNamespace(path=p) for p in paths]
    return SimpleNamespace(
        story=SimpleNamespace(story_id=sid * 16, repo_id=repo, latest=None),
        latest=SimpleNamespace(refs=refs),
    )


def test_the_format_alternates_and_a_punchline_needs_code_in_the_story():
    code = _story("src/retry.py")
    docs = _story("README.md", "docs/RESULTS.md")
    assert pick_format(code, None) == "plain"
    assert pick_format(code, "plain") == "code-punchline"
    assert pick_format(code, "code-punchline") == "plain"
    assert pick_format(docs, "plain") == "plain"  # no code line to open with


def test_the_last_repository_goes_behind_the_others_unless_a_story_is_in_progress(tmp_path):
    one = SimpleNamespace(story=SimpleNamespace(story_id="a" * 16, repo_id="owner/one"))
    two = SimpleNamespace(story=SimpleNamespace(story_id="b" * 16, repo_id="owner/two"))
    assert _prefer_other_repo([one, two], "owner/one", tmp_path) == [two, one]
    assert _prefer_other_repo([one], "owner/one", tmp_path) == [one]  # the only choice
    assert _prefer_other_repo([one, two], None, tmp_path) == [one, two]
    (tmp_path / f"{'a' * 16}-attempt").mkdir()
    assert _prefer_other_repo([one, two], "owner/one", tmp_path) == [one, two]


def test_the_drafted_format_is_read_from_the_draft_not_the_request():
    line = "return { score: result.score, source: 'model' };"
    punch = seg("S01", line, "SOURCE", src=[ref("api/review.ts", 16, 16, quote=line)])
    assert drafted_format([Segment.model_validate(punch)]) == "code-punchline"
    other = seg("S01", "The route returns.", "SOURCE", src=[ref("api/review.ts", 16, 16)])
    assert drafted_format([Segment.model_validate(other)]) == "plain"
    doc = seg("S01", line, "SOURCE", src=[ref("README.md", 1, 1, quote=line)])
    assert drafted_format([Segment.model_validate(doc)]) == "plain"
    assert drafted_format([]) == "plain"


def test_a_repeat_the_loop_could_not_avoid_is_recorded_with_allow_repeat(tmp_path):
    first = rotation.Entry("ab" * 16, "owner/one", "plain", "dry-nerdy")
    rotation.record(tmp_path, first)
    again = rotation.Entry("cd" * 16, "owner/one", "plain", "dry-nerdy")
    with pytest.raises(rotation.RotationError, match="FORMAT_REPEATED"):
        rotation.record(tmp_path, again)
    assert rotation.record(tmp_path, again, allow_repeat=True).last == again
    unknown = rotation.Entry("ef" * 16, "owner/two", "thread", "dry-nerdy")
    with pytest.raises(rotation.RotationError, match="ENTRY_INVALID"):
        rotation.record(tmp_path, unknown, allow_repeat=True)


# --- length ------------------------------------------------------------------------------------


def _public(name: str):
    brief = parse_brief((PUBLIC / "refund.brief.json").read_bytes())
    data = (PUBLIC / f"{name}.response.json").read_bytes()
    return brief, parse_response(data), data


def _relabel(response, label):
    data = response.model_dump(mode="json")
    data["generator"]["label"] = label
    return parse_response(json.dumps(data).encode())


def test_the_length_range_depends_on_the_drafting_prompt():
    assert length_range(P5) == LENGTH_P5 == (300, 800)
    assert length_range(P4) == LENGTH_P4 == (800, 1300)
    assert length_range("SYNTHETIC hand-authored model-shaped fixture") == LENGTH_P4


def test_after_a_drop_a_p5_draft_is_shelved_under_300_and_a_p4_draft_under_800():
    brief, before, _ = _public("p5-plain")
    after = before.model_copy(update={"segments": before.segments[:3]})
    p5 = repair.drop_problems(brief, before, after)
    assert len(p5) == 1 and p5[0].startswith("after dropping S04, S05 the post is ")
    assert p5[0].endswith("characters, under 300")
    longer = before.model_copy(update={"segments": before.segments[:4]})
    assert repair.drop_problems(brief, before, longer) == []  # over 300: it stands
    p4 = repair.drop_problems(brief, _relabel(before, P4), longer)
    assert p4[0].endswith("characters, under 800")


# --- FRAMING_HAS_CLAIM -------------------------------------------------------------------------

PASSES = (
    "Naming things remains the hard part.",
    "Credit where credit is not due.",
    "Optimism, but make it a button.",
    "Not a bug.",
    "Computers, honestly.",
    # C2c: a factual verb counts only after the repository, its code, tests or tool.
    "A scaffold that says it is a scaffold is a pleasant change.",
    "A scaffold that says it is a scaffold is easy to read accurately.",
    "Easy to test, hard to trust.",
)
BLOCKS = {
    "A solid 40 points of borrowed confidence.": "number 40",
    "Three cheers for honesty.": "number Three",
    'The badge says "Model Review" anyway.': 'quoted "Model Review"',
    "Bold move, returning 'model' no matter who did the work.": "quoted 'model'",
    "Blame desk.tsx, as usual.": "names desk.tsx",
    "Thanks, parse_reply().": "names parse_reply()",
    "Classic ruleCheck energy.": "names ruleCheck",
    "Somewhere a MAX_RETRIES is sulking.": "names MAX_RETRIES",
    "The tool refuses, politely.": "states what it does: tool refuses",
    "It refuses, politely.": "states what it does: It refuses",
    "The tests also keep them apart.": "states what it does: tests keep",
}


@pytest.mark.parametrize("text", PASSES)
def test_a_framing_line_without_a_claim_passes(text):
    assert framing_claims(text, ["app/desk.tsx"]) == []


@pytest.mark.parametrize("text", sorted(BLOCKS))
def test_a_framing_line_with_a_claim_names_what_it_carries(text):
    assert any(BLOCKS[text] in found for found in framing_claims(text, ["app/desk.tsx"]))


def _check(name, label=None):
    brief, response, data = _public(name)
    if label is not None:
        response = _relabel(response, label)
        data = json.dumps(response.model_dump(mode="json")).encode()
    sha = hashlib.sha256((PUBLIC / "refund.brief.json").read_bytes()).hexdigest()
    amap = check_draft(brief, response, sha, hashlib.sha256(data).hexdigest())
    return amap, judge(amap, response)


def _codes(amap, sid):
    return {f.code for s in amap.segments if s.id == sid for f in s.findings}


@pytest.mark.parametrize("name", ["p5-plain", "p5-code-punchline", "p5-author"])
def test_the_p5_fixtures_pass_every_check(name):
    amap, gate = _check(name)
    assert gate.approvable and amap.draft_status.value == "READY_FOR_REVIEW"
    _, response, _ = _public(name)
    assert LENGTH_P5[0] <= len("\n\n".join(s.text for s in response.segments)) <= LENGTH_P5[1]


def test_a_sarcastic_line_with_a_claim_blocks_only_in_a_p5_draft():
    amap, gate = _check("p5-sarcastic-claim")
    assert FindingCode.FRAMING_HAS_CLAIM in _codes(amap, "S05") and not gate.approvable
    amap, gate = _check("p5-sarcastic-claim", label=P4)  # the same words from p4
    assert FindingCode.FRAMING_HAS_CLAIM not in _codes(amap, "S05") and gate.approvable


def test_a_sarcastic_number_is_blocked_twice_over():
    amap, gate = _check("p5-sarcastic-number")
    assert {FindingCode.FRAMING_HAS_CLAIM, FindingCode.NUMBER_NOT_IN_EVIDENCE} <= _codes(
        amap, "S05"
    )
    assert not gate.approvable


# --- labelled sections and Markdown structure --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Cause: the cache.", "VOICE_LABELLED_SECTION"),
        ("**Fix:** narrower.", "VOICE_LABELLED_SECTION"),
        ("Lesson: read the code.", "VOICE_LABELLED_SECTION"),
        ("TL;DR the route lies.", "VOICE_LABELLED_SECTION"),
        ("Impact: none.", "VOICE_LABELLED_SECTION"),
        ("Takeaway: cite it.", "VOICE_LABELLED_SECTION"),
        ("## What happened", "VOICE_MARKDOWN_STRUCTURE"),
        ("- the first point", "VOICE_MARKDOWN_STRUCTURE"),
        ("2) the second point", "VOICE_MARKDOWN_STRUCTURE"),
    ],
)
def test_structure_lines_are_noted(text, code):
    segment = Segment.model_validate(seg("S01", text, "FRAMING"))
    assert [n.code for n in voice.structure_notes(segment)] == [code]


@pytest.mark.parametrize(
    "text", ["The fix was one line.", "#AIAgents", "2026 was the year.", "A cause for concern."]
)
def test_ordinary_sentences_are_not_structure(text):
    assert voice.structure_notes(Segment.model_validate(seg("S01", text, "FRAMING"))) == []


def test_a_labelled_section_is_must_fix_for_p5_and_a_warning_for_p4():
    _, gate = _check("p5-labelled")
    s04 = next(s for s in gate.segments if s.segment_id == "S04")
    assert [n.code for n in s04.blocks] == ["LABELLED_SECTION"] and not gate.approvable
    _, gate = _check("p5-labelled", label=P4)
    s04 = next(s for s in gate.segments if s.segment_id == "S04")
    assert not s04.blocks and "VOICE_LABELLED_SECTION" in [n.code for n in s04.warnings]
    assert gate.approvable


def test_the_page_counts_a_p5_structure_block_as_needing_attention():
    from shipyap.review.ux import REASONS, attention_count

    _, strict = _check("p5-labelled")
    _, plain = _check("p5-labelled", label=P4)
    assert attention_count(strict) == attention_count(plain) + 1  # the style warning never counts
    assert {"LABELLED_SECTION", "MARKDOWN_STRUCTURE", "FRAMING_HAS_CLAIM"} <= REASONS.keys()


def test_model_text_helper_still_builds_p5_shaped_replies():
    """The fake model can return a FRAMING line; the strict parser takes it as before."""
    text = model_text([seg("S01", "Naming things remains the hard part.", "FRAMING")])
    assert json.loads(text)["segments"][0]["basis"] == "FRAMING"


def test_the_shipped_example_style_file_is_a_valid_style(automation_work):  # noqa: F811
    _, path = automation_work
    example = Path(__file__).resolve().parents[1] / "examples" / "author-style.example.md"
    text = example.read_text("utf-8")
    assert "SYNTHETIC" in text
    cfg = _dry_nerdy(path, text=text)
    assert prompt_version(story_system_prompt("dry-nerdy", cfg.author_style)) == "p5"
