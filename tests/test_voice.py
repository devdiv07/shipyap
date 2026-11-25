"""C2a: voice and readability warnings, the voice setting, rotation memory and the approval
rating. Offline, synthetic data only; the replay on the owner's real drafts is in
tests/private/test_voice_real.py."""

import json

import pytest
from test_automation import work as automation_work  # noqa: F401  (fixture)
from test_review import (
    CHANNEL,
    DUE,
    NOW,
    refused,
)
from test_review import (
    serve as serve,
)
from test_review import (
    work as work,
)
from test_review_ux import confirm, sheet

from shipyap.automation import rotation
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.evaluation import EvalReport, load_decisions, render
from shipyap.editorial import voice
from shipyap.editorial.models import Segment
from shipyap.publication.approval import confirmation_phrase
from shipyap.review.gate import Gate, Note, SegmentGate, is_style
from shipyap.review.service import ReviewDesk, ReviewErrorCode, parse_rating
from shipyap.review.ux import REASONS, attention_count


def seg(sid: str, text: str, brk: bool = False) -> Segment:
    return Segment.model_validate(
        {
            "id": sid,
            "paragraph_break_before": brk,
            "text": text,
            "basis": "FRAMING",
            "source_refs": [],
            "author_refs": [],
        }
    )


def codes(*texts: str) -> list[str]:
    segments = [seg(f"S{i:02d}", t, brk=i > 1) for i, t in enumerate(texts, start=1)]
    return [n.code for n in voice.check_voice(segments)]


PLAIN = (
    "The protocol records the change before the next request.",
    "It lists what stayed fixed.",
    "A reader can check the timing for themselves, because every step of the change was "
    "written down on the day it happened.",
)


# --- the checks --------------------------------------------------------------------------------


def test_a_plain_post_gets_no_warning():
    assert codes(*PLAIN) == []


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Here's the thing: the test caught it.", "VOICE_CLICHE"),
        ("What surprised me was the order of the calls.", "VOICE_CLICHE"),
        ("The release went out 🚀 on the first try.", "VOICE_CLICHE"),
        ("We leveraged the cache to cut the calls.", "VOICE_AI_WORD"),
        ("The harness is robust to a missing key.", "VOICE_AI_WORD"),
        ("Let me delve into the record.", "VOICE_AI_WORD"),
        ("It's not a bug, it's a missing check.", "VOICE_CONTRAST"),
        ("The record is not just a log but a contract.", "VOICE_CONTRAST"),
        ("The check — the one that failed — ran twice — then stopped.", "VOICE_EM_DASHES"),
        ("Ever thought about what a screenshot proves?", "VOICE_QUESTION_OPENER"),
        ("✅ The test passed on the second try.", "VOICE_EMOJI_BULLET"),
    ],
)
def test_each_voice_check_fires_on_its_pattern(text, code):
    assert code in codes(text)


def test_one_em_dash_and_a_factual_contrast_are_fine():
    assert codes("The check ran — then stopped.") == []
    assert "VOICE_CONTRAST" not in codes("Case 13b is a false accept, not an abstention.")


def test_a_summary_closer_opens_the_last_paragraph_only():
    assert "VOICE_SUMMARY_CLOSER" in codes(*PLAIN, "Ultimately, the record decides.")
    assert "VOICE_SUMMARY_CLOSER" not in codes("Ultimately the record decides.", *PLAIN)


def test_triads_warn_once_two_sentences_carry_one():
    one = "The run kept the model, the prompt and the schema."
    two = "The note is short, dated and signed."
    assert "VOICE_TRIADS" not in codes(one, *PLAIN)
    assert "VOICE_TRIADS" in codes(one, two)
    # A longer list is a list, not a triad.
    assert voice._triad("It kept the model, system prompt, tool schema and max turns.") is None


def test_flat_rhythm_needs_five_sentences_all_about_the_same_length():
    same = ["The run stopped at the first failed call."] * 5
    assert "VOICE_FLAT_RHYTHM" in codes(*same)
    assert "VOICE_FLAT_RHYTHM" not in codes(*same[:4])
    assert "VOICE_FLAT_RHYTHM" not in codes(*same[:4], "It stopped.")


