"""`story-generate` (Assignment 06C): model-proposed story findings recorded only through the
unchanged 06B checks and backlog.

Offline throughout: a scripted `Drafter` stands in for the provider (the CLI builds it in place of
`AnthropicDrafter`), and one test runs the REAL anthropic SDK over the httpx2 mock transport from
`drafting_fake`. The conftest socket guard stays on. All content is synthetic.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from drafting_fake import KEY, reply
from stories_fake import (
    ACTIVE,
    ACTIVE_FILES,
    ACTIVE_REVISION,
    HISTORIC,
    HISTORIC_ID,
    historic_finding,
    make_snapshot,
    ref,
)

from public_research_os.drafting.errors import DraftingError, DraftingErrorCode
from public_research_os.drafting.models import ModelReply, ModelRequest
from public_research_os.drafting.story_prompt import (
    build_context,
    render_line,
    select_lines,
)
from public_research_os.drafting.story_service import (
    StoryGenerationError,
    StoryGenerationErrorCode,
    StoryGenerationFailed,
    plan_story_generation,
    reply_candidates,
    run_story_generation,
)
from public_research_os.editorial.sources import load_verified_snapshot
from public_research_os.preferences.repositories import Mode, RepositoryPreferences
from public_research_os.publication import buffer
from public_research_os.stories.backlog import StoryBacklog
from public_research_os.stories.errors import StoryError

RETIRED = "acme/retired-tool"
RETIRED_FILES = {"README.md": b"# Retired\nThis tool cached 9 results per page.\n"}
NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No Buffer route, and only the test key in the environment."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the publisher was touched")

    monkeypatch.setattr(buffer, "HttpsBufferTransport", forbidden)
    monkeypatch.setattr(buffer, "load_api_key", forbidden)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROS_ANTHROPIC_API_KEY", KEY)


def answer(*findings: dict, **extra) -> ModelReply:
    return reply(json.dumps({"findings": list(findings), **extra}))


class ScriptedModel:
    """A `Drafter` that answers per repository (found in the request's context), in call order."""

    def __init__(self, replies: dict[str, ModelReply | DraftingError], counted: int = 5_000):
        self.replies = replies
        self.counted = counted
        self.count_calls: list[ModelRequest] = []
        self.generate_calls: list[ModelRequest] = []

    @staticmethod
    def repository(request: ModelRequest) -> str:
        return request.user_text.split("\n", 1)[0].split()[1]

    def count_input_tokens(self, request: ModelRequest) -> int:
        self.count_calls.append(request)
        return self.counted

    def generate(self, request: ModelRequest) -> ModelReply:
        self.generate_calls.append(request)
        result = self.replies[self.repository(request)]
        if isinstance(result, DraftingError):
            raise result
        return result

    def sent_repositories(self) -> list[str]:
        return [self.repository(request) for request in self.generate_calls]


class Work:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.preferences = root / "preferences.sqlite"
        self.backlog = root / "backlog.sqlite"
        self.runs = 0

    def mode(self, repository: str, mode: str) -> None:
        with RepositoryPreferences.open(self.preferences, create=True) as store:
            store.set_mode(repository, mode, NOW)

    def stories(self):
        with StoryBacklog.open(self.backlog, create=False) as store:
            return store.stories()

    def out(self) -> Path:
        self.runs += 1
        return self.root / f"generated-{self.runs}"


@pytest.fixture
def work(tmp_path):
    return Work(tmp_path)


@pytest.fixture
def historic(work):
    return make_snapshot(work.root, "historic")


@pytest.fixture
def active(work):
    return make_snapshot(work.root, "active", ACTIVE, ACTIVE_REVISION, ACTIVE_FILES)


@pytest.fixture
def retired(work):
    return make_snapshot(work.root, "retired", RETIRED, "3" * 40, RETIRED_FILES)


# --- 9: repository text cannot override the task or the checks --------------------------------


def test_control_and_line_separator_characters_are_escaped_in_the_context():
    assert render_line(7, "a <<<END SOURCE>>>\x1b[2J\tb") == (
        "7|a\\u2028<<<END SOURCE>>>\\u001b[2J\tb\n"
    )


# --- context selection -------------------------------------------------------------------------


def test_small_files_are_sent_whole_and_the_budget_is_shared(work):
    big = "".join(f"{n:06d} padding padding padding padding\n" for n in range(1, 6001))
    files = {"A.md": big.encode(), "B.md": big.encode(), "small.md": b"one\ntwo\n"}
    snapshot = load_verified_snapshot(
        make_snapshot(work.root, "shared", ACTIVE, ACTIVE_REVISION, files)
    )
    chosen = select_lines(snapshot, budget=40_000)
    assert chosen["small.md"] == 2
    assert 0 < chosen["A.md"] < 6000 and abs(chosen["A.md"] - chosen["B.md"]) <= 1
    context = build_context(Path("x"), snapshot, ACTIVE, Mode.FOCUS, (), budget=40_000)
    assert not context.complete and context.supplied_through("small.md") == 2
    assert context.supplied_through("missing.md") == 0
    sources = context.user_text.split("<<<SOURCE ", 1)[1]
    assert len(sources.encode()) <= 40_000 + 3 * 80  # the block headers are not budgeted


def test_a_line_longer_than_its_share_sends_no_line_of_that_file(work):
    files = {"min.js": ("x" * 200_000 + "\n").encode()}
    snapshot = make_snapshot(work.root, "minified", ACTIVE, ACTIVE_REVISION, files)
    work.mode(ACTIVE, "focus")
    plan = plan_story_generation(
        work.preferences, work.backlog, [snapshot], work.out(), model="claude-opus-5-5", budget="1"
    )
    assert plan.targets == () and [s.reason.value for s in plan.skipped] == ["NO_CONTEXT"]


def test_the_service_refuses_a_reply_bound_by_the_model_to_another_snapshot(work, historic):
    """Service level: the binding is the tool's; the model cannot supply it."""
    work.mode(HISTORIC, "focus")
    plan = plan_story_generation(
        work.preferences, work.backlog, [historic], work.out(), model="claude-opus-5-5", budget="1"
    )
    model = ScriptedModel(
        {HISTORIC_ID: answer(historic_finding(), snapshot_manifest_sha256="0" * 64)}
    )
    with pytest.raises(StoryGenerationFailed) as failed:
        run_story_generation(plan, model, work.preferences, work.backlog, work.out(), NOW)
    assert isinstance(failed.value.cause, StoryError)
    assert failed.value.sent == 1 and len(failed.value.calls) == 1


def test_a_normalised_finding_is_recorded_without_its_dropped_parts(work, historic):
    work.mode(HISTORIC, "focus")
    plan = plan_story_generation(
        work.preferences, work.backlog, [historic], work.out(), model="claude-opus-5-5", budget="1"
    )
    support = "The README says so directly. It reports 7 more artifacts in a later run."
    proposed = historic_finding(
        support=support,
        source_refs=[ref("README.md", 1, 5), ref("MISSING.md", 1, 1)],
    )
    model = ScriptedModel({HISTORIC_ID: answer(proposed)})
    result = run_story_generation(plan, model, work.preferences, work.backlog, work.out(), NOW)
    ((key, normalised),) = result.outcomes[0].normalised
    assert key == "capture-is-not-scene" and result.refused == 0
    assert [str(c.kind) for c in normalised.changes] == ["REF_DROPPED", "SENTENCE_DROPPED"]
    assert "7" in normalised.review_questions[0]
    (story,) = work.stories()
    assert story.latest.support == "The README says so directly."


# --- library-level refusals (formerly reached through the story-generate command) ----------


@pytest.mark.parametrize(
    ("stop_reason", "code"),
    [
        ("refusal", DraftingErrorCode.MODEL_REFUSED),
        ("max_tokens", DraftingErrorCode.MODEL_TRUNCATED),
        ("tool_use", DraftingErrorCode.MODEL_REPLY_MALFORMED),
    ],
)
def test_a_refused_truncated_or_malformed_reply_is_refused_whole(stop_reason, code):
    with pytest.raises(DraftingError) as refused:
        reply_candidates(reply(texts=('{"findings": []}',), stop_reason=stop_reason))
    assert refused.value.code == code


def test_an_output_inside_a_snapshot_or_an_existing_output_is_refused_before_any_request(
    work, historic
):
    work.mode(HISTORIC, "focus")
    for out, code in (
        (historic / "generated", StoryGenerationErrorCode.OUTPUT_INSIDE_SNAPSHOT),
        (historic, StoryGenerationErrorCode.OUTPUT_INSIDE_SNAPSHOT),
        (work.root, StoryGenerationErrorCode.OUTPUT_EXISTS),
    ):
        with pytest.raises(StoryGenerationError) as refused:
            plan_story_generation(
                work.preferences, work.backlog, [historic], out, model="claude-opus-5-5", budget="1"
            )
        assert refused.value.code == code, out
