"""08A acceptance: actual service chain, synthetic public GitHub + controlled model adapters.

No request/findings/contribution file is prepared by the operator. The existing ingestion,
generation/discovery, draft, editorial and review services run; sockets remain forbidden.
"""

import json
import re
import shutil
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from drafting_fake import model_text, reply
from editorial_fake import seg
from github_fake import FakeGithub, json_response, tree_id
from stories_fake import HISTORIC_FILES, historic_finding, ref

from shipyap.automation import cli, engine, factcheck, repair, selection
from shipyap.automation import review as review_page
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.selection import select_sources
from shipyap.automation.slots import describe_local, next_free_slot
from shipyap.automation.state import State, run_lock
from shipyap.drafting import service, story_draft, story_prompt, story_service
from shipyap.drafting.errors import DraftingError, DraftingErrorCode, Phase
from shipyap.drafting.policy import counted_estimate
from shipyap.drafting.prompt import STORY_SYSTEM_PROMPT, absolute_phrases
from shipyap.drafting.story_draft import ANGLE_LABEL, SUPPORT_LABEL
from shipyap.editorial.grounding import number_tokens
from shipyap.editorial.models import UNVERIFIED_PREFIX, DraftStatus
from shipyap.ingestion.transport import _API_PATH
from shipyap.preferences.repositories import RepositoryPreferences
from shipyap.review.pages import StoryView, queue_page, story_page
from shipyap.review.service import ReviewDesk, ReviewState
from shipyap.stories.backlog import StoryBacklog, StoryState
from shipyap.stories.errors import StoryError, StoryErrorCode

NOW = datetime(2026, 10, 29, tzinfo=UTC)
REPO = "synthetic-owner/synthetic-repo"
FIRST = "1" * 40
SECOND = "2" * 40
THIRD = "3" * 40
SEGMENTS = (
    seg(
        "S01",
        "A screenshot proves capture, not that the right scene was shown.",
        "SOURCE",
        src=[ref("README.md", 3, 3)],
    ),
    seg(
        "S02",
        "The README reports the scene confirmed for 3/10 synthetic artifacts.",
        "SOURCE",
        src=[ref("README.md", 4, 4)],
    ),
)


class Github:
    """Scanner routes plus the existing Git Data fixture for real ingestion."""

    def __init__(self, revision=FIRST, files=None):
        self.current = revision
        self.files = files or HISTORIC_FILES
        self.git = FakeGithub(self.files, revision=revision)
        self.calls = []
        self.truncated = False
        self.compare_files = [
            dict(
                filename="README.md",
                status="modified",
                changes=4,
                patch="+Meaningful research changes to the source report.\n"
                "+A second explanation describes the evidence limits.",
            )
        ]

    def get(self, path, accept, max_bytes, deadline):
        assert _API_PATH.fullmatch(path), path  # production transport accepts every actual route
        self.calls.append(path)
        if path.endswith("/commits/HEAD"):
            return json_response(dict(sha=self.current, commit=dict(tree=dict(sha=tree_id("")))))
        if path.endswith("?recursive=1"):
            return json_response(
                dict(
                    sha=tree_id(""),
                    truncated=self.truncated,
                    tree=[
                        dict(
                            path=p,
                            type="blob",
                            mode="100644",
                            sha=self.git._blob_sha(p),
                            size=len(d),
                        )
                        for p, d in self.files.items()
                    ],
                )
            )
        if "/compare/" in path:
            base = path.split("/compare/")[1][:40]
            return json_response(
                dict(
                    status="ahead",
                    base_commit=dict(sha=base),
                    merge_base_commit=dict(sha=base),
                    files=self.compare_files,
                )
            )
        # The old transport fixture uses this spelling. Never derive a real URL from metadata.
        return self.git.get(path, accept, max_bytes, deadline)


class Model:
    def __init__(self, findings=None, failure=None):
        self.findings = [historic_finding()] if findings is None else findings
        self.failure = failure
        self.counts = []
        self.generations = []
        self.fact_checks = []

    def count_input_tokens(self, request):
        if "verdicts" in request.output_schema.get("properties", {}):
            return 5000  # fact checks are recorded in self.fact_checks, not in counts
        self.counts.append(request)
        return 5000

    def generate(self, request):
        if "verdicts" in request.output_schema.get("properties", {}):
            self.fact_checks.append(request)
            ids = re.findall(r"^(S\d+): ", request.user_text, re.MULTILINE)
            verdicts = getattr(self, "verdicts", {})
            return reply(
                json.dumps(
                    {
                        "verdicts": [
                            {
                                "segment_id": sid,
                                "verdict": verdicts.get(sid, ("SUPPORTED", ""))[0],
                                "reason": verdicts.get(sid, ("", "Supported."))[1],
                                "refs": [{"path": "README.md", "line_start": 3, "line_end": 3}],
                            }
                            for sid in ids
                        ]
                    }
                )
            )
        self.generations.append(request)
        if self.failure is not None:
            if isinstance(self.failure, BaseException):
                raise self.failure
            return reply(self.failure)
        if "findings" in request.output_schema.get("properties", {}):
            return reply(json.dumps(dict(findings=self.findings)))
        return reply(getattr(self, "draft_text", None) or model_text(SEGMENTS))

    @property
    def discovery_calls(self):
        return [r for r in self.generations if "findings" in r.output_schema.get("properties", {})]


@pytest.fixture
def work(tmp_path, monkeypatch):
    # Any accidental production key access or publisher action is a hard failure.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("production model/key accessed")

    monkeypatch.setattr(engine, "_production_model", forbidden)
    config = tmp_path / "pros.toml"
    config.write_text(
        f"""workspace_root = '{tmp_path}'
workspace_id = 'demo_workspace'
preferences = 'preferences.sqlite'
backlog = 'backlog.sqlite'
drafts = 'drafts'
snapshots = 'snapshots'
state = 'automation'
max_cost_usd = '1.00'
""",
        encoding="utf-8",
    )
    cfg = load_config(config)
    with RepositoryPreferences.open(cfg.preferences, create=True) as store:
        store.set_mode(REPO, "focus", NOW)
    return cfg, config


def execute(cfg, github=None, model=None, **kwargs):
    model = model or Model()
    return engine.run(cfg, github or Github(), model_factory=lambda: model, now=NOW, **kwargs)


