"""An auto-repaired draft inherits its origin's completed fact check only when every change since
is a free repair (scope line added, ID dropped, tool-text sentence dropped) or a model rewrite
with its own re-check; anything else stays "Not fact-checked". `pros eval --workspace` reports
every live draft's approvability and holds it to pins. Offline; synthetic data only."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from stories_fake import HISTORIC_FILES
from test_review import CHANNEL, DUE, NOW, R, Work, refused

from shipyap.automation import evaluation, factcheck, live_drafts
from shipyap.automation import repair as auto_repair
from shipyap.automation.cli import main
from shipyap.automation.review import REVIEW_LEDGER
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.service import build_from_response, prepare_build
from shipyap.review import required
from shipyap.review.pages import StoryView, story_page
from shipyap.review.ux import status

PUBLIC = Path(__file__).resolve().parents[1] / "evals" / "public"
REWRITE = "A screenshot proves capture, not the right scene."
SCOPE_NOTE = "the record's own scope line, verbatim"
# The review fixture's README plus its own scope paragraph (lines 7-8), as a record would have.
SCOPED = {
    "README.md": HISTORIC_FILES["README.md"]
    + b"\nScope: one synthetic configuration. It does not test production capture.\n"
}


@pytest.fixture
def work(tmp_path):
    return Work(tmp_path, files=SCOPED)


def _verdict(sid: str, verdict: str = "SUPPORTED", reason: str = "fixture") -> dict:
    return {"segment_id": sid, "verdict": verdict, "reason": reason, "refs": []}


def repaired(
    work,
    *,
    rewrite: str | None = REWRITE,
    recheck: bool = True,
    edit=None,
    scope_edit=None,
    record_edit=None,
    origin_verdicts=None,
):
    """Build what the engine builds after a CLEAN repair: the record's own scope line appended
    and (optionally) S01 rewritten by a model round with its re-check; the record's fact check,
    like the live one, has no verdict for the added scope sentence. Returns the desk."""
    origin = json.loads((work.path("draft") / "response.json").read_bytes())
    brief = parse_brief((work.path("brief") / "brief.json").read_bytes())
    data = json.loads(json.dumps(origin))
    scope = auto_repair._scope_segment(brief, data)
    assert scope is not None
    if scope_edit:
        scope_edit(scope)
    data["segments"].append(scope)
    rounds = []
    if rewrite is not None:
        data["segments"][0]["text"] = rewrite
        rounds.append({"round": 1, "sent": ["S01"], "rewrites": {"S01": rewrite}, "failed": None})
    if edit:
        edit(data)
    data["generator"]["label"] = (data["generator"]["label"][:80] + " | auto-repaired")[:100]
    raw = auto_repair.raw(data)
    out = auto_repair.repaired_dir(work.drafts, work.sid, raw)
    build_from_response(prepare_build(work.snapshot, work.path("brief")), raw, out)
    verdicts = [_verdict(s["id"]) for s in origin["segments"]]
    if rewrite is not None and recheck:
        verdicts[0] = _verdict("S01", reason="re-checked")
    elif rewrite is not None:
        verdicts = verdicts[1:]
    record = {
        "repair_version": auto_repair.REPAIR_VERSION,
        "origin_draft": work.path("draft").name,
        "final_draft": out.name,
        "status": auto_repair.CLEAN,
        "reasons": [],
        "free": [f"{scope['id']}: added the record's own scope line"],
        "rounds": rounds,
        "reserved_usd": "0",
        "charged_usd": "0",
        "fact_check": {"verdicts": verdicts, "draft": out.name},
    }
    if record_edit:
        record_edit(record)
    auto_repair.save_record(work.drafts, work.sid, record)
    desk = work.desk()
    if origin_verdicts is not None:
        desk.fact_checks = lambda _sid: {
            "draft": work.path("draft").name,
            "verdicts": origin_verdicts([_verdict(s["id"]) for s in origin["segments"]]),
        }
    return desk


def _blocked_not_checked(desk, work):
    item = desk.item(work.sid)
    gate = desk.gate(item)
    assert desk.fact_check(item) is None
    assert any(n.code == "FACT_CHECK_REQUIRED" for n in gate.document_blocks)
    assert status(item, gate) == "Not fact-checked - cannot approve"
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.APPROVAL_BLOCKED


def test_a_scope_line_and_a_rechecked_rewrite_inherit_the_origin_fact_check(work):
    desk = repaired(work)
    item = desk.item(work.sid)
    assert item.repaired is not None and item.base.directory != item.origin.directory
    # Before this fix: the record's check misses the scope sentence, so it is incomplete.
    assert not required.completed_fact_check(item.repair["fact_check"], item.base.response)
    check = desk.fact_check(item)
    assert check["inherited_from"] == work.path("draft").name
    assert check["draft"] == item.base.directory.name
    scope_id = item.base.response.segments[-1].id
    assert check["basis"]["S01"] == "re-checked after repair round 1"
    assert check["basis"]["S02"] == "inherited"
    assert SCOPE_NOTE in check["basis"][scope_id]
    assert scope_id not in {v["segment_id"] for v in check["verdicts"]}  # no invented verdict
    gate = desk.gate(item)
    assert not any(n.code == "FACT_CHECK_REQUIRED" for n in gate.document_blocks)
    assert gate.approvable, gate.reasons()
    assert desk.approval_candidate(work.sid, CHANNEL, DUE, NOW).text == item.current.text
    page = story_page(item, "token", StoryView(), None, gate=gate, fact_check=check)
    assert f"Fact-checked, inherited from {work.path('draft').name}" in page
    assert "Not fact-checked" not in page
    items, refusals = desk.queue()
    assert [i.story.story_id for i in items] == [work.sid] and not refusals


def test_a_free_scope_line_alone_inherits_every_other_verdict(work):
    desk = repaired(work, rewrite=None)
    check = desk.fact_check(desk.item(work.sid))
    assert check is not None and check["basis"]["S01"] == "inherited"
    assert desk.gate(desk.item(work.sid)).approvable


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(None, id="never-fact-checked"),
        pytest.param(lambda verdicts: verdicts[1:], id="failed-partial-check"),
        pytest.param(lambda verdicts: [], id="skipped-empty-check"),
        pytest.param(lambda vs: [dict(v, verdict="MAYBE") for v in vs], id="corrupt-verdict"),
    ],
)
def test_an_origin_without_a_completed_check_stays_blocked(work, origin):
    desk = repaired(work, origin_verdicts=origin)
    if origin is None:
        desk.fact_checks = lambda _sid: None
    _blocked_not_checked(desk, work)


def test_an_origin_check_of_another_draft_or_a_sample_stays_blocked(work):
    desk = repaired(work)
    good = desk.fact_checks(work.sid)
    desk.fact_checks = lambda _sid: dict(good, draft="another-draft")
    _blocked_not_checked(desk, work)
    desk.fact_checks = lambda _sid: dict(good, label="SAMPLE fact check (scripted)")
    _blocked_not_checked(desk, work)


def test_a_model_rewrite_without_its_own_recheck_stays_blocked(work):
    _blocked_not_checked(repaired(work, recheck=False), work)


def test_a_rewrite_from_a_failed_round_is_not_a_recorded_rewrite(work):
    def failed(record):
        record["rounds"][0]["failed"] = "REPAIR_REPLY_UNUSABLE"

    _blocked_not_checked(repaired(work, record_edit=failed), work)


def test_a_sentence_changed_outside_the_repair_record_stays_blocked(work):
    """A human (or anything unrecorded) changed S02 in the repaired bundle: no re-check."""

    def edit(data):
        data["segments"][1]["text"] = "The README reports the scene confirmed for 3/10 artifacts."

    _blocked_not_checked(repaired(work, edit=edit), work)


def test_a_changed_reference_stays_blocked_even_with_the_same_words(work):
    def edit(data):
        data["segments"][1]["source_refs"][0]["quote"] = None

    _blocked_not_checked(repaired(work, edit=edit), work)


def test_an_added_sentence_that_is_not_the_verbatim_scope_line_stays_blocked(work):
    def scope_edit(scope):
        scope["text"] = scope["text"].replace("does not test", "also covers")

    _blocked_not_checked(repaired(work, scope_edit=scope_edit), work)


def test_a_second_copy_of_the_scope_line_stays_blocked(work):
    def edit(data):
        copy = dict(data["segments"][-1], id="S09")
        data["segments"].append(copy)

    _blocked_not_checked(repaired(work, edit=edit), work)


def test_a_dropped_sentence_nobody_recorded_stays_blocked(work):
    def edit(data):
        data["segments"] = [s for s in data["segments"] if s["id"] != "S02"]

    _blocked_not_checked(repaired(work, edit=edit), work)


def test_an_inherited_contradicted_verdict_still_blocks_approval(work):
    def contradicted(verdicts):
        verdicts[1] = _verdict("S02", "CONTRADICTED", "a later record disagrees")
        return verdicts

    desk = repaired(work, origin_verdicts=contradicted)
    item = desk.item(work.sid)
    assert desk.fact_check(item)["inherited_from"] == work.path("draft").name
    gate = desk.gate(item)
    assert not any(n.code == "FACT_CHECK_REQUIRED" for n in gate.document_blocks)
    s02 = next(s for s in gate.segments if s.segment_id == "S02")
    assert [n.code for n in s02.blocks] == ["FACT_CHECK_CONTRADICTED"]
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.APPROVAL_BLOCKED


def test_an_owner_edit_on_the_page_keeps_the_not_rechecked_warning_not_a_block(work):
    desk = repaired(work)
    item = desk.item(work.sid)
    texts = {s.id: s.text for s in item.current.response.segments}
    texts["S02"] = "The README reports that the scene was confirmed for 3/10 synthetic artifacts."
    preview = desk.preview(item, texts)
    s02 = next(s for s in preview.gate.segments if s.segment_id == "S02")
    assert [n.code for n in s02.warnings] == ["NOT_RECHECKED"] and not s02.blocks
    assert not any(n.code == "FACT_CHECK_REQUIRED" for n in preview.gate.document_blocks)


# --- the pure rules, on the public synthetic cases ----------------------------------------------


def _case(name: str):
    _stories, drafts = evaluation.load_cases(PUBLIC)
    case = next(c for c in drafts if c.name == name)
    brief = parse_brief(case.brief)
    from shipyap.drafting.service import checked_input

    response = parse_response(checked_input(case.response, brief).data)
    return case, brief, response


def _complete(response) -> dict:
    return {"verdicts": [_verdict(s.id) for s in response.segments]}


def _inherit(case, brief, response, outcome, origin=None):
    amap = auto_repair.assess(brief, response, case.records, None).amap
    return required.inherited_fact_check(
        brief,
        response,
        amap,
        origin if origin is not None else _complete(response),
        outcome.response,
        outcome.record("origin", "repaired"),
        "origin",
        "repaired",
    )


def test_a_dropped_unneeded_id_inherits_the_verdict():
    case, brief, response = _case("hash-aside")
    outcome = auto_repair.repair(brief, response, case.records, None, None, None, "m")
    assert any("dropped the unneeded ID" in line for line in outcome.free)
    inherited = _inherit(case, brief, response, outcome)
    assert inherited is not None
    assert any("an unneeded ID was dropped" in how for how in inherited.basis.values())


def test_a_dropped_tool_text_sentence_is_explained_and_an_unrecorded_drop_is_not():
    case, brief, response = _case("tool-text-short")
    outcome = auto_repair.repair(brief, response, case.records, None, None, None, "m")
    assert "S06" not in {s.id for s in outcome.response.segments}  # the free tool-text drop
    assert _inherit(case, brief, response, outcome) is not None  # (shelved for length, apart)
    data = outcome.response.model_dump(mode="json")
    data["segments"] = data["segments"][1:]  # the opening sentence vanished: nobody recorded it
    outcome.response = parse_response(auto_repair.raw(data))
    assert _inherit(case, brief, response, outcome) is None


def test_the_eval_reports_what_a_repaired_draft_fact_check_rests_on():
    _stories, drafts = evaluation.load_cases(PUBLIC)
    report = evaluation.evaluate([], drafts)
    got = {d.name: d.repair.fact_check for d in report.drafts if d.repair is not None}
    # No public case carries a complete origin check, so none inherits one.
    assert got["hash-aside"] == got["missing-scope"] == got["fixture-repair"] == "REQUIRED"
    assert got["tool-text-short"] is None  # shelved: never offered


# --- pros eval --workspace: every live draft, pinned ------------------------------------------


def _workspace(work, root: Path) -> Path:
    """A pros.toml workspace around the review fixture (paths as `pros init` lays them out)."""
    (root / "snapshots").mkdir()
    shutil.copytree(work.snapshot, root / "snapshots" / work.snapshot.name)
    state = root / "automation"
    (state / factcheck.DIRECTORY).mkdir(parents=True)
    (state / factcheck.DIRECTORY / f"{work.sid}.json").write_text(
        json.dumps(work.desk().fact_checks(work.sid)), encoding="utf-8"
    )
    shutil.copy(work.ledger, state / REVIEW_LEDGER)
    db = sqlite3.connect(state / "automation.sqlite")  # no stored replies: drafts only
    db.execute("CREATE TABLE generation_attempts (repo, revision, request_json, reply_json)")
    db.commit()
    db.close()
    (root / "pros.toml").write_text(
        f"""workspace_root = '{root.as_posix()}'
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
    return root


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _cases(tmp_path: Path, pins: dict) -> Path:
    cases = tmp_path / "cases"
    (cases / "drafts").mkdir(parents=True)
    (cases / "stories").mkdir()
    (cases / "drafts.json").write_text('{"cases": []}', encoding="utf-8")
    (cases / "expected.json").write_text(
        json.dumps({"stories": {}, "drafts": {}, "workspace_drafts": pins}), encoding="utf-8"
    )
    return cases


