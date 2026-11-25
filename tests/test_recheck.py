"""`pros factcheck`: re-run only the fact check on an existing draft bundle. SYNTHETIC fixtures:
the public C1 cases code-label and code-shared-flag (refund-desk snapshot); the provider is a
fake. No network, no key. The real ordinary-trial drafts are in tests/private/."""

import json
import re
import shutil
import sqlite3
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import pytest
from drafting_fake import reply

from shipyap.automation import cli, recheck
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.engine import STOP_FILE
from shipyap.automation.state import State
from shipyap.drafting.errors import DraftingError, DraftingErrorCode

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public"
SNAPSHOT = "refund-desk"
BADGE = "a1" * 32  # code-label: S02 says what a label means
MINT = "b2" * 32  # code-shared-flag: S02 and S03 read one shared flag


class FakeModel:
    def __init__(self, verdicts=None, text=None):
        self.verdicts = verdicts or {}
        self.text = text
        self.counts, self.requests = [], []

    def count_input_tokens(self, request):
        self.counts.append(request)
        return 22_000

    def generate(self, request):
        self.requests.append(request)
        if self.text is not None:
            return reply(self.text)
        ids = re.findall(r"^(S\d+): ", request.user_text, re.MULTILINE)
        items = [
            {
                "segment_id": sid,
                "verdict": self.verdicts.get(sid, "SUPPORTED"),
                "reason": f"Reason for {sid}.",
                "refs": [{"path": "app/desk.tsx", "line_start": 1, "line_end": 2}],
            }
            for sid in ids
        ]
        return reply(json.dumps({"verdicts": items}))


@pytest.fixture
def ws(tmp_path):
    config = tmp_path / "pros.toml"
    config.write_text(
        f"""workspace_root = '{tmp_path}'
workspace_id = 'ws_test'
preferences = 'preferences.sqlite'
backlog = 'backlog.sqlite'
drafts = 'drafts'
snapshots = 'snapshots'
state = 'automation'
model = 'claude-sonnet-5-5'
max_cost_usd = '1.00'
""",
        encoding="utf-8",
    )
    shutil.copytree(PUBLIC / "snapshots" / SNAPSHOT, tmp_path / "snapshots" / SNAPSHOT)
    for sid, case in ((BADGE, "code-label"), (MINT, "code-shared-flag")):
        draft = tmp_path / "drafts" / f"{sid}-draft"
        draft.mkdir(parents=True)
        shutil.copy(PUBLIC / "drafts" / "refund.brief.json", draft / "brief.json")
        shutil.copy(PUBLIC / "drafts" / f"{case}.response.json", draft / "response.json")
    return load_config(config), config


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_preview_builds_the_c1_request_and_sends_nothing(ws, capsys):
    cfg, path = ws
    before = _tree(cfg.workspace_root)
    model = FakeModel()
    args = Namespace(config=path, draft=MINT[:8], allow_paid=False)
    assert cli._factcheck(args, model_factory=lambda: model) == 0
    out = capsys.readouterr().out
    assert "S03: WARNING CODE_MEANING_INCOMPLETE; also sent: app/desk.tsx:7-7" in out
    assert "Preview only: nothing was sent" in out
    assert not model.counts and not model.requests
    assert _tree(cfg.workspace_root) == before  # nothing written, not even the state database


def test_prepare_sends_the_meaning_lines_beside_their_sentence(ws):
    cfg, _ = ws
    prepared = recheck.prepare(cfg, BADGE[:8])
    assert prepared.story_id == BADGE
    assert prepared.supplied == {
        "S02": ("api/review.ts:8-8", "api/review.ts:16-16", "app/desk.tsx:23-23")
    }
    text = prepared.request.user_text
    assert "<<<ALSO CHECK id=S02>>>" in text and "<<<ALSO CHECK id=S01>>>" not in text
    assert Decimal(0) < prepared.ceiling_usd < Decimal("0.15")  # cents, not dollars


def test_paid_rerun_prints_each_verdict_and_stores_it_beside_the_bundle_only(ws, capsys):
    cfg, path = ws
    (cfg.state / "fact-checks").mkdir(parents=True)
    stored = cfg.state / "fact-checks" / f"{MINT}.json"
    stored.write_text('{"draft": "old", "verdicts": []}', encoding="utf-8")
    bundle = _tree(cfg.drafts / f"{MINT}-draft")
    model = FakeModel({"S02": "OVERSTATED", "S03": "CONTRADICTED"})
    args = Namespace(config=path, draft=MINT[:8], allow_paid=True)
    assert cli._factcheck(args, model_factory=lambda: model) == 0
    out = capsys.readouterr().out
    assert (
        "  S02 OVERSTATED [judged with app/desk.tsx:13-13, app/desk.tsx:17-17]: Reason for S02."
        in out
    )
    assert "  S03 CONTRADICTED [judged with app/desk.tsx:7-7]: Reason for S03." in out
    assert "  S01 SUPPORTED: Reason for S01." in out
    assert len(model.requests) == 1 and "<<<ALSO CHECK id=S03>>>" in model.requests[0].user_text

    record_path = cfg.drafts / f"{MINT}-recheck" / "fact-check-01.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["draft"] == f"{MINT}-draft" and record["error"] is None
    assert record["prompt_version"] == "f2"
    s03 = next(v for v in record["verdicts"] if v["segment_id"] == "S03")
    assert s03["verdict"] == "CONTRADICTED" and s03["supplied"] == ["app/desk.tsx:7-7"]
    assert record["reply"]["texts"]  # the raw paid reply is kept for free replay
    assert Decimal(record["reserved_usd"]) > 0

    # Nothing else changed: the bundle, the review page's fact check, no draft slot used.
    assert _tree(cfg.drafts / f"{MINT}-draft") == bundle
    assert stored.read_text(encoding="utf-8") == '{"draft": "old", "verdicts": []}'
    db = sqlite3.connect(cfg.state / "automation.sqlite")
    assert db.execute("SELECT kind FROM spending").fetchall() == [("fact_check",)]
    db.close()

    # A second run never overwrites the first paid result.
    assert cli._factcheck(args, model_factory=lambda: model) == 0
    assert (cfg.drafts / f"{MINT}-recheck" / "fact-check-02.json").is_file()
    assert json.loads(record_path.read_text(encoding="utf-8")) == record