def test_historical_then_backlog_then_idempotent_and_reviewable(work):
    cfg, _ = work
    second = historic_finding(
        story_key="one-synthetic-configuration",
        angle="The study tested one synthetic configuration only.",
        support="The README explicitly limits the experiment to one configuration.",
        source_refs=[ref("README.md", 5, 5)],
    )
    model = Model([historic_finding(), second])
    github = Github()
    first = execute(cfg, github, model, allow_paid=True)
    assert not first.failures
    assert first.statuses == ["NEW_STORY_DRAFTED"]
    assert (first.evidence_examined, first.new_candidates, first.drafts_created) == (1, 2, 1)
    assert len(model.generations) == 2 and len(model.discovery_calls) == 1
    with StoryBacklog.open(cfg.backlog, create=False) as store:
        assert sorted(s.state for s in store.stories()) == [
            StoryState.AVAILABLE,
            StoryState.OFFERED,
        ]
    second_run = execute(cfg, github, model, allow_paid=True)
    assert second_run.statuses == ["BACKLOG_DRAFTED"]
    assert second_run.existing_available == 1 and second_run.evidence_examined == 0
    assert len(model.generations) == 3 and len(model.discovery_calls) == 1
    counts = len(model.counts)
    third_run = execute(cfg, github, model, allow_paid=True)
    assert third_run.statuses == ["NO_CHANGES"]
    assert not third_run.drafts_created and not third_run.estimated_usd
    assert len(model.counts) == counts and len(model.generations) == 3
    desk = ReviewDesk(cfg.drafts, cfg.backlog, cfg.state / "review.sqlite", tuple(first.snapshots))
    items, refused = desk.queue()
    assert not refused and len(items) == 2
    assert all(i.state == ReviewState.UNAPPROVED for i in items)
    assert all(i.current.record.draft_status == DraftStatus.READY_FOR_REVIEW for i in items)
    html = queue_page(items, refused)
    assert all(item.story.story_id in html for item in items)
    assert all((p / "review.md").is_file() for p in first.draft_paths + second_run.draft_paths)


def test_new_commit_automatically_ingested_and_trivial_changes_skipped(work):
    cfg, _ = work
    model = Model([])
    assert execute(cfg, model=model, allow_paid=True).evidence_examined == 1
    github = Github(SECOND)
    result = execute(cfg, github, model, allow_paid=True)
    assert not result.failures and result.evidence_examined == 1
    assert any(f"{FIRST}...{SECOND}" in p for p in github.calls)
    assert len(model.discovery_calls) == 2
    github = Github(THIRD)
    github.compare_files = [
        dict(
            filename="README.md",
            status="modified",
            changes=2,
            patch="-some documentation\n+some   documentation",
        )
    ]
    result = execute(cfg, github, model, allow_paid=True)
    assert not result.failures and len(model.discovery_calls) == 2
    assert not execute(cfg, github, model, allow_paid=True).evidence_examined


def test_no_findings_advances_cursor_and_never_repeats(work):
    cfg, _ = work
    model = Model([])
    first = execute(cfg, model=model, allow_paid=True)
    assert first.statuses == ["EXAMINED_NO_STORY"] and not first.drafts_created
    state = State(cfg.state / "automation.sqlite")
    assert state.cursor(REPO) == FIRST
    assert state.db.execute("SELECT status FROM generation_attempts").fetchone()[0] == "NO_FINDINGS"
    state.close()
    assert execute(cfg, model=model).statuses == ["NO_CHANGES"]
    assert len(model.counts) == len(model.generations) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "not JSON",
        DraftingError(DraftingErrorCode.PROVIDER_TIMEOUT, phase=Phase.GENERATE),
        KeyboardInterrupt(),
    ],
)
def test_malformed_uncertain_or_crashed_generation_is_not_repeated(work, failure):
    cfg, _ = work
    model = Model(failure=failure)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(cfg, model=model, allow_paid=True)
    else:
        assert execute(cfg, model=model, allow_paid=True).failures
    state = State(cfg.state / "automation.sqlite")
    assert state.cursor(REPO) is None
    assert state.pending(REPO)["status"] == ("FAILED" if isinstance(failure, str) else "UNCERTAIN")
    state.close()
    # Even a newer HEAD or different model must not bypass an unresolved attempt.
    result = execute(replace(cfg, model="claude-opus-5-5"), Github(SECOND), model, allow_paid=True)
    assert result.failures and result.evidence_examined == 0
    assert len(model.generations) == len(model.counts) == 1


def test_failed_attempt_with_a_stored_reply_is_recovered_offline(work, monkeypatch):
    """The first real trial: a paid nine-finding reply was rejected whole (7 cited 11 lines),
    the attempt became FAILED and the repository was stuck. With the reply stored, a later run
    replays it offline, keeps the valid findings and makes no request and no new attempt."""
    cfg, _ = work
    too_long = historic_finding(
        story_key="eleven-line-citation", source_refs=[ref("README.md", 1, 11)]
    )
    model = Model(findings=[too_long, historic_finding(), too_long])
    original = story_service._reply_document

    def old_strict_parser(*_args, **_kwargs):  # the rule in force during the trial
        raise StoryError(StoryErrorCode.FINDINGS_INVALID, ("findings.0: too long",))

    monkeypatch.setattr(story_service, "_reply_document", old_strict_parser)
    assert execute(cfg, model=model, allow_paid=True).failures
    state = State(cfg.state / "automation.sqlite")
    row = state.pending(REPO)
    assert row["status"] == "FAILED" and row["reply_json"]
    state.close()
    monkeypatch.setattr(story_service, "_reply_document", original)

    # No paid opt-in, no drafting: only the stored reply can be used.
    result = execute(replace(cfg, max_new_drafts=0), model=model)
    assert not result.estimated_usd and result.evidence_examined == 1
    assert any("FINDINGS_REFUSED (2)" in failure for failure in result.failures)
    assert len(model.generations) == len(model.counts) == 1  # the trial's one paid call only
    state = State(cfg.state / "automation.sqlite")
    assert state.pending(REPO) is None
    assert state.db.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0] == 1
    state.close()
    with StoryBacklog.open(cfg.backlog, create=False) as store:
        (story,) = store.stories()
    assert story.story_key == "capture-is-not-scene" and story.state == StoryState.AVAILABLE


def test_failed_attempt_without_a_stored_reply_still_needs_the_owner(work):
    cfg, _ = work
    cfg.state.mkdir(parents=True)
    state = State(cfg.state / "automation.sqlite")
    state.attempt(REPO, FIRST, "0" * 64, {}, 5000, "0.10")
    state.failed(REPO, FIRST)
    state.close()
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert any("GENERATION_FAILED_OWNER_INTERVENTION" in f for f in result.failures)
    assert not model.generations