def test_readability_opener_long_sentences_and_jargon():
    long_opener = " ".join(["word"] * 21) + "."
    assert "READ_OPENER_LONG" in codes(long_opener)
    assert "READ_OPENER_LONG" not in codes(" ".join(["word"] * 20) + ".")
    assert "READ_LONG_SENTENCE" in codes(*PLAIN, " ".join(["word"] * 31) + ".")
    assert "READ_OPENER_JARGON" in codes("The GRANT_A handle reaches run_canary.py first.")
    assert "READ_OPENER_JARGON" not in codes("The API returned a 640-character token in 2024.")
    dense = "The FOO_BAR and BazQux call qux_v2 via lib/x.ts and the ZZZ flag."
    assert "READ_JARGON_DENSITY" in codes(dense)
    assert voice.jargon_score([seg("S01", dense)]) > voice.JARGON_DENSITY_MAX
    assert voice.jargon_score([]) == 0.0


def test_the_word_lists_live_in_one_data_file_and_load():
    found = voice.lists()
    assert found.cliches is not None and found.ai_words is not None and found.closers
    assert set(found.formats) == {"plain", "dry-nerdy"}
    assert "I" in found.plain_acronyms and "🚀" in found.cliche_emoji


def test_every_code_has_a_message_and_a_plain_reason_on_the_page():
    assert set(voice.CODES) == set(voice.MESSAGES)
    assert set(voice.CODES) <= REASONS.keys()


def test_sentences_keep_decimals_and_abbreviations_whole():
    assert voice.sentences("It took 2.5 s, e.g. on one run. Then it stopped.") == [
        "It took 2.5 s, e.g. on one run.",
        "Then it stopped.",
    ]


# --- in the gate: warnings only ----------------------------------------------------------------


def test_style_notes_never_need_attention_and_never_block():
    style = Note("READ_OPENER_LONG", "25 words")
    gate = Gate((SegmentGate("S01", (), (style,)),), (), (Note("VOICE_TRIADS", "x"),))
    assert is_style(style) and gate.approvable and attention_count(gate) == 0
    other = Gate((SegmentGate("S01", (), (style, Note("SPELLED_NUMBER", "two"))),))
    assert attention_count(other) == 1


def test_a_draft_with_style_warnings_is_still_approved_and_rated(work, tmp_path):
    desk = ReviewDesk(
        work.drafts, work.backlog, work.ledger, (work.snapshot,), eval_cases=tmp_path / "cases"
    )
    desk.fact_checks = work.desk().fact_checks
    item = desk.item(work.sid)
    candidate = desk.approval_candidate(work.sid, CHANNEL, DUE, NOW)
    typed = confirmation_phrase(candidate.approval_id)
    assert refused(desk.approve, work.sid, CHANNEL, DUE, typed, item.seq, NOW, rating=6) == (
        ReviewErrorCode.RATING_INVALID
    )
    assert not list((tmp_path / "cases").glob("*.json"))  # nothing recorded
    desk.approve(work.sid, CHANNEL, DUE, typed, item.seq, NOW, rating=4)
    (case,) = (tmp_path / "cases").glob("*-APPROVED.json")
    assert json.loads(case.read_text("utf-8"))["rating"] == 4


def test_a_decision_without_a_rating_has_none_and_eval_shows_ratings(tmp_path):
    cases = tmp_path / "automation" / "eval-cases"
    cases.mkdir(parents=True)
    base = {"case_version": "1", "decision": "APPROVED", "segments": [], "approvable_before": True}
    (cases / "a-0001-APPROVED.json").write_text(json.dumps({**base, "rating": 5}), "utf-8")
    (cases / "b-0002-APPROVED.json").write_text(json.dumps(base), "utf-8")
    (cases / "c-0003-APPROVED.json").write_text(json.dumps({**base, "rating": "5"}), "utf-8")
    decisions = load_decisions(tmp_path)
    assert [d.rating for d in decisions] == [5, None, None]
    lines = render(EvalReport(decisions=decisions))
    assert lines[0].endswith("approvable_before=True rating=5/5")
    assert lines[1].endswith("approvable_before=True")


@pytest.mark.parametrize(("value", "want"), [(None, None), ("", None), ("1", 1), ("5", 5)])
def test_parse_rating(value, want):
    assert parse_rating(value) == want