def test_the_spend_cap_refuses_before_any_request(ws, capsys):
    cfg, path = ws
    path.write_text(path.read_text(encoding="utf-8").replace("'1.00'", "'0.01'"), encoding="utf-8")
    model = FakeModel()
    args = Namespace(config=path, draft=BADGE[:8], allow_paid=True)
    assert cli._factcheck(args, model_factory=lambda: model) == 2
    assert "BUDGET_EXCEEDED" in capsys.readouterr().out
    assert model.counts and not model.requests
    assert not (cfg.drafts / f"{BADGE}-recheck").exists()


def test_stop_file_and_usage_hold_send_nothing(ws):
    cfg, _ = ws
    model = FakeModel()
    (cfg.workspace_root / STOP_FILE).write_text("", encoding="utf-8")
    with pytest.raises(AutomationError, match="STOPPED"):
        recheck.run(cfg, BADGE[:8], lambda: model)
    (cfg.workspace_root / STOP_FILE).unlink()
    cfg.state.mkdir(parents=True, exist_ok=True)
    state = State(cfg.state / "automation.sqlite")
    state.hold("PROVIDER_USAGE_LIMIT", "2999-01-01T00:00:00Z", "2026-11-21T00:00:00Z")
    state.close()
    with pytest.raises(AutomationError, match="USAGE_LIMIT_WAIT"):
        recheck.run(cfg, BADGE[:8], lambda: model)
    assert not model.counts and not model.requests


def test_an_unusable_reply_is_still_stored(ws, capsys):
    cfg, path = ws
    model = FakeModel(text="not json")
    args = Namespace(config=path, draft=BADGE[:8], allow_paid=True)
    assert cli._factcheck(args, model_factory=lambda: model) == 2
    assert "FACT_CHECK_REPLY_UNUSABLE" in capsys.readouterr().out
    record = json.loads(
        (cfg.drafts / f"{BADGE}-recheck" / "fact-check-01.json").read_text(encoding="utf-8")
    )
    assert record["error"] == "FACT_CHECK_REPLY_UNUSABLE" and record["verdicts"] == []
    assert record["reply"]["texts"] == ["not json"]


def test_a_provider_failure_stores_nothing(ws, capsys):
    cfg, path = ws

    class Failing(FakeModel):
        def generate(self, request):
            raise DraftingError(DraftingErrorCode.PROVIDER_TIMEOUT)

    args = Namespace(config=path, draft=BADGE[:8], allow_paid=True)
    assert cli._factcheck(args, model_factory=Failing) == 2
    assert "PROVIDER_TIMEOUT" in capsys.readouterr().out
    assert not (cfg.drafts / f"{BADGE}-recheck").exists()


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        ("A1A1A1A1", "DRAFT_ID_INVALID"),
        ("a1a1", "DRAFT_ID_INVALID"),
        ("../../x0000000", "DRAFT_ID_INVALID"),
        ("*", "DRAFT_ID_INVALID"),
        ("deadbeef", "DRAFT_NOT_FOUND"),
    ],
)
def test_bad_or_unknown_draft_ids_are_refused(ws, draft, code):
    cfg, _ = ws
    with pytest.raises(AutomationError, match=code):
        recheck.prepare(cfg, draft)


def test_an_ambiguous_prefix_is_refused(ws):
    cfg, _ = ws
    shutil.copytree(cfg.drafts / f"{BADGE}-draft", cfg.drafts / f"{BADGE[:8]}{'0' * 56}-draft")
    with pytest.raises(AutomationError, match="DRAFT_AMBIGUOUS"):
        recheck.prepare(cfg, BADGE[:8])


def test_a_missing_snapshot_is_refused(ws):
    cfg, _ = ws
    shutil.rmtree(cfg.snapshots / SNAPSHOT)
    with pytest.raises(AutomationError, match="SNAPSHOT_NOT_FOUND"):
        recheck.prepare(cfg, BADGE[:8])


def test_the_command_is_registered(ws, capsys):
    _, path = ws
    assert cli.main(["factcheck", "--config", str(path), "--draft", MINT[:8]]) == 0
    assert "Preview only" in capsys.readouterr().out