@pytest.mark.parametrize("after_registration", [False, True])
def test_persisted_success_recovers_without_model_calls(work, monkeypatch, after_registration):
    cfg, _ = work
    original = engine.run_story_generation

    def crash(*args, **kwargs):
        if after_registration:
            original(*args, **kwargs)
        else:
            plan, drafter = args[:2]
            request = plan.targets[0].request
            drafter.count_input_tokens(request)
            drafter.generate(request)
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "run_story_generation", crash)
    model = Model()
    with pytest.raises(KeyboardInterrupt):
        execute(cfg, model=model, allow_paid=True)
    monkeypatch.setattr(engine, "run_story_generation", original)
    # Recovery is allowed without paid opt-in. Drafting is deferred by config for this check.
    result = execute(replace(cfg, max_new_drafts=0), model=model)
    assert not result.failures and result.evidence_examined == 1
    assert len(model.generations) == len(model.counts) == 1
    assert not result.estimated_usd
    assert execute(cfg, model=model, allow_paid=True).statuses == ["BACKLOG_DRAFTED"]
    assert len(model.discovery_calls) == 1


def test_budget_is_cumulative_and_refusal_leaves_draft_retryable(work):
    cfg, _ = work
    cfg = replace(cfg, max_cost_usd="0.20")  # one Sonnet request fits, two do not
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.new_candidates == 1 and not result.drafts_created
    assert "BUDGET_EXCEEDED" in str(result.failures)
    assert result.estimated_usd == counted_estimate(cfg.model, 5000).max_cost_usd
    assert len(model.generations) == 1
    assert not list(cfg.drafts.glob("*-attempt"))
    second = execute(cfg, model=model, allow_paid=True)
    assert not second.drafts_created and second.statuses[0] == "NEEDS_ATTENTION"
    assert "DRAFT_AND_FACT_CHECK_BUDGET_EXCEEDED" in str(second.failures)
    assert "no draft call made" in str(second.failures) and not model.fact_checks
    capped = replace(cfg, max_cost_usd="0.01")
    result = execute(capped, Github(SECOND), model, allow_paid=True)
    assert not result.estimated_usd and "BUDGET_EXCEEDED" in str(result.failures)
    assert len(model.generations) == 1


def test_no_paid_opt_in_never_constructs_provider_and_lock_prevents_overlap(work):
    cfg, path = work
    model = Model()
    result = execute(cfg, model=model)
    assert result.failures and "PAID_OPT_IN_REQUIRED" in str(result.failures)
    assert not model.counts and not model.generations
    with run_lock(cfg.state / "run-lock.sqlite"):
        with pytest.raises(AutomationError, match="RUN_LOCKED"):
            execute(cfg, model=model, allow_paid=True)
    assert not model.counts
    assert execute(cfg, model=model, allow_paid=True).drafts_created == 1
    assert load_config(path).model == "claude-sonnet-5-5"


def test_selection_filters_generated_binary_lock_and_unsafe_paths_and_fails_closed(work):
    cfg, _ = work
    files = dict(
        HISTORIC_FILES,
        **{
            "research/report.md": b"Meaningful report with evidence and limitations.\n" * 3,
            "experiments/eval.py": b"def experiment():\n    return 'a meaningful research experiment'\n",
            "docs/explanation.md": b"Technical explanation of the research method and boundaries.\n",
            "uv.lock": b"lock" * 100,
            "dist/research.py": b"generated" * 100,
            "paper.pdf": b"binary" * 100,
            "../research.md": b"unsafe" * 100,
        },
    )
    github = Github(files=files)
    selected = select_sources(REPO, None, github)
    # The lead document first, then the rest in priority order (no code shares its words);
    # up to six files now, and never the excluded ones.
    assert selected.paths[:3] == (
        "research/report.md",
        "experiments/eval.py",
        "docs/explanation.md",
    )
    assert len(selected.paths) <= 6
    assert not {"uv.lock", "dist/research.py", "paper.pdf", "../research.md"} & set(selected.paths)
    github.truncated = True
    with pytest.raises(AutomationError, match="TREE_INCOMPLETE"):
        select_sources(REPO, None, github)
    github.truncated = False
    github.compare_files *= 300
    with pytest.raises(AutomationError, match="COMPARE_FILE_LIMIT"):
        select_sources(REPO, SECOND, github)


AUTHOR_TABLE = f"""
[author]
name = "Synthetic Owner"
[[author.repos]]
repo = "{REPO}"
relationship = "primary_author"
statements = ["I built this repository to test when a screenshot counts as evidence."]
"""
FIRST_PERSON = (
    seg(
        "S01",
        "I built this repository to test when a screenshot counts as evidence.",
        "AUTHOR",
        auth=["attempted.1"],
    ),
    *({**segment, "id": f"S0{n}"} for n, segment in enumerate(SEGMENTS, start=2)),
)


def _with_author(cfg, path):
    path.write_text(path.read_text(encoding="utf-8") + AUTHOR_TABLE, encoding="utf-8")
    return load_config(path)


def test_author_profile_lets_the_draft_speak_in_first_person_with_p2(work):
    cfg, path = work
    cfg = _with_author(cfg, path)
    assert cfg.authors[REPO].statements[0].startswith("I built")
    model = Model()
    model.draft_text = model_text(FIRST_PERSON)
    result = execute(cfg, model=model, allow_paid=True)
    assert not result.failures and result.drafts_created == 1, result.failures
    draft_request = [r for r in model.generations if r not in model.discovery_calls][0]
    assert draft_request.system_prompt == STORY_SYSTEM_PROMPT
    assert "Synthetic Owner" in draft_request.user_text
    assert "primary_author" in draft_request.user_text
    bundle = result.draft_paths[0]
    text = (bundle / "draft.txt").read_text(encoding="utf-8")
    assert text.startswith("I built this repository")
    record = json.loads((bundle / "assertion-map.json").read_text(encoding="utf-8"))
    assert record["draft_status"] == "READY_FOR_REVIEW"
    assert not any("no [author] entry" in note for note in result.notes)


def test_without_a_profile_first_person_is_still_blocked_and_the_owner_is_told(work):
    cfg, _ = work
    model = Model()
    model.draft_text = model_text(FIRST_PERSON)
    result = execute(cfg, model=model, allow_paid=True)
    assert "NEEDS_REVISION" in result.statuses
    assert any("no [author] entry" in note for note in result.notes)