def test_the_live_replay_reports_each_draft_and_changes_nothing(work):
    repaired(work)
    root = _workspace(work, work.root)
    before = _tree(root)
    drafts = live_drafts.replay_live(root)
    assert _tree(root) == before  # read-only: no ledger lock file, no eval case, no revision
    assert [(d.story_id, d.approvable, d.fact_check) for d in drafts] == [
        (work.sid, True, required.INHERITED)
    ]
    line = live_drafts.render(drafts)[0]
    assert "approvable: yes (fact-checked (inherited from" in line


def test_a_pinned_good_draft_that_a_gate_change_blocks_fails_the_replay(
    work, tmp_path, monkeypatch, capsys
):
    repaired(work)
    root = _workspace(work, work.root)
    cases = _cases(tmp_path, {work.sid: {"approvable": True, "fact_check": "INHERITED"}})
    args = ["eval", "--cases", str(cases), "--workspace", str(root), "--check"]
    assert main(args) == 0
    assert capsys.readouterr().out.rstrip().endswith("GATE PASS")
    # The bug this pins: a gate that no longer carries the origin's check blocks a good draft.
    monkeypatch.setattr(required, "inherited_fact_check", lambda *a, **k: None)
    assert main(args) == 1
    out = capsys.readouterr().out
    assert f"LIVE {work.sid[:16]}: approvable: no (FACT_CHECK_REQUIRED)" in out
    assert f"GATE FAIL LIVE {work.sid[:16]}: approvable False, pinned True" in out


def test_an_unpinned_live_draft_or_a_pin_without_its_draft_fails(work, tmp_path, capsys):
    repaired(work)
    root = _workspace(work, work.root)
    cases = _cases(tmp_path, {"f" * 64: {"approvable": False}})
    assert main(["eval", "--cases", str(cases), "--workspace", str(root), "--check"]) == 1
    out = capsys.readouterr().out
    assert f"LIVE {'f' * 16}: pinned draft missing from the workspace" in out
    assert f"LIVE {work.sid[:16]}: not pinned in expected.json workspace_drafts" in out


def test_pins_are_not_checked_without_a_workspace(tmp_path):
    cases = _cases(tmp_path, {"f" * 64: {"approvable": True}})
    assert main(["eval", "--cases", str(cases), "--check"]) == 0