@pytest.mark.parametrize("value", ["0", "6", "4.5", " 4", "five"])
def test_parse_rating_refuses_anything_else(value):
    assert refused(parse_rating, value) == ReviewErrorCode.RATING_INVALID


def test_the_approval_sheet_offers_the_optional_rating_and_the_form_accepts_it(work, serve):
    client = serve(work.desk())
    code, _, html = client.post(f"/story/{work.sid}/sheet", seq="0", channel_id=CHANNEL, due_at=DUE)
    assert code == 200 and "Would I post this proudly?" in html
    assert html.count("type=radio name=rating") == 5
    key = sheet(client, work.sid)
    assert confirm(client, work.sid, key, rating="9")[0] == 409
    assert confirm(client, work.sid, key, rating="3")[0] == 303


# --- voice setting -----------------------------------------------------------------------------


def test_voice_defaults_to_plain_and_accepts_dry_nerdy(automation_work):  # noqa: F811
    cfg, path = automation_work
    assert cfg.voice == "plain"
    (path.parent / "style.md").write_text("Short and dry.", encoding="utf-8")
    path.write_text(
        path.read_text("utf-8") + "\nvoice = 'dry-nerdy'\nauthor_style_file = 'style.md'\n",
        encoding="utf-8",
    )
    assert load_config(path).voice == "dry-nerdy"  # C2b: only with the author's style file


@pytest.mark.parametrize("value", ["'loud'", "''", "3"])
def test_an_unknown_voice_is_refused(automation_work, value):  # noqa: F811
    _, path = automation_work
    path.write_text(path.read_text("utf-8") + f"\nvoice = {value}\n", encoding="utf-8")
    with pytest.raises(AutomationError):
        load_config(path)


# --- rotation memory ---------------------------------------------------------------------------

STORY = "ab" * 32


def entry(repo="owner/one", fmt="lesson", voice_name="plain", story=STORY):
    return rotation.Entry(story, repo, fmt, voice_name)


def test_rotation_never_repeats_the_format_or_the_repo(tmp_path):
    memory = rotation.load(tmp_path)
    assert memory.last is None and memory.next_format("plain") == "lesson"
    assert memory.repo_allowed("owner/one")
    memory = rotation.record(tmp_path, entry())
    assert memory.next_format("plain") == "before-after"
    assert not memory.repo_allowed("owner/one") and memory.repo_allowed("owner/two")
    with pytest.raises(rotation.RotationError, match="FORMAT_REPEATED"):
        rotation.record(tmp_path, entry(repo="owner/two"))
    with pytest.raises(rotation.RotationError, match="REPO_REPEATED"):
        rotation.record(tmp_path, entry(fmt="mistake"))
    memory = rotation.record(tmp_path, entry(repo="owner/two", fmt="how-it-works"))
    assert memory.next_format("plain") == "lesson"  # wraps round
    assert rotation.load(tmp_path) == memory  # what was written is what loads


def test_rotation_refuses_unknown_or_invalid_entries_and_writes_nothing(tmp_path):
    for bad in (entry(fmt="listicle"), entry(voice_name="loud"), entry(repo="not a repo")):
        with pytest.raises(rotation.RotationError, match="ENTRY_INVALID"):
            rotation.record(tmp_path, bad)
    assert not (tmp_path / rotation.ROTATION_FILE).exists()
    with pytest.raises(rotation.RotationError, match="FORMAT_UNKNOWN"):
        rotation.Rotation().next_format("loud")


@pytest.mark.parametrize(
    "content",
    ["not json", "{}", '{"rotation_version": "2", "entries": []}', '{"rotation_version": "1"}'],
)
def test_a_corrupt_rotation_file_is_refused(tmp_path, content):
    (tmp_path / rotation.ROTATION_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(rotation.RotationError, match="ROTATION_UNREADABLE"):
        rotation.load(tmp_path)


def test_rotation_keeps_a_bounded_history(tmp_path):
    formats = rotation.formats("plain")
    for index in range(rotation.KEEP + 3):
        rotation.record(
            tmp_path, entry(repo=f"owner/r{index % 2}", fmt=formats[index % len(formats)])
        )
    assert len(rotation.load(tmp_path).entries) == rotation.KEEP