def test_style_notes_flag_length_and_machine_phrases_without_editing(work):
    cfg, path = work
    cfg = _with_author(cfg, path)
    tail = [
        seg(f"S{n:02d}", "Excited to share this." + " Framing words only." * 12, "FRAMING")
        for n in range(4, 9)
    ]
    model = Model()
    model.draft_text = model_text((*FIRST_PERSON, *tail))
    result = execute(cfg, model=model, allow_paid=True)
    notes = " | ".join(result.notes)
    assert "over the 1,300 target" in notes and "excited to share" in notes
    text = (result.draft_paths[0] / "draft.txt").read_text(encoding="utf-8")
    assert "Excited to share" in text  # flagged, never rewritten


def test_a_bad_reference_in_a_review_question_no_longer_sinks_the_draft(work):
    """The first paid draft: the post was fine, but one verification question cited 18 lines.
    The reference is dropped, the question kept, and the draft is built."""
    cfg, path = work
    cfg = _with_author(cfg, path)
    model = Model()
    model.draft_text = model_text(
        FIRST_PERSON,
        verification_needed=[
            {
                "text": "Is the result current?",
                "reason": "Check it.",
                "source_refs": [ref("README.md", 1, 18)],
            }
        ],
    )
    result = execute(cfg, model=model, allow_paid=True)
    assert not result.failures and result.drafts_created == 1, result.failures
    amap = json.loads((result.draft_paths[0] / "assertion-map.json").read_text(encoding="utf-8"))
    (question,) = amap["verification_needed"]
    assert question["text"] == "Is the result current?" and question["source_refs"] == []


@pytest.mark.parametrize(
    "table",
    [
        'name = "X"\n[[author.repos]]\nrepo = "a/b"\nrelationship = "reader"\nstatements = ["I read it."]',
        'name = "X"\n[[author.repos]]\nrepo = "a/b"\nrelationship = "primary_author"\nstatements = []',
        'name = ""',
        'name = "X"\nemail = "x@example.com"',
    ],
    ids=["reader-cannot-say-i", "no-statements", "empty-name", "unknown-key"],
)
def test_an_invalid_author_table_is_refused(work, table):
    _, path = work
    path.write_text(path.read_text(encoding="utf-8") + "\n[author]\n" + table + "\n")
    with pytest.raises(AutomationError):
        load_config(path)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "max_cost_usd = '0'",
        "model = 'unrecognized'",
        "max_new_drafts = 2",
        "drafts = '../escape'",
        "api_key = 'SECRET-SENTINEL'",
    ],
)
def test_config_invalid_values_are_redacted(work, change, capsys):
    _, path = work
    key = change.split(" = ")[0] if change else "max_cost_usd"
    lines = [line for line in path.read_text().splitlines() if not line.startswith(key + " =")]
    path.write_text("\n".join([*lines, *([change] if change else [])]), encoding="utf-8")
    assert cli.main(["run", "--config", str(path), "--allow-paid"]) == 2
    output = capsys.readouterr().out
    assert "CONFIG_INVALID" in output
    assert "SECRET-SENTINEL" not in output


def test_console_command_runs_full_controlled_pipeline_and_prints_review_location(
    work, monkeypatch, capsys
):
    cfg, path = work
    model = Model()
    monkeypatch.setattr(cli, "HttpsTransport", lambda limits: Github())
    monkeypatch.setattr(engine, "_production_model", lambda: model)
    assert cli.main(["run", "--config", str(path), "--allow-paid"]) == 0
    output = capsys.readouterr().out
    assert "NEW_STORY_DRAFTED" in output and "Drafts created: 1" in output
    assert f"Review: shipyap review --config {path}" in output and str(cfg.drafts) in output
    assert "review-serve" not in output
    assert "UNKNOWN" in output and "UNAPPROVED" in output
    assert len(model.generations) == 2
    assert cli.main(["run", "--config", str(path)]) == 0
    assert "NO_CHANGES" in capsys.readouterr().out
    assert len(model.generations) == 2


