"""Story/revision exclusion, concurrent lexical duplicates and explicit owner reposts; offline."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from publication_fake import CHANNEL, DUE, NOW, POST_TEXT
from test_review import EDITED_S01, ScriptedPublisher, approve_current, schedule_here, texts
from test_review import serve as serve
from test_review import work as work
from test_review_ux import Fields

from shipyap.automation import publication_replay
from shipyap.publication.approval import Candidate, confirmation_phrase
from shipyap.publication.errors import PublicationError
from shipyap.publication.errors import PublicationErrorCode as E
from shipyap.publication.history import fingerprint, repost_phrase, similarity
from shipyap.publication.ledger import IntentState as S
from shipyap.publication.ledger import Ledger, ReviewSubject
from shipyap.publication.migration import migrate, preview
from shipyap.publication.port import CreateKind, CreateOutcome
from shipyap.publication.service import submit
from shipyap.review.pages import StoryView, queue_page, story_page


class Recorder:
    def __init__(self, barrier=None):
        self.destinations = 0
        self.creates = 0
        self.barrier = barrier

    def check_destination(self, *_args):
        self.destinations += 1
        if self.barrier:
            self.barrier.wait(timeout=10)

    def create_scheduled_post(self, *_args):
        self.creates += 1
        return CreateOutcome(CreateKind.ACCEPTED, "ACCEPTED", "0" * 24)


def candidate(**changes):
    return replace(
        Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "b" * 64, "a" * 64), **changes
    )


def subject(post):
    return ReviewSubject(
        post.story_id, "sample-draft", post.bundle_record_sha256, "c" * 64, "d" * 64
    )


def refusal(call, *args, **kwargs):
    with pytest.raises(PublicationError) as raised:
        call(*args, **kwargs)
    return raised.value.code


@pytest.mark.parametrize("state", list(S))
def test_new_date_and_revision_never_unlock_a_story(tmp_path, state):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as ledger:
        first = candidate()
        ledger.add_approval(first, NOW)
        provider = Recorder()
        submit(ledger, provider, first, first.approval_id, NOW)
        if state != S.ACCEPTED:
            if state in {S.REJECTED, S.AMBIGUOUS}:
                # These outcomes come straight from SENDING, so use a separate fresh ledger.
                pass
            elif state != S.SENDING:
                ledger.transition(first.approval_id, state, NOW)
        # Reconstruct a provider outcome, preserving identity, using a fresh synthetic ledger.
        if state in {S.SENDING, S.REJECTED, S.AMBIGUOUS}:
            with Ledger.open(tmp_path / "other.sqlite", create=True) as other:
                other.add_approval(first, NOW)
                other.begin_send(first.approval_id, NOW)
                if state != S.SENDING:
                    other.transition(first.approval_id, state, NOW)
                changed = candidate(
                    text=POST_TEXT + " A revised ending.",
                    due_at="2026-11-04T04:00:00Z",
                    bundle_record_sha256="f" * 64,
                )
                other.add_approval(changed, NOW)
                empty = Recorder()
                assert (
                    refusal(submit, other, empty, changed, changed.approval_id, NOW)
                    == E.STORY_ALREADY_SUBMITTED
                )
                assert empty.destinations == empty.creates == 0
        else:
            changed = candidate(
                text=POST_TEXT + " A revised ending.",
                due_at="2026-11-04T04:00:00Z",
                bundle_record_sha256="f" * 64,
            )
            ledger.add_approval(changed, NOW)
            empty = Recorder()
            assert (
                refusal(submit, ledger, empty, changed, changed.approval_id, NOW)
                == E.STORY_ALREADY_SUBMITTED
            )
            assert empty.destinations == empty.creates == 0


@pytest.mark.parametrize("near", [False, True], ids=["same-story", "near-text-different-story"])
def test_two_approvals_racing_reserve_only_one_send(tmp_path, near):
    path = tmp_path / "ledger.sqlite"
    a = candidate()
    b = candidate(
        due_at="2026-11-04T04:00:00Z",
        story_id="f" * 64 if near else a.story_id,
        text=POST_TEXT.upper() if near else POST_TEXT,
    )
    with Ledger.open(path, create=True) as ledger:
        ledger.add_approval(a, NOW)
        ledger.add_approval(b, NOW)
    barrier = threading.Barrier(2)
    recorders = [Recorder(barrier), Recorder(barrier)]

    def send(post, recorder):
        with Ledger.open(path, create=False) as ledger:
            try:
                return submit(ledger, recorder, post, post.approval_id, NOW).code
            except PublicationError as exc:
                return exc.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: send(*pair), zip((a, b), recorders, strict=True)))
    assert sorted(results) == sorted(
        ["ACCEPTED", "NEAR_DUPLICATE_POST" if near else "STORY_ALREADY_SUBMITTED"]
    )
    assert sum(r.creates for r in recorders) == 1
    with Ledger.open(path, create=False) as ledger:
        assert len(ledger.list_intents()) == 1


@pytest.mark.parametrize(
    "change", ["whitespace", "punctuation", "underscores", "invisible", "one-word", "footer"]
)
def test_lexical_duplicate_rules_catch_minor_edits(change):
    original = " ".join(f"word{i}" for i in range(70))
    changed = {
        "whitespace": original.replace(" ", "\n\n"),
        "underscores": original.replace(" ", "_"),
        "invisible": original.replace("word", "wo\u200brd"),
        "punctuation": original.upper().replace(" ", "! "),
        "one-word": original.replace("word20", "replacement"),
        "footer": original + " " + " ".join(f"extra{i}" for i in range(40)),
    }[change]
    assert similarity(original, changed) is not None
    assert similarity(original, "An unrelated account of an orchard and its harvest.") is None


def test_missing_or_unknown_historical_identity_fails_before_provider_access(tmp_path):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as ledger:
        unknown = candidate(story_id=None)
        ledger.add_approval(unknown, NOW)
        empty = Recorder()
        assert (
            refusal(submit, ledger, empty, unknown, unknown.approval_id, NOW)
            == E.PUBLICATION_IDENTITY_REQUIRED
        )
        ledger._db.execute(
            "INSERT INTO intents VALUES (?, 'SENDING', NULL, NULL, 'past', 'past')",
            (unknown.approval_id,),
        )
        new = candidate(text="A different post.", story_id="e" * 64)
        ledger.add_approval(new, NOW)
        assert (
            refusal(submit, ledger, empty, new, new.approval_id, NOW)
            == E.PUBLICATION_IDENTITY_REQUIRED
        )
        assert empty.destinations == empty.creates == 0


def test_typed_repost_is_exact_single_use_and_does_not_cover_changed_candidate(tmp_path):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as ledger:
        first = candidate()
        _, initial = ledger.add_reviewed_approval(subject(first), first, 0, NOW)
        provider = Recorder()
        submit(ledger, provider, first, first.approval_id, NOW)
        ledger.transition(first.approval_id, S.SENT, NOW)
        post = candidate(due_at="2026-11-04T04:00:00Z")
        matches = ledger.publication_conflicts(post.story_id, post.text, post.approval_id)
        assert (
            refusal(ledger.add_reviewed_approval, subject(post), post, initial.seq, NOW)
            == E.STORY_ALREADY_SUBMITTED
        )
        assert (
            refusal(
                ledger.add_reviewed_approval,
                subject(post),
                post,
                initial.seq,
                NOW,
                repost_typed="REPOST",
                history_sha256=fingerprint(matches),
            )
            == E.REPOST_CONFIRMATION_REQUIRED
        )
        ledger.add_reviewed_approval(
            subject(post),
            post,
            initial.seq,
            NOW,
            repost_typed=repost_phrase(post.story_id, post),
            history_sha256=fingerprint(matches),
        )
        changed = replace(post, due_at="2026-11-05T04:00:00Z")
        assert (
            refusal(submit, ledger, Recorder(), changed, post.approval_id, NOW)
            == E.APPROVAL_MISMATCH
        )
        submit(ledger, provider, post, post.approval_id, NOW)
        assert (
            refusal(submit, ledger, Recorder(), post, post.approval_id, NOW)
            == E.DUPLICATE_SUBMISSION
        )
        assert provider.creates == 2


def test_new_matching_history_invalidates_a_previously_confirmed_repost(tmp_path):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as ledger:
        first = candidate()
        ledger.add_approval(first, NOW)
        submit(ledger, Recorder(), first, first.approval_id, NOW)
        post = candidate(due_at="2026-11-04T04:00:00Z")
        matches = ledger.publication_conflicts(post.story_id, post.text, post.approval_id)
        ledger.add_reviewed_approval(
            subject(post),
            post,
            0,
            NOW,
            repost_typed=repost_phrase(post.story_id, post),
            history_sha256=fingerprint(matches),
        )
        # Imported legacy history appearing after the sheet is read must invalidate its grant.
        extra = candidate(due_at="2026-11-06T04:00:00Z")
        ledger.add_approval(extra, NOW)
        ledger._db.execute(
            "INSERT INTO intents VALUES (?, 'SENDING', NULL, NULL, 'new', 'new')",
            (extra.approval_id,),
        )
        empty = Recorder()
        assert (
            refusal(submit, ledger, empty, post, post.approval_id, NOW) == E.STORY_ALREADY_SUBMITTED
        )
        assert empty.destinations == empty.creates == 0


@pytest.mark.parametrize("state", [S.SENT, S.SCHEDULED])
@pytest.mark.parametrize("decision", ["edit", "reject"])
def test_published_status_survives_later_decisions_and_hides_ordinary_actions(
    work, state, decision
):
    desk = work.desk()
    approval, current = approve_current(desk, work.sid)
    with Ledger.open(work.ledger, create=False) as ledger:
        ledger.begin_send(approval.approval_id, NOW, approval.bundle_record_sha256)
        ledger.transition(approval.approval_id, S.ACCEPTED, NOW)
        ledger.transition(approval.approval_id, state, NOW)
    if decision == "edit":
        desk.save_edit(work.sid, texts(S01=EDITED_S01), current.seq, NOW)
    else:
        from shipyap.review.service import reject_phrase

        desk.reject(work.sid, reject_phrase(work.sid), current.seq, NOW)
    item = desk.item(work.sid)
    for html in (
        queue_page([item], []),
        story_page(item, "sample-token", StoryView(), None, scheduling=True, gate=desk.gate(item)),
    ):
        assert ("Published " if state == S.SENT else "Scheduled ") in html
        assert (
            "<button>Approve</button>" not in html
            and "<button disabled>Approve</button>" not in html
        )
        assert "<button>Schedule approved post</button>" not in html


def test_http_repost_requires_typed_confirmation_and_exact_server_ticket(work, serve):
    desk = work.desk()
    approval, current = approve_current(desk, work.sid)
    with Ledger.open(work.ledger, create=False) as ledger:
        ledger.begin_send(approval.approval_id, NOW, approval.bundle_record_sha256)
        ledger.transition(approval.approval_id, S.ACCEPTED, NOW)
        ledger.transition(approval.approval_id, S.SENT, NOW)
    client = serve(desk)
    code, _, html = client.post(
        f"/story/{work.sid}/repost-sheet",
        seq=str(current.seq),
        channel_id=CHANNEL,
        due_at="2026-11-04T04:00:00Z",
    )
    assert code == 200 and "Repost this exact post?" in html
    key = Fields(html).values["confirmation"]
    params = dict(seq=str(current.seq), confirmation=key, confirm="yes")
    assert client.post(f"/story/{work.sid}/approve", **params)[0] == 409
    assert client.post(f"/story/{work.sid}/approve", **params, typed="REPOST")[0] == 409
    post = desk.approval_candidate(work.sid, CHANNEL, "2026-11-04T04:00:00Z", NOW, repost=True)
    assert (
        client.post(f"/story/{work.sid}/approve", **params, typed=repost_phrase(work.sid, post))[0]
        == 303
    )
    item = desk.item(work.sid)
    assert item.repost_ready and item.approval.approval_id == post.approval_id
    assert (
        client.post(f"/story/{work.sid}/approve", **params, typed=repost_phrase(work.sid, post))[0]
        == 303
    )


def test_guard_runs_before_constructing_the_real_publisher(work):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    with Ledger.open(work.ledger, create=False) as ledger:
        ledger.begin_send(approval.approval_id, NOW, approval.bundle_record_sha256)
    # Existing submission: no provider factory should be touched by a repeated schedule.
    publisher = ScriptedPublisher()
    assert refusal(schedule_here, desk, work.sid, publisher) == E.DUPLICATE_SUBMISSION
    assert publisher.built == 0


def test_x_only_duplicate_is_visible_and_offers_typed_repost(work, monkeypatch):
    from test_review import x_desk

    from shipyap.review.x_post import XPost

    desk = x_desk(work)
    x_text = "An earlier short post repeated only by this X companion."
    monkeypatch.setattr(desk, "x_post", lambda _item: XPost(x_text))
    prior = candidate(text=x_text, story_id="f" * 64)
    with Ledger.open(work.ledger, create=False) as ledger:
        ledger.add_approval(prior, NOW)
        submit(ledger, Recorder(), prior, prior.approval_id, NOW)
    item = desk.item(work.sid)
    assert similarity(item.current.text, x_text) is None
    assert [p.approval_id for p in item.duplicate_posts] == [prior.approval_id]
    html = story_page(item, "sample-token", StoryView(), None, gate=desk.gate(item))
    assert "Review repost" in html and x_text in html and "<button>Approve</button>" not in html
    assert refusal(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == E.NEAR_DUPLICATE_POST


def test_unicode_punctuation_duplicate_of_another_story_is_blocked_before_provider(tmp_path):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as ledger:
        first = candidate()
        ledger.add_approval(first, NOW)
        submit(ledger, Recorder(), first, first.approval_id, NOW)
        edited = candidate(text=POST_TEXT.replace(" ", "_"), story_id="f" * 64)
        ledger.add_approval(edited, NOW)
        empty = Recorder()
        assert (
            refusal(submit, ledger, empty, edited, edited.approval_id, NOW) == E.NEAR_DUPLICATE_POST
        )
        assert empty.destinations == empty.creates == 0


def test_typed_repost_keeps_linkedin_and_x_in_one_new_cycle(work):
    from test_review import approve_both, x_desk

    from shipyap.review.service import x_digest

    desk = x_desk(work)
    first, _ = approve_both(desk, work.sid)
    recorder = Recorder()
    schedule_here(desk, work.sid, lambda: recorder)
    assert recorder.creates == 2
    post = desk.approval_candidate(work.sid, CHANNEL, "2026-11-04T04:00:00Z", NOW, repost=True)
    item = desk.item(work.sid)
    matches = desk.repost_history(item, post)
    desk.approve(
        work.sid,
        post.channel_id,
        post.due_at,
        confirmation_phrase(post.approval_id),
        item.seq,
        NOW,
        x_digest(desk.x_sheet(work.sid, post)),
        repost_typed=repost_phrase(work.sid, post),
        history_sha256=fingerprint(matches),
    )
    schedule_here(desk, work.sid, lambda: recorder)
    assert recorder.creates == 4
    with Ledger.open(work.ledger, create=False) as ledger:
        assert {p.cycle_id for p in ledger.publication_posts(work.sid)} == {
            first.approval_id,
            post.approval_id,
        }


def test_stop_refuses_the_repost_sheet_before_any_decision(work, serve):
    client = serve(work.desk(), stopped=lambda: True)
    code, _, html = client.post(
        f"/story/{work.sid}/repost-sheet", seq="0", channel_id=CHANNEL, due_at=DUE
    )
    assert code == 423 and "STOPPED" in html
    assert work.desk().item(work.sid).decisions == ()


def test_publication_replay_pins_exact_send_counts_and_missing_cases():
    root = Path(__file__).resolve().parents[1] / "evals/public"
    reports = publication_replay.load_replays(root)
    pins = json.loads((root / "expected.json").read_text("utf-8"))["publications"]
    assert reports[0].recorded_sends == 3 and reports[0].create_calls == 1
    assert publication_replay.failures(reports, pins) == []
    assert publication_replay.failures([], pins)
    assert publication_replay.failures(reports, {})


def legacy_database(path):
    """Synthetic old ledger: incident shape plus two intentional first LI/X pairs."""
    from shipyap.publication.ledger import _SCHEMA_V1, _SCHEMA_V2, _approval_row

    root = Path(__file__).resolve().parents[1] / "evals/public"
    case = json.loads((root / "publications/repeated-story.history.json").read_text("utf-8"))
    db = sqlite3.connect(path, isolation_level=None)
    for sql in (*_SCHEMA_V1, *_SCHEMA_V2):
        db.execute(sql)
    db.execute("PRAGMA user_version=2")
    for approval in case["approvals"]:
        db.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(approval.values())
        )
    for event in case["events"]:
        if event["kind"] == "review":
            db.execute(
                "INSERT INTO review_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(event["row"].values()),
            )
        elif event["kind"] == "submit":
            db.execute(
                "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event["approval_id"],
                    event["recorded_final_state"],
                    event["recorded_post_id"],
                    "RECORDED",
                    event["at"],
                    event["recorded_updated_at"],
                ),
            )
    for index, story in enumerate(("d" * 64, "e" * 64)):
        lead = candidate(
            story_id=story,
            text=f"Distinct intentional first send {index}.",
            due_at=f"2026-11-{11 + index * 7:02d}T04:00:00Z",
            bundle_record_sha256=str(index + 3) * 64,
        )
        follow = replace(lead, text=f"Distinct X first send {index}.", channel_id="f1" * 12)
        for post in (lead, follow):
            db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _approval_row(post, NOW)
            )
            db.execute(
                "INSERT INTO intents VALUES (?, 'ACCEPTED', ?, 'ACCEPTED', ?, ?)",
                (post.approval_id, "9" * 24, "2026-11-06T20:32:19Z", "2026-11-06T20:32:19Z"),
            )
        db.execute(
            "INSERT INTO review_decisions VALUES (?, ?, 'APPROVED', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                12 + index * 3,
                story,
                lead.approval_id,
                lead.text,
                _approval_row(lead, NOW)[2],
                "sample-draft",
                lead.bundle_record_sha256,
                "c" * 64,
                "d" * 64,
                "2026-11-04T16:27:29Z",
            ),
        )
    db.close()
    return case


def test_explicit_migration_preview_preserves_history_and_intentional_first_pairs(tmp_path):
    import hashlib

    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    before = path.read_bytes()
    assert refusal(Ledger.open, path, create=True) == E.LEDGER_MIGRATION_REQUIRED
    report = preview(path)
    assert path.read_bytes() == before
    assert report["unresolved_approvals"] == [] and report["intents"] == 7
    assert sum(p["classification"] == "legacy_duplicate" for p in report["posts"]) == 2
    intentional = [p for p in report["posts"] if p["story_id"] in {"d" * 64, "e" * 64}]
    assert (
        sorted(p["classification"] for p in intentional)
        == ["first_cycle_follow_on"] * 2 + ["first_send"] * 2
    )
    source = sqlite3.connect(path)
    original_rows = {
        t: source.execute("SELECT * FROM " + t).fetchall()
        for t in ("approvals", "intents", "review_decisions")
    }
    source.close()
    migrate(path)
    source = sqlite3.connect(path)
    assert all(
        source.execute("SELECT * FROM " + t).fetchall() == rows for t, rows in original_rows.items()
    )
    source.close()
    assert hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(before).digest()
    assert migrate(path)["posts"] == report["posts"]


def test_migration_never_hides_an_unresolved_historical_send(tmp_path):
    from shipyap.publication.ledger import _approval_row

    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    unknown = candidate(
        story_id=None, text="Unmapped historical post.", bundle_record_sha256="8" * 64
    )
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _approval_row(unknown, NOW))
    db.execute(
        "INSERT INTO intents VALUES (?, 'SENDING', NULL, NULL, 'past', 'past')",
        (unknown.approval_id,),
    )
    db.close()
    report = migrate(path)
    assert report["unresolved_approvals"] == [unknown.approval_id]
    assert (
        next(p for p in report["posts"] if p["approval_id"] == unknown.approval_id)[
            "classification"
        ]
        == "unresolved"
    )
    with Ledger.open(path, create=False) as ledger:
        post = candidate(text="A new unrelated story.", story_id="7" * 64)
        ledger.add_approval(post, NOW)
        assert (
            refusal(submit, ledger, Recorder(), post, post.approval_id, NOW)
            == E.PUBLICATION_IDENTITY_REQUIRED
        )


def test_publication_history_tables_are_append_only_and_schema_checked(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Ledger.open(path, create=True) as ledger:
        post = candidate()
        ledger.add_approval(post, NOW)
        submit(ledger, Recorder(), post, post.approval_id, NOW)
        for table in ("publication_bindings", "publication_reservations"):
            for statement in ("DELETE FROM ", "UPDATE "):
                sql = (
                    statement
                    + table
                    + (" SET approval_id=approval_id" if statement == "UPDATE " else "")
                )
                with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                    ledger._db.execute(sql)
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER publication_bindings_no_delete")
    db.close()
    assert refusal(Ledger.open, path, create=False) == E.LEDGER_UNUSABLE


def test_eval_command_fails_when_the_pinned_publication_history_is_missing(tmp_path, capsys):
    import shutil

    from shipyap.automation.cli import main

    root = Path(__file__).resolve().parents[1] / "evals/public"
    target = tmp_path / "cases"
    shutil.copytree(root, target)
    (target / "publications/repeated-story.history.json").unlink()
    assert main(["eval", "--cases", str(target), "--check"]) == 1
    assert (
        "GATE FAIL PUBLICATION repeated-story.history: missing replay or pin"
        in capsys.readouterr().out
    )


def test_migration_command_defaults_to_a_read_only_source_preview(tmp_path, capsys):
    from shipyap.automation.cli import main

    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    before = path.read_bytes()
    assert main(["publication", "migrate", "--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "disposable-copy"
    assert path.read_bytes() == before


def test_invalid_backfill_link_rolls_back_the_entire_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    drafts = tmp_path / "drafts"
    link = drafts / ("f" * 64 + "-link")
    link.mkdir(parents=True)
    (link / "story-link.json").write_text(
        json.dumps(dict(story_id="f" * 64, draft_bundle="../escape")), "utf-8"
    )
    before = path.read_bytes()
    assert refusal(migrate, path, drafts) == E.PUBLICATION_IDENTITY_REQUIRED
    assert path.read_bytes() == before
