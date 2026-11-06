"""`pros eval`: offline replay of stored model replies (sockets are blocked by conftest)."""

import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from stories_fake import ACTIVE, ACTIVE_FILES, ACTIVE_REVISION, active_finding, make_snapshot, ref

from public_research_os.automation import evaluation
from public_research_os.automation.cli import main
from public_research_os.automation.state import State
from public_research_os.drafting.models import ModelReply, Usage
from public_research_os.drafting.normalise import normalise_finding
from public_research_os.drafting.story_prompt import build_context
from public_research_os.editorial.grounding import claim_numbers, labels
from public_research_os.editorial.sources import load_verified_snapshot
from public_research_os.preferences.repositories import Mode
from public_research_os.stories.findings import StoryFinding


def test_labels_are_names_and_need_only_to_exist_somewhere():
    assert claim_numbers("Phase 4 ran 4 trials; Attempt 3 made 0 calls") == ("4", "0")
    assert claim_numbers("Phase 4 ran") == ()
    assert labels("Attempt 3, phase-4 and PHASE_4") == (("attempt", "3"), ("phase", "4"))
    files = {"CANARY.md": ["# Attempt 3", "One request; 0 tool executions."]}
    finding = {
        "story_key": "attempt-three",
        "angle": "Attempt 3 recorded 0 tool executions on its one request.",
        "support": "The canary record lists the attempt and its counters.",
        "source_refs": [ref("CANARY.md", 2, 2)],
    }
    assert normalise_finding(finding, files, {}).changes == ()
    unknown = {**finding, "angle": "Attempt 7 recorded 0 tool executions on its one request."}
    assert normalise_finding(unknown, files, {}).core_claim_dropped


def test_a_finding_whose_main_claim_was_dropped_is_refused():
    files = {"NOTES.md": ["The cap is short.", "Flaky tasks dominated wall time."]}
    finding = {
        "story_key": "cap",
        "angle": "The cap saved 40 hours. Flaky tasks dominated wall time before it.",
        "support": "The notes say flaky tasks dominated wall time without the cap.",
        "source_refs": [ref("NOTES.md", 1, 2)],
    }
    normalised = normalise_finding(finding, files, {})
    assert normalised.core_claim_dropped and normalised.candidate["angle"] == finding["angle"]
    later = {**finding, "angle": "Flaky tasks dominated wall time. The cap saved 40 hours."}
    kept = normalise_finding(later, files, {})
    assert not kept.core_claim_dropped
    assert kept.candidate["angle"] == "Flaky tasks dominated wall time."


def test_the_audit_flags_a_number_no_quote_holds(tmp_path):
    snapshot = load_verified_snapshot(
        make_snapshot(tmp_path, "s", ACTIVE, ACTIVE_REVISION, ACTIVE_FILES)
    )
    held = StoryFinding.model_validate(active_finding())  # unquoted: its lines count
    assert evaluation.numbers_outside_quote(held, snapshot) == ()
    quoted = StoryFinding.model_validate(
        active_finding(source_refs=[ref("NOTES.md", 2, 3, "Retries are capped")])
    )
    assert evaluation.numbers_outside_quote(quoted, snapshot) == ("2",)


def _workspace(tmp_path: Path, findings: list) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "automation").mkdir(parents=True)
    (workspace / "snapshots").mkdir()
    directory = make_snapshot(
        workspace / "snapshots", "snap", ACTIVE, ACTIVE_REVISION, ACTIVE_FILES
    )
    context = build_context(directory, load_verified_snapshot(directory), ACTIVE, Mode.FOCUS, ())
    reply = ModelReply(
        model="claude-opus-5-5",
        request_id="req_1",
        stop_reason="end_turn",
        texts=(json.dumps({"findings": findings}),),
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    state = State(workspace / "automation" / "automation.sqlite")
    for revision in (f"{ACTIVE_REVISION}@archived-20261101T000000Z", ACTIVE_REVISION):
        state.attempt(ACTIVE, revision, "0" * 64, {"user_text": context.user_text}, 1, "0.01")
        state.responded(ACTIVE, revision, asdict(reply))
    state.close()
    return workspace


def test_a_workspace_is_replayed_read_only_including_archived_attempts(tmp_path, capsys):
    over_long = active_finding(
        story_key="wall-time",
        support="Without the cap, flaky tasks dominated wall time. It saved 40 hours.",
        source_refs=[ref("NOTES.md", 2, 3), ref("GONE.md", 1, 1)],
    )
    workspace = _workspace(tmp_path, [active_finding(), over_long, {"story_key": "x"}])
    database = workspace / "automation" / "automation.sqlite"
    before = database.read_bytes()
    assert main(["eval", "--workspace", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert database.read_bytes() == before
    assert (
        "STORY workspace:agent-evals-2222222-archived-20261101T000000Z: proposed=3 accepted=2"
        in out
    )
    assert "STORY workspace:agent-evals-2222222: proposed=3 accepted=2" in out
    assert "REF_DROPPED GONE.md:1-1 x PATH_NOT_IN_SNAPSHOT" in out
    assert "'It saved 40 hours.' (not in a quote: 40)" in out
    assert "REFUSED SCHEMA " in out and "numbers_outside_quote=0" in out


def test_a_missing_snapshot_or_case_directory_is_reported_not_raised(tmp_path, capsys):
    workspace = _workspace(tmp_path, [active_finding()])
    shutil.rmtree(workspace / "snapshots")
    assert main(["eval", "--workspace", str(workspace)]) == 2
    assert "EVAL_CASES_INVALID" in capsys.readouterr().out
    assert main(["eval", "--workspace", str(tmp_path / "nowhere")]) == 2
    assert "EVAL_CASES_INVALID" in capsys.readouterr().out


def test_eval_needs_a_source_and_check_needs_cases(capsys):
    for argv in (["eval"], ["eval", "--workspace", "x", "--check"]):
        with pytest.raises(SystemExit) as exited:
            main(argv)
        assert exited.value.code == 2
    capsys.readouterr()