def test_a_fresh_workspace_runs_and_reviews_with_one_clear_message(tmp_path, monkeypatch, capsys):
    from shipyap.publication import buffer
    from shipyap.review.pages import STOP_MESSAGE
    from shipyap.review.server import ReviewServer

    def no_page(_server):
        raise KeyboardInterrupt  # the owner's Ctrl+C, at once

    class NoGithub:  # any GitHub request would be an attribute error and fail the test
        def __init__(self, _limits) -> None:
            pass

    monkeypatch.setattr(ReviewServer, "serve_forever", no_page)
    monkeypatch.setattr(buffer, "publishing_enabled", lambda: False)
    monkeypatch.setattr(cli, "HttpsTransport", NoGithub)
    root = tmp_path / "ws"
    root.mkdir()
    path = root / "pros.toml"  # a valid config is the only file in the workspace
    path.write_text(
        f"""workspace_root = '{root}'
workspace_id = 'demo_workspace'
preferences = 'preferences.sqlite'
backlog = 'backlog.sqlite'
drafts = 'drafts'
snapshots = 'snapshots'
state = 'automation'
max_cost_usd = '1.00'
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    add = f"Add one: shipyap repo set --repo OWNER/REPO --mode focus --db {cfg.preferences}"
    # Review first: it creates what it needs and opens an empty queue.
    assert cli.main(["review", "--config", str(path), "--port", "0"]) == 0
    out = capsys.readouterr().out
    assert "Open this in your browser (Ctrl+click):\nhttp://127.0.0.1:" in out
    assert "NEEDS_ATTENTION" not in out
    assert STOP_MESSAGE not in out
    assert cfg.drafts.is_dir() and cfg.snapshots.is_dir() and cfg.backlog.is_file()
    assert (cfg.state / "review-ledger.sqlite").is_file()
    # Run: no preferences store yet, so one message says what to do; nothing is read.
    assert cli.main(["run", "--config", str(path)]) == 2
    out = capsys.readouterr().out
    assert out == f"NO_REPOSITORIES: no repository is tracked yet. {add}\n"
    assert not cfg.preferences.exists()
    assert "NO_REPOSITORIES" in (root / "notify.txt").read_text(encoding="utf-8")
    # A store whose only repository is off: the same message, not NO_CHANGES.
    assert (
        cli.main(["repo", "set", "--repo", REPO, "--mode", "off", "--db", str(cfg.preferences)])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["run", "--config", str(path)]) == 2
    out = capsys.readouterr().out
    assert out == f"NO_REPOSITORIES: no repository is enabled (focus or occasional). {add}\n"
    # The STOPPED line at review start also says Buffer is not touched.
    (root / "STOP").write_text("", encoding="utf-8")
    assert cli.main(["review", "--config", str(path), "--port", "0"]) == 0
    assert STOP_MESSAGE in capsys.readouterr().out


def test_unusable_old_snapshot_does_not_block_intact_backlog(work):
    cfg, _ = work
    model = Model()
    first = execute(replace(cfg, max_new_drafts=0), model=model, allow_paid=True)
    assert first.new_candidates == 1
    old = cfg.snapshots / "deleted-old-snapshot"
    state = State(cfg.state / "automation.sqlite")
    state.remember("acme/off-repo", "a" * 40, old)
    state.close()
    # Remembered corruption must be reported without stopping the valid candidate.
    result = execute(cfg, model=model, allow_paid=True)
    assert result.drafts_created == 1 and "BACKLOG_DRAFTED" in result.statuses
    assert "SNAPSHOT_INCOMPLETE" in str(result.failures)
    assert len(model.discovery_calls) == 1


def test_fact_check_count_over_reservation_is_not_sent(work):
    cfg, _ = work
    cfg = replace(cfg, max_cost_usd="2.00")

    class OversizedCount(Model):
        def __init__(self):
            super().__init__()
            self.check_counts = 0

        def count_input_tokens(self, request):
            if "verdicts" in request.output_schema.get("properties", {}):
                self.check_counts += 1
                return 1_000_000 if self.check_counts > 1 else 5000
            return super().count_input_tokens(request)

    model = OversizedCount()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.drafts_created == 1
    assert "FACT_CHECK_RESERVATION_EXCEEDED" in str(result.failures)
    assert not model.fact_checks
    assert not list((cfg.state / "fact-checks").glob("*.json"))


def test_one_failed_repository_does_not_mark_it_complete_or_stop_other_repository(work):
    cfg, _ = work
    cfg = replace(cfg, max_cost_usd="2.00")  # reserve two discoveries and the draft/check pair
    other = "acme/other-research"
    with RepositoryPreferences.open(cfg.preferences, create=False) as store:
        store.set_mode(other, "occasional", NOW)
        store.set_mode("acme/off-research", "off", NOW)

    class MultipleGithub(Github):
        def get(self, path, *args):
            assert "off-research" not in path
            return super().get(path.replace(other, REPO), *args)

    class OneFailure(Model):
        def generate(self, request):
            if REPO in request.user_text:
                self.generations.append(request)
                return reply("malformed")
            return super().generate(request)

    model = OneFailure()
    result = execute(cfg, MultipleGithub(), model, allow_paid=True)
    assert result.repositories_inspected == 2 and result.evidence_examined == 1
    assert result.new_candidates == result.drafts_created == 1
    assert result.failures
    state = State(cfg.state / "automation.sqlite")
    assert state.cursor(REPO) is None and state.cursor(other) == FIRST
    state.close()


def test_draft_malformed_reply_keeps_existing_attempt_protection(work):
    cfg, _ = work

    class BadDraft(Model):
        def generate(self, request):
            if "findings" in request.output_schema.get("properties", {}):
                return super().generate(request)
            self.generations.append(request)
            return reply("malformed draft")

    model = BadDraft()
    first = execute(cfg, model=model, allow_paid=True)
    assert first.evidence_examined == 1 and not first.drafts_created
    assert "RESPONSE_INVALID_JSON" in str(first.failures)
    assert len(list(cfg.drafts.glob("*-attempt"))) == 1
    second = execute(cfg, model=model, allow_paid=True)
    assert "GENERATION_ATTEMPTED" in str(second.failures)
    assert len(model.generations) == len(model.counts) == 2


def _spending(cfg) -> list[str]:
    state = State(cfg.state / "automation.sqlite")
    kinds = [kind for (kind,) in state.db.execute("SELECT kind FROM spending ORDER BY id")]
    state.close()
    return kinds


def test_a_kept_reply_refused_whole_is_rebuilt_free_and_never_counts_again(work, monkeypatch):
    """2026-11-04 burst run: one broken reference refused the whole paid draft reply
    (RESPONSE_INVALID). The kept reply is rebuilt by a later run with no request, no new `draft`
    row in spending and no change to the weekly draft count; the run after that changes nothing."""
    cfg, _ = work
    cfg = replace(cfg, weekly_draft_cap=2)
    five = [ref("README.md", 3, 3)] * 5  # the schema allows 4 per segment
    model = Model()
    model.draft_text = model_text(
        (seg("S01", SEGMENTS[0]["text"], "SOURCE", src=five), SEGMENTS[1])
    )

    def before_the_fix(data, brief):  # the parser in force during the burst run
        return service.ResponseRepair(service.tidy_model_questions(data), ())

    monkeypatch.setattr(service, "checked_input", before_the_fix)
    monkeypatch.setattr(story_draft, "checked_input", before_the_fix)
    first = execute(cfg, model=model, allow_paid=True)
    assert "draft: RESPONSE_INVALID" in first.failures and not first.drafts_created
    assert _spending(cfg) == ["discovery", "draft"]
    generations = len(model.generations)
    monkeypatch.undo()
    monkeypatch.setattr(engine, "_production_model", lambda: pytest.fail("production model"))

    # At the cap, even this free rebuild waits: the cap is checked before anything is read.
    capped = execute(replace(cfg, weekly_draft_cap=1), model=model, allow_paid=True)
    assert capped.statuses == ["WEEKLY_CAP_REACHED"] and not capped.drafts_created

    second = execute(cfg, model=model, allow_paid=True)
    assert second.drafts_created == 1 and not second.failures
    assert "draft: repaired before checks: S01: README.md:3-3 dropped (OVER_REF_LIMIT)" in (
        second.notes
    )
    assert len(model.generations) == generations  # no new draft request
    assert _spending(cfg) == ["discovery", "draft", "fact_check"]  # the fact check is paid
    state = State(cfg.state / "automation.sqlite")
    assert engine._paid_drafts_this_week(state, NOW) == 1
    state.close()
    (draft,) = cfg.drafts.glob("*-draft")
    response = json.loads((draft / "response.json").read_bytes())
    assert len(response["segments"][0]["source_refs"]) == 4
    kept = next(cfg.drafts.glob("*-response")) / "response.json"
    assert len(json.loads(kept.read_bytes())["segments"][0]["source_refs"]) == 5  # as sent

    third = execute(cfg, model=model, allow_paid=True)
    assert third.statuses == ["NO_CHANGES"] and not third.failures
    assert _spending(cfg) == ["discovery", "draft", "fact_check"]


@pytest.mark.parametrize("status", ["NO_POST", "NEEDS_REVISION"])
def test_editorial_no_post_and_blocked_draft_are_reported_honestly(work, status):
    cfg, _ = work

    class EditorialOutcome(Model):
        def generate(self, request):
            properties = request.output_schema.get("properties", {})
            if "findings" in properties or "verdicts" in properties:
                return super().generate(request)
            self.generations.append(request)
            if status == "NO_POST":
                return reply(model_text((), outcome="NO_POST", no_post_reason="No useful post."))
            unsupported = seg(
                "S01",
                "The study confirms 999 synthetic captures.",
                "SOURCE",
                src=[ref("README.md", 4, 4)],
            )
            return reply(model_text((unsupported,)))

    model = EditorialOutcome()
    result = execute(cfg, model=model, allow_paid=True)
    assert not result.failures and status in result.statuses
    assert result.drafts_created == int(status != "NO_POST")
    assert result.draft_paths[0].is_dir()
    # A blocked draft gets two auto-repair rounds (this fake answers them unusably), then is
    # shelved: never offered, and named in the run's report.
    assert len(model.generations) == (2 if status == "NO_POST" else 4)
    assert bool(result.shelved) == (status == "NEEDS_REVISION")
    assert execute(cfg, model=model, allow_paid=True).statuses == ["NO_CHANGES"]


# --- the loop: slots, STOP, weekly cap, notifications ----------------------------------------


def test_next_free_slot_is_the_next_mon_wed_fri_in_india_not_already_taken():
    thursday_evening = datetime(2026, 10, 29, 11, 30, tzinfo=UTC)  # 17:00 IST
    slots = ("Mon 09:30", "Wed 09:30", "Fri 09:30")
    assert next_free_slot(thursday_evening, slots, "+05:30", set()) == "2026-10-30T04:00:00Z"
    taken = {"2026-10-30T04:00:00Z"}
    assert next_free_slot(thursday_evening, slots, "+05:30", taken) == "2026-11-02T04:00:00Z"
    assert describe_local("2026-11-02T04:00:00Z", "+05:30") == "Mon 2 Nov 2026, 09:30"
    # A slot closer than the minimum lead is skipped.
    friday_early = datetime(2026, 10, 30, 3, 58, tzinfo=UTC)
    assert next_free_slot(friday_early, slots, "+05:30", set()) == "2026-11-02T04:00:00Z"


@pytest.mark.parametrize(
    "line",
    [
        "buffer_channel_id = 'not-hex'",
        "posting_slots = ['Funday 09:30']",
        "posting_slots = ['Mon 25:00']",
        "utc_offset = '5:30'",
        "weekly_draft_cap = 99",
        "x_channel_id = 'not-hex'",
        "buffer_channel_id = '0123456789abcdef01234567'\nx_channel_id = '0123456789abcdef01234567'",
    ],
)
def test_invalid_publishing_settings_are_refused(work, line):
    _, path = work
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    with pytest.raises(AutomationError):
        load_config(path)


def test_valid_publishing_settings_are_read(work):
    _, path = work
    path.write_text(
        path.read_text(encoding="utf-8")
        + "buffer_channel_id = '0123456789abcdef01234566'\n"
        + "posting_slots = ['Tue 18:00', 'Sat 10:15']\n"
        + "weekly_draft_cap = 2\n"
        + "x_channel_id = '0123456789abcdef01234567'\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.buffer_channel_id == "0123456789abcdef01234566"
    assert cfg.x_channel_id == "0123456789abcdef01234567"
    assert cfg.posting_slots == ("Tue 18:00", "Sat 10:15") and cfg.weekly_draft_cap == 2


def test_the_stop_file_halts_every_run_before_anything_happens(work):
    cfg, _ = work
    (cfg.workspace_root / "STOP").write_text("", encoding="utf-8")
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.statuses == ["STOPPED"]
    assert not model.generations and not model.counts
    assert not (cfg.state / "automation.sqlite").exists()


def _paid_drafts(cfg, *attempted_at):
    cfg.state.mkdir(parents=True, exist_ok=True)
    state = State(cfg.state / "automation.sqlite")
    with state.db:
        state.db.executemany(
            "INSERT INTO spending(kind, request_hash, estimated_usd, attempted_at) "
            "VALUES ('draft', ?, '0.10', ?)",
            [("0" * 64, at.isoformat()) for at in attempted_at],
        )
    state.close()


@pytest.mark.parametrize("cap", [0, 4])
def test_the_weekly_draft_cap_stops_the_run_before_any_paid_discovery(work, cap):
    """Prior live run: discovery was paid for, then the cap refused the draft. At the cap,
    nothing is read from GitHub, no model request is counted or sent, and no state changes."""
    cfg, _ = work
    cfg = replace(cfg, weekly_draft_cap=cap)
    _paid_drafts(cfg, *[NOW] * cap)  # NOW is a Thursday; these are this ISO week's drafts
    model, github = Model(), Github()
    result = execute(cfg, github, model, allow_paid=True, reexamine=(REPO,))
    assert result.statuses == ["WEEKLY_CAP_REACHED"]
    assert not github.calls and not model.counts and not model.generations
    assert result.estimated_usd == 0 and not result.failures and not result.drafts_created
    assert result.notes == [
        f"draft: weekly cap reached ({cap} of {cap} paid drafts this week); nothing was read, "
        "sent or spent. Stories wait in the backlog until Monday 00:00 UTC, or raise "
        "weekly_draft_cap in pros.toml"
    ]
    with StoryBacklog.open(cfg.backlog, create=True) as store:
        assert not store.stories()
    # Raising the cap lets the same workspace run normally: nothing was left half-done.
    after = execute(replace(cfg, weekly_draft_cap=cap + 1), github, model, allow_paid=True)
    assert after.drafts_created == 1 and len(model.discovery_calls) == 1


def test_drafts_from_last_week_do_not_count_toward_the_cap(work):
    cfg, _ = work
    cfg = replace(cfg, weekly_draft_cap=1)
    _paid_drafts(cfg, datetime(2026, 10, 25, 23, 59, tzinfo=UTC))  # the Sunday before NOW
    result = execute(cfg, model=Model(), allow_paid=True)
    assert result.drafts_created == 1 and "WEEKLY_CAP_REACHED" not in result.statuses


def test_discovery_only_runs_are_not_stopped_by_the_cap(work):
    cfg, _ = work
    cfg = replace(cfg, weekly_draft_cap=0, max_new_drafts=0)
    model = Model()
    result = execute(cfg, model=model, allow_paid=True)
    assert result.new_candidates == 1 and len(model.discovery_calls) == 1
    assert result.drafts_created == 0 and len(model.generations) == 1


def test_a_run_writes_last_run_and_one_notification_per_event(work):
    cfg, _ = work
    result = execute(cfg, model=Model(), allow_paid=True)
    cli._tell(cfg, result, ["Posted A screenshot proves capture"])
    lines = (cfg.workspace_root / "notify.txt").read_text(encoding="utf-8").splitlines()
    assert re.fullmatch(r"[0-9a-f]{64}", result.draft_story)
    title, text, link = lines[0].split("|")
    assert title == "Shipyap: 1 draft ready to review" and "Review" in text
    assert link == f"shipyap://review/{result.draft_story}"
    assert lines[1] == "Shipyap|Posted A screenshot proves capture"
    last = (cfg.workspace_root / "last-run.txt").read_text(encoding="utf-8")
    assert "drafts created 1" in last


def test_the_ready_toast_counts_every_draft_waiting_and_links_the_new_one(work, monkeypatch):
    cfg, _ = work
    result = execute(cfg, model=Model(), allow_paid=True)
    others = ("a" * 64, "b" * 64)
    monkeypatch.setattr(cli.review_page, "waiting", lambda config: (*others, result.draft_story))
    cli._tell(cfg, result, [])
    line = (cfg.workspace_root / "notify.txt").read_text(encoding="utf-8").splitlines()[0]
    assert line.startswith("Shipyap: 3 drafts ready to review|")
    assert line.endswith(f"|shipyap://review/{result.draft_story}")


def test_no_ready_toast_or_link_without_a_draft_this_run(work, monkeypatch):
    cfg, _ = work
    monkeypatch.setattr(cli.review_page, "waiting", lambda config: ("a" * 64,))
    cli._tell(cfg, engine.RunResult(), [])
    assert "shipyap://" not in (cfg.workspace_root / "notify.txt").read_text(encoding="utf-8")


def test_waiting_is_empty_when_the_workspace_cannot_be_read(work):
    cfg, _ = work
    result = execute(cfg, model=Model(), allow_paid=True)
    assert review_page.waiting(cfg) == (result.draft_story,)  # what the page lists
    broken = replace(cfg, drafts=cfg.workspace_root / "missing", snapshots=cfg.drafts)
    assert review_page.waiting(broken) == ()


# --- evidence selection: a plan is paired with the code and tests behind it -------------------


class _Entry:
    def __init__(self, path):
        self.path = path


def _paths(entries):
    return [e.path for e in entries]


def test_a_protocol_is_paired_with_the_code_it_names_and_the_matching_test():
    candidates = [
        _Entry("docs/PHASE4_EXPERIMENT_PROTOCOL.md"),
        _Entry("docs/OTHER_NOTES.md"),
        _Entry("experiments/llm/freeze_protocol.py"),
        _Entry("experiments/llm/harness/world.py"),
        _Entry("tests/test_phase4_canary.py"),
        _Entry("tests/test_phase4_harness.py"),
    ]
    text = "The signing key exists only inside `harness/world.py`."
    ordered = _paths(selection.evidence_set(candidates, text))
    assert ordered[:3] == [
        "docs/PHASE4_EXPERIMENT_PROTOCOL.md",
        "experiments/llm/harness/world.py",
        "tests/test_phase4_harness.py",
    ]
    # Without the document's text, path words still pair a plan with code and a test.
    assert _paths(selection.evidence_set(candidates))[1:3] == [
        "experiments/llm/freeze_protocol.py",
        "tests/test_phase4_canary.py",
    ]


def test_later_records_and_the_code_they_discuss_outrank_what_the_plan_named():
    """Prior regression: the protocol named the old adapter; a deviation record and the
    canary describe the Responses adapter that actually ran. Later evidence decides."""
    candidates = [
        _Entry("docs/PHASE4_EXPERIMENT_PROTOCOL.md"),
        _Entry("evidence/phase4/PROTOCOL_DEVIATION_001.md"),
        _Entry("evidence/phase4/canary/ATTEMPT_1_FAILED.md"),
        _Entry("evidence/phase4/canary/CANARY.md"),
        _Entry("evidence/phase4/pilot-v1-INVALID/RESULTS.md"),
        _Entry("experiments/llm/models/openai_adapter.py"),
        _Entry("experiments/llm/models/responses_adapter.py"),
        _Entry("tests/test_phase4_canary.py"),
    ]
    texts = {
        "evidence/phase4/PROTOCOL_DEVIATION_001.md": "The Responses API rejected temperature. "
        "The Responses adapter is strict. Responses only.",
        "evidence/phase4/canary/CANARY.md": "Attempt 3 exercised the Responses adapter.",
    }
    lead = "The adapter `openai_adapter.py` retries without temperature."
    ordered = _paths(selection.evidence_set(candidates, lead, lambda e: texts.get(e.path)))
    assert ordered[:3] == [
        "docs/PHASE4_EXPERIMENT_PROTOCOL.md",
        "evidence/phase4/PROTOCOL_DEVIATION_001.md",
        "evidence/phase4/canary/CANARY.md",
    ]
    # The adapter the later records discuss comes first; the one only the plan named after it.
    assert ordered[3] == "experiments/llm/models/responses_adapter.py"
    assert ordered.index("experiments/llm/models/openai_adapter.py") > 3


def test_a_named_side_script_does_not_displace_the_code_the_records_discuss():
    """Prior live run: CANARY.md names capture_payload.py once and discusses the Responses
    adapter throughout; RESULTS_CORRECTED is a results document, not a correction record."""
    candidates = [
        _Entry("docs/PHASE4_EXPERIMENT_PROTOCOL.md"),
        _Entry("evidence/phase4/PROTOCOL_DEVIATION_001.md"),
        _Entry("evidence/phase4/canary/CANARY.md"),
        _Entry("evidence/phase4/pilot-v2/RESULTS_CORRECTED.md"),
        _Entry("experiments/llm/capture_payload.py"),
        _Entry("experiments/llm/models/responses_adapter.py"),
        _Entry("tests/test_phase4_canary.py"),
    ]
    texts = {
        "evidence/phase4/PROTOCOL_DEVIATION_001.md": "Responses API. Responses adapter. Responses.",
        "evidence/phase4/canary/CANARY.md": "The Responses adapter, Responses API, adapter SHA, "
        "adapter again (`benchmark/llm/capture_payload.py`, now also run in CI).",
    }
    ordered = _paths(selection.evidence_set(candidates, "", lambda e: texts.get(e.path)))
    assert ordered[1:3] == [
        "evidence/phase4/PROTOCOL_DEVIATION_001.md",
        "evidence/phase4/canary/CANARY.md",
    ]
    assert ordered[3] == "experiments/llm/models/responses_adapter.py"


def test_the_rules_for_plan_only_claims_motives_and_absolutes_are_in_every_prompt():
    assert "committed \nin advance" not in story_prompt.SYSTEM_PROMPT  # one line, not split
    assert "worded as planned" in story_prompt.SYSTEM_PROMPT
    assert "the protocol specifies" in STORY_SYSTEM_PROMPT and "motives" in STORY_SYSTEM_PROMPT
    assert "Phase 4" in STORY_SYSTEM_PROMPT and "nothing, never" in STORY_SYSTEM_PROMPT
    assert "only support is a PLAN file is OVERSTATED" in factcheck.SYSTEM_PROMPT.replace("\n", " ")
    assert "judged on the factual claim" in factcheck.SYSTEM_PROMPT
    assert selection.role("evidence/phase4/canary/CANARY.md") == "RESULT"
    assert selection.role("docs/PHASE4_EXPERIMENT_PROTOCOL.md") == "PLAN"


def test_the_fact_check_reads_every_file_and_flags_outdated_sentences(work):
    cfg, path = work
    cfg = _with_author(cfg, path)
    model = Model()
    model.draft_text = model_text(FIRST_PERSON)
    model.verdicts = {"S03": ("OUTDATED", "The canary replaced this behaviour.")}
    result = execute(cfg, model=model, allow_paid=True)
    (check,) = model.fact_checks
    assert "role=" in check.user_text and "<<<DRAFT SEGMENTS>>>" in check.user_text
    assert "S03 OUTDATED: The canary replaced this behaviour." in " ".join(result.failures)
    saved = factcheck.load(cfg.state, result.draft_paths[0].name.removesuffix("-draft"))
    assert saved and saved["verdicts"][2]["verdict"] == "OUTDATED"
    spending = (
        State(cfg.state / "automation.sqlite")
        .db.execute("SELECT kind FROM spending ORDER BY id")
        .fetchall()
    )
    # Auto-repair tried twice (this fake cannot rewrite), so the draft is shelved with the flag.
    assert [r[0] for r in spending] == ["discovery", "draft", "fact_check", "repair", "repair"]
    assert "S03: FACT_CHECK_OUTDATED" in " ".join(result.shelved)
    desk = ReviewDesk(cfg.drafts, cfg.backlog, cfg.state / "r.sqlite", tuple(result.snapshots))
    assert desk.queue()[0] == []
    # Without the repair record, the review page shows the flag before the approval form.
    sid = result.draft_paths[0].name.removesuffix("-draft")
    shutil.rmtree(repair.record_dir(cfg.drafts, sid))
    item = desk.item(sid)
    page = story_page(item, "t", StoryView(), None, fact_check=saved)
    assert (
        "OUTDATED" in page
        and "<button disabled aria-describedby=approval-blocked-reason>Approve</button>" in page
    )
    assert page.index('class="panel flagged"') < page.index(
        "<button disabled aria-describedby=approval-blocked-reason>Approve</button>"
    )
    assert page.index("<h2>Fact check</h2>") > page.index("<summary>Details</summary>")
    # A second run does not check the same story again.
    execute(cfg, model=model, allow_paid=True)
    assert len(model.fact_checks) == 1


def test_names_with_digits_are_not_measured_numbers_and_backlog_text_cannot_say_i():
    assert number_tokens("on a real X11 display with SHA-256, GPT-4, Ed25519 and v2") == ()
    assert number_tokens("Phase 4 took 5 ms, 10x faster, 2026-08-12") == (
        "4",
        "5",
        "10",
        "2026",
        "8",
        "12",
    )
    assert ANGLE_LABEL.startswith(UNVERIFIED_PREFIX) and SUPPORT_LABEL.startswith(UNVERIFIED_PREFIX)


def test_reexamine_sets_aside_old_stories_and_reads_the_repository_again(work):
    cfg, _ = work
    model = Model()
    first = execute(replace(cfg, max_new_drafts=0), model=model, allow_paid=True)
    assert first.new_candidates == 1 and len(model.discovery_calls) == 1
    # Without paid opt-in, re-examining is refused before anything changes.
    with pytest.raises(AutomationError, match="REEXAMINE_NEEDS_PAID_OPT_IN"):
        execute(cfg, model=model, reexamine=(REPO,))
    again = execute(
        replace(cfg, max_new_drafts=0),
        model=Model(findings=[]),
        allow_paid=True,
        reexamine=(REPO,),
    )
    assert again.evidence_examined == 1  # read again at the same commit
    state = State(cfg.state / "automation.sqlite")
    rows = state.db.execute("SELECT revision FROM generation_attempts").fetchall()
    assert len(rows) == 2 and any("@archived-" in r[0] for r in rows)  # history kept
    assert len(state.set_aside()) == 1
    state.close()
    # The old story is set aside: no draft is planned from it.
    assert not engine._available(cfg, frozenset(State(cfg.state / "automation.sqlite").set_aside()))


def test_agent_instruction_folders_are_never_evidence():
    assert not selection.eligible(".claude/skills/evidence-first-research/SKILL.md")
    assert not selection.eligible(".agents/skills/x/SKILL.md")
    assert selection.eligible("docs/PROTOCOL.md")


def test_absolute_security_claims_are_flagged_and_dated_documents_are_ruled():
    assert absolute_phrases("Secrets cannot be recovered from the fingerprint.") == (
        "cannot be recovered",
    )
    assert absolute_phrases("A stable fingerprint lets a reviewer compare values.") == ()
    assert "pre-registration" in STORY_SYSTEM_PROMPT and "current" in STORY_SYSTEM_PROMPT
    assert "pre-registration" in story_prompt.SYSTEM_PROMPT
