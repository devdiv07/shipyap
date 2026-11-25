"""C2c: the repository link closing line, owner attribution (built_by_me), subject-aware
FRAMING_HAS_CLAIM, trim-first repair of a dry line, and change-first story ranking for voice
dry-nerdy. Offline, synthetic data only (the replay on the two real p5 trial drafts is pinned in
evals/expected.json and tests/private/test_c2c_trial_real.py)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_automation import REPO, Model, execute
from test_automation import work as automation_work  # noqa: F401  (fixture)

from shipyap.automation import repair
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.evaluation import FixtureDrafter
from shipyap.drafting.prompt import (
    STORY_SYSTEM_PROMPT,
    p5_body_limit,
    prompt_version,
    story_system_prompt,
)
from shipyap.drafting.story_draft import is_change_story, prefer_change
from shipyap.editorial.closing import (
    CLOSING_ID,
    DEFAULT_BUILT_BY_ME,
    add_closing,
    closing_text,
    is_closing,
    split_style,
)
from shipyap.editorial.grounding import check_draft, framing_claims, render_post
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.models import MAX_SEGMENTS, FindingCode
from shipyap.editorial.sources import load_verified_snapshot
from shipyap.publication.x import x_weight
from shipyap.review.gate import judge
from shipyap.review.required import completed_fact_check
from shipyap.review.x_post import x_post

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public"
URL = "https://github.com/synthetic-owner/synthetic-repo"
STYLE = "Short sentences. One dry line at the end."


def _public(name: str):
    brief = parse_brief((PUBLIC / "drafts" / "refund.brief.json").read_bytes())
    return brief, (PUBLIC / "drafts" / f"{name}.response.json").read_bytes()


def _closed(name: str, text: str | None = None):
    brief, data = _public(name)
    url = brief.snapshot.source_url
    data = add_closing(data, text or url, url, MAX_SEGMENTS)
    return brief, parse_response(data), data


def _amap(brief, response, data):
    sha = hashlib.sha256((PUBLIC / "drafts" / "refund.brief.json").read_bytes()).hexdigest()
    return check_draft(brief, response, sha, hashlib.sha256(data).hexdigest())


# --- the closing line ---------------------------------------------------------------------------


def test_the_closing_is_the_url_or_the_owners_wording_around_it():
    assert closing_text(URL, False) == URL
    assert closing_text(URL, True) == f"Built this one myself: {URL}"
    assert closing_text(URL, True, "Mine, all of it: {url}") == f"Mine, all of it: {URL}"
    with pytest.raises(ValueError):
        closing_text("https://example.com/x", False)


@pytest.mark.parametrize(
    "wording",
    ["{url} is mine", "Built: {url} {url}", "No link here", "See http://x {url}", "a\nb {url}"],
)
def test_a_wording_that_is_not_one_short_line_ending_in_the_url_is_refused(wording):
    with pytest.raises(ValueError):
        closing_text(URL, True, wording)


def test_the_style_file_wording_is_taken_out_of_the_style_text():
    text = f"{STYLE}\n\nBuilt-by-me closing: All mine: {{url}}\n\nExample: a post."
    style, wording = split_style(text)
    assert wording == "All mine: {url}" and "closing" not in style.casefold()
    assert style == f"{STYLE}\n\nExample: a post."
    assert split_style(STYLE) == (STYLE, DEFAULT_BUILT_BY_ME)  # no line: text unchanged
    with pytest.raises(ValueError):
        split_style("Built-by-me closing: a: {url}\nBuilt-by-me closing: b: {url}")


def test_add_closing_appends_once_and_removes_a_model_written_one():
    brief, data = _public("p5-plain")
    url = brief.snapshot.source_url
    assert add_closing(data, None, url, MAX_SEGMENTS) == data  # off: byte for byte
    closed = parse_response(add_closing(data, url, url, MAX_SEGMENTS))
    assert closed.segments[-1].id == CLOSING_ID and closed.segments[-1].text == url
    assert closed.segments[-1].paragraph_break_before
    # A model that wrote the reserved id, or the bare link at the end, does not keep either.
    forged = json.loads(data)
    forged["segments"].append(
        {"id": CLOSING_ID, "text": f"Built this one myself: {url}", "basis": "FRAMING"}
    )
    again = parse_response(add_closing(json.dumps(forged).encode(), url, url, MAX_SEGMENTS))
    assert [s.text for s in again.segments].count(url) == 1
    assert not any("myself" in s.text for s in again.segments)
    bare = json.loads(data)
    bare["segments"].append({"id": "S40", "text": url + ".", "basis": "FRAMING"})
    once = parse_response(add_closing(json.dumps(bare).encode(), url, url, MAX_SEGMENTS))
    assert sum(url in s.text for s in once.segments) == 1


def test_a_no_post_reply_or_unreadable_bytes_get_no_closing():
    assert add_closing(b"not json", URL, URL, MAX_SEGMENTS) == b"not json"
    empty = json.dumps({"segments": []}).encode()
    assert add_closing(empty, URL, URL, MAX_SEGMENTS) == empty


@pytest.mark.parametrize("built_by_me", [False, True])
def test_the_checker_never_judges_the_closing_and_the_draft_still_passes(built_by_me):
    brief, data = _public("p5-plain")
    text = closing_text(brief.snapshot.source_url, built_by_me)
    brief, response, data = _closed("p5-plain", text)
    amap = _amap(brief, response, data)
    record = amap.segments[-1]
    assert record.id == CLOSING_ID and record.findings == ()  # "myself" is not first person here
    assert judge(amap, response).approvable
    assert text in "\n\n".join(s.text for s in response.segments)  # it counts toward length


def test_a_closing_shaped_line_that_is_not_last_or_names_another_repo_is_checked():
    brief, data = _public("p5-plain")
    raw = json.loads(data)
    other = "https://github.com/someone-else/elsewhere"
    raw["segments"].append(
        {"id": CLOSING_ID, "text": f"Built this one myself: {other}", "basis": "FRAMING"}
    )
    response = parse_response(json.dumps(raw).encode())
    amap = _amap(brief, response, json.dumps(raw).encode())
    codes = {f.code for f in amap.segments[-1].findings}
    assert FindingCode.LINK_NOT_ALLOWED in codes  # judged like any other sentence


def test_the_fact_check_is_complete_without_a_verdict_for_the_closing():
    brief, response, _ = _closed("p5-plain")
    verdicts = [
        {"segment_id": s.id, "verdict": "SUPPORTED", "reason": "x", "refs": []}
        for s in response.segments
        if s.id != CLOSING_ID
    ]
    assert is_closing(response.segments[-1], brief.snapshot.source_url)
    assert completed_fact_check({"verdicts": verdicts}, response)
    assert not completed_fact_check({"verdicts": verdicts[:-1]}, response)


def test_x_gets_the_link_when_it_fits_and_keeps_its_text_when_it_does_not():
    brief, data = _public("p5-plain")
    plain = x_post(brief, parse_response(data))
    assert plain.text is not None
    url = brief.snapshot.source_url
    # Short enough: the X text of the post without the link, then the link.
    short = json.loads(data)
    short["segments"] = short["segments"][:2]
    short_data = json.dumps(short).encode()
    short_x = x_post(brief, parse_response(short_data))
    assert short_x.text is not None and x_weight(f"{short_x.text}\n\n{url}") <= 280
    linked = x_post(brief, parse_response(add_closing(short_data, url, url, MAX_SEGMENTS)))
    assert linked.text == f"{short_x.text}\n\n{url}" and linked.segment_ids[-1] == CLOSING_ID
    # Too long with the link: X keeps today's text, without it.
    assert x_weight(f"{plain.text}\n\n{url}") > 280
    _, closed, _ = _closed("p5-plain")
    assert x_post(brief, closed) == plain


# --- config and the engine ------------------------------------------------------------------------


def _toml(path, extra: str, author: str = "") -> object:
    (path.parent / "author-style.md").write_text(STYLE, encoding="utf-8")
    path.write_text(path.read_text("utf-8") + extra + author, encoding="utf-8")
    return load_config(path)


AUTHOR = f"""
[author]
name = "Synthetic Owner"
[[author.repos]]
repo = "{REPO}"
relationship = "primary_author"
statements = ["I built this repository to test synthetic screenshots."]
built_by_me = true
"""
DRY = "\nvoice = 'dry-nerdy'\nauthor_style_file = 'author-style.md'\n"


def test_link_repo_defaults_on_for_dry_nerdy_and_off_for_plain(automation_work):  # noqa: F811
    cfg, path = automation_work
    assert cfg.link_repo is None and not cfg.links_repo
    cfg = _toml(path, DRY)
    assert cfg.links_repo
    assert not _toml(path, "link_repo = false\n").links_repo


@pytest.mark.parametrize("bad", ["link_repo = 'yes'\n", "built_by_me_line = 'x'\n"])
def test_a_bad_link_setting_is_refused(automation_work, bad):  # noqa: F811
    _, path = automation_work
    with pytest.raises(AutomationError, match="CONFIG_INVALID"):
        _toml(path, bad)


def test_built_by_me_must_be_a_boolean(automation_work):  # noqa: F811
    _, path = automation_work
    with pytest.raises(AutomationError, match="CONFIG_INVALID"):
        _toml(path, DRY, AUTHOR.replace("built_by_me = true", "built_by_me = 'yes'"))


def _closing_of(result):
    response = json.loads((result.draft_paths[0] / "response.json").read_text("utf-8"))
    last = response["segments"][-1]
    return last["text"] if last["id"] == CLOSING_ID else None


def test_a_dry_nerdy_run_ends_with_the_repo_link_never_sent_to_the_fact_check(
    automation_work,  # noqa: F811
):
    cfg, path = automation_work
    cfg = _toml(path, DRY)
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.drafts_created == 1, result.failures
    assert _closing_of(result) == URL
    draft = (result.draft_paths[0] / "draft.txt").read_text("utf-8")
    assert draft.endswith(f"\n\n{URL}\n")
    assert model.fact_checks and all(CLOSING_ID not in r.user_text for r in model.fact_checks)
    sent = [r for r in model.generations if r not in model.discovery_calls][0]
    assert f"300 to {p5_body_limit(URL)} characters for what you write" in sent.system_prompt


def test_built_by_me_credits_the_owner_only_for_that_repository(automation_work):  # noqa: F811
    cfg, path = automation_work
    style = f"{STYLE}\n\nBuilt-by-me closing: Made this one: {{url}}\n"
    (path.parent / "author-style.md").write_text(style, encoding="utf-8")
    path.write_text(path.read_text("utf-8") + DRY + AUTHOR, encoding="utf-8")
    cfg = load_config(path)
    assert "Made this one" not in cfg.author_style  # the wording never reaches the prompt
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert _closing_of(result) == f"Made this one: {URL}", result.failures
    sent = [r for r in model.generations if r not in model.discovery_calls][0]
    assert "Made this one" not in sent.system_prompt + sent.user_text


def test_without_built_by_me_the_closing_is_the_bare_link(automation_work):  # noqa: F811
    cfg, path = automation_work
    cfg = _toml(path, DRY, AUTHOR.replace("built_by_me = true\n", ""))
    result = execute(cfg, model=Model(), allow_paid=True)
    assert _closing_of(result) == URL, result.failures


def test_a_plain_run_has_no_closing(automation_work):  # noqa: F811
    cfg, _ = automation_work
    result = execute(cfg, model=Model(), allow_paid=True)
    assert result.drafts_created == 1 and _closing_of(result) is None


# --- FRAMING_HAS_CLAIM: subject-aware verbs ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("A scaffold that says it is a scaffold is a pleasant change.", None),
        ("A scaffold that says it is a scaffold is easy to read accurately.", None),
        ("Measuring a picture and understanding it are different things.", None),
        ("Some things are worth a second read.", None),
        ("Nice that the tests keep them apart.", "tests keep"),
        ("The code still returns the wrong badge.", "code returns"),
        ("The repository quietly uses a regex.", "repository uses"),
        ("Somehow the tests pass anyway.", "tests pass"),
    ],
)
def test_a_factual_verb_counts_only_after_a_code_subject(text, claim):
    found = framing_claims(text)
    if claim is None:
        assert found == []
    else:
        assert found == [f"states what it does: {claim}"]


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("It refuses to cut a sentence.", "It refuses"),
        ("They never retried.", "They retried"),
        ("This uses a regex, sadly.", "This uses"),
        ("Fine, and that ships anyway.", "that ships"),
        # A copula or auxiliary after a pronoun, or a relative "that", is the post's own wording.
        ("A scaffold that says it is a scaffold is a pleasant change.", None),
        ("A scaffold that says it is a scaffold is easy to describe accurately.", None),
        ("Optimism, but make it a button.", None),
        ("They were right, mostly.", None),
    ],
)
def test_a_pronoun_subject_before_an_action_verb_is_a_claim(text, claim):
    expected = [] if claim is None else [f"states what it does: {claim}"]
    assert framing_claims(text) == expected


def test_the_pronoun_fixture_blocks_and_its_controls_pass():
    brief, response, data = _closed("p5-pronoun-claim")
    amap = _amap(brief, response, data)
    s05 = next(s for s in amap.segments if s.id == "S05")
    assert s05.text == "It refuses to cut a sentence."
    assert FindingCode.FRAMING_HAS_CLAIM in {f.code for f in s05.findings}
    assert not judge(amap, response).approvable


def test_numbers_quotes_and_names_still_block_whatever_the_subject():
    assert framing_claims("A solid 40 points.") == ["number 40"]
    assert framing_claims("Returning 'model' anyway.") == ["quoted 'model'"]
    assert framing_claims("Thanks, parse_reply().") == ["names parse_reply()"]


# --- trim-first repair of a dry line ------------------------------------------------------------


def _rewrite(text):
    return {
        "stop_reason": "end_turn",
        "texts": [json.dumps({"rewrites": [{"segment_id": "S05", "text": text}]})],
    }


def _repair(reply):
    brief, response, _ = _closed("p5-sarcastic-claim")
    snapshot = load_verified_snapshot(PUBLIC / "snapshots" / "refund-desk")
    drafter = FixtureDrafter((reply, reply))
    outcome = repair.repair(brief, response, (), None, snapshot, drafter, "claude-sonnet-5-5")
    return outcome, drafter


def test_a_dry_line_with_a_claim_is_trimmed_first():
    outcome, drafter = _repair(_rewrite("Bold move."))
    assert outcome.status == repair.CLEAN, outcome.reasons
    assert len(drafter.requests) == 1
    assert repair.TRIM_FRAMING in drafter.requests[0].user_text
    s05 = next(s for s in outcome.response.segments if s.id == "S05")
    assert s05.text == "Bold move."
    assert outcome.response.segments[-1].id == CLOSING_ID  # the link stays last


@pytest.mark.parametrize("trim", ["Bold move, returning 'model' anyway.", ""])
def test_a_dry_line_that_trimming_cannot_clear_is_removed_after_one_round(trim):
    outcome, drafter = _repair(_rewrite(trim))
    assert len(drafter.requests) == 1  # one paid round for the line, never a second
    assert all(s.id != "S05" for s in outcome.response.segments)
    if trim:
        assert any("S05: removed the dry line" in line for line in outcome.free)
    assert outcome.status == repair.CLEAN, outcome.reasons


def test_the_trim_instruction_is_only_for_framing_claims():
    brief, response, _ = _closed("p5-sarcastic-claim")
    request = repair.build_request("claude-sonnet-5-5", brief, response, {"S02": ["X detail"]})
    assert repair.TRIM_FRAMING not in request.user_text


# --- change-first ranking ------------------------------------------------------------------------


def _ranked(key, angle, support="", mode="focus", repo="owner/one"):
    story = SimpleNamespace(
        story_id=key * 64,
        story_key=key,
        repo_id=repo,
        latest=SimpleNamespace(angle=angle, support=support),
    )
    return SimpleNamespace(story=story, mode=mode)


def test_change_stories_come_before_descriptions_within_a_mode():
    ranked = [
        _ranked("a", "The CRM splits each stage into its own workflow."),
        _ranked("b", "A bug: the UI marks the NFT minted before the transaction is signed."),
        _ranked("c", "The occasional repo fixed its retry.", mode="occasional"),
        _ranked("d", "Writing first and validating after clobbered good output; now it is fixed."),
    ]
    assert [i.story.story_key for i in prefer_change(ranked)] == ["b", "d", "a", "c"]


def test_a_description_is_still_drafted_when_nothing_else_waits():
    only = [_ranked("a", "The README says the backend is a scaffold.")]
    assert prefer_change(only) == only and not is_change_story(only[0].story)


# --- the link budget ----------------------------------------------------------------------------


@pytest.mark.parametrize("built_by_me", [False, True])
def test_p5_is_told_the_body_budget_and_a_post_at_it_plus_the_link_stays_within_800(built_by_me):
    brief, data = _public("p5-plain")
    url = brief.snapshot.source_url
    closing = closing_text(url, built_by_me)
    limit = p5_body_limit(closing)
    assert limit == 800 - len(closing) - 2
    prompt = story_system_prompt("dry-nerdy", STYLE, "plain", closing)
    assert f"300 to {limit} characters for what you write" in prompt
    assert closing not in prompt and url not in prompt  # its length only, never its text
    assert prompt_version(prompt) == "p5"
    # The model's own text exactly at the budget, every segment its own paragraph.
    raw = json.loads(data)
    body = raw["segments"]
    for segment in body:
        segment["paragraph_break_before"] = segment is not body[0]
    current = len("\n\n".join(s["text"] for s in body))
    body[-1]["text"] = body[-1]["text"][:-1] + " " + "x" * (limit - current - 1) + "."
    at_limit = json.dumps(raw).encode()
    assert len(render_post(parse_response(at_limit).segments)) == limit
    closed = parse_response(add_closing(at_limit, closing, url, MAX_SEGMENTS))
    assert closed.segments[-1].text == closing
    assert len(render_post(closed.segments)) == 800


def test_without_a_closing_p5_and_p4_are_byte_for_byte_unchanged():
    assert story_system_prompt("dry-nerdy", STYLE, "plain", None) == story_system_prompt(
        "dry-nerdy", STYLE, "plain"
    )
    assert "300 to 800 characters in total" in story_system_prompt("dry-nerdy", STYLE, "plain")
    assert story_system_prompt("plain", STYLE, "plain", closing_text(URL, True)) is (
        STORY_SYSTEM_PROMPT
    )
    assert p5_body_limit(None) == 800


def test_repair_holds_a_scope_line_to_the_same_body_budget():
    brief, response, _ = _closed("p5-plain")
    closing = response.segments[-1]
    ceiling, chars = repair._budget(response.generator.label, response.segments)
    assert ceiling == p5_body_limit(closing.text)
    assert chars == len(render_post(response.segments[:-1]))
    assert chars + 2 + len(closing.text) == len(render_post(response.segments))
