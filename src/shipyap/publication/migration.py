"""Explicit offline upgrade. Preview uses SQLite backup from a read-only source.

Opening a review ledger never opts an existing workspace into this upgrade. The caller must
separately authorize apply; preview changes only a disposable copy and never reads credentials.
"""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from shipyap.publication.approval import approval_id, text_sha256
from shipyap.publication.errors import PublicationError
from shipyap.publication.errors import PublicationErrorCode as E
from shipyap.publication.history import History
from shipyap.publication.ledger import (
    _EXPECTED_OBJECTS,
    _SCHEMA_V2,
    _V1_OBJECTS,
    _V2_OBJECTS,
    HISTORY_SCHEMA,
    SCHEMA_VERSION,
    _application_objects,
)


def copy_database(source: Path, destination: Path) -> None:
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _link_bindings(drafts: Path | None) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if drafts is None:
        return found
    from shipyap.editorial.bundle import BundleIncomplete, verify_bundle
    from shipyap.editorial.models import BundleKind

    for path in sorted(drafts.glob("*-link/story-link.json")):
        link = json.loads(path.read_text("utf-8"))
        story = link["story_id"]
        if path.parent.name != story + "-link":
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        name = link["draft_bundle"]
        if Path(name).name != name or "/" in name or "\\" in name:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        try:
            bundle = verify_bundle(drafts / name, BundleKind.DRAFT)
        except BundleIncomplete:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED) from None
        if bundle.record_sha256 != link["draft_record_sha256"]:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        found.setdefault(bundle.record_sha256, set()).add(story)
    return found


def migrate(path: Path, drafts: Path | None = None) -> dict:
    """Apply to an explicitly selected database. Historical approval/intent rows stay exact."""
    if not path.is_file():
        raise PublicationError(E.LEDGER_UNUSABLE)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        objects = _application_objects(db)
        if version == SCHEMA_VERSION and objects == _EXPECTED_OBJECTS:
            report = _report(db)
            db.execute("COMMIT")
            return report
        if (version, objects) not in ((1, _V1_OBJECTS), (2, _V2_OBJECTS)):
            raise PublicationError(E.LEDGER_UNUSABLE)
        if version == 1:
            for statement in _SCHEMA_V2:
                db.execute(statement)
        for statement in HISTORY_SCHEMA:
            db.execute(statement)
        rows = db.execute("SELECT * FROM approvals").fetchall()
        bundles = _link_bindings(drafts)
        for story, bundle in db.execute(
            "SELECT story_id, draft_record_sha256 FROM review_decisions"
        ):
            bundles.setdefault(bundle, set()).add(story)
        direct: dict[str, set[str]] = {}
        for story, aid in db.execute(
            "SELECT story_id, approval_id FROM review_decisions WHERE approval_id IS NOT NULL"
        ):
            direct.setdefault(aid, set()).add(story)
        leads = {
            r[0]
            for r in db.execute(
                "SELECT approval_id FROM review_decisions WHERE approval_id IS NOT NULL"
            )
        }
        history = History(db)
        unresolved = []
        for row in rows:
            aid, text, digest, channel, due, workspace, bundle, approved = row
            if text_sha256(text) != digest or approval_id(text, channel, due) != aid:
                raise PublicationError(E.LEDGER_UNUSABLE)
            stories = direct.get(aid) or bundles.get(bundle, set())
            if len(stories) != 1:
                unresolved.append(aid)
                continue
            story = next(iter(stories))
            # Only an unambiguous, simultaneously approved companion of a reviewed lead can
            # share its cycle. Channel IDs alone are not trusted to identify a platform.
            peers = [
                r[0]
                for r in rows
                if r[0] in leads
                and r[0] != aid
                and r[3] != channel
                and r[4] == due
                and r[5] == workspace
                and r[6] == bundle
                and r[7] == approved
                and direct.get(r[0]) == {story}
            ]
            if aid not in leads and len(peers) != 1:
                unresolved.append(aid)
                continue
            history.bind(aid, story, aid if aid in leads else peers[0])
        # Keep unresolved history explicit; scheduling fails closed until an operator resolves
        # it. Never silently manufacture a story or infer identity from text similarity.
        for aid, created in db.execute("SELECT approval_id, created_at FROM intents").fetchall():
            if history.binding(aid) is not None:
                history.reserve(aid, created, "legacy")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        report = _report(db)
        report["unresolved_approvals"] = sorted(unresolved)
        db.execute("COMMIT")
        return report
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _report(db: sqlite3.Connection) -> dict:
    posts = db.execute(
        "SELECT b.story_id, b.cycle_id, b.role, a.approval_id, a.due_at, i.state, "
        "i.provider_post_id, i.created_at FROM intents i JOIN approvals a USING(approval_id) "
        "LEFT JOIN publication_bindings b USING(approval_id) ORDER BY i.created_at, a.approval_id"
    ).fetchall()
    first = {}
    records = []
    for story, cycle, role, aid, due, state, post_id, created in posts:
        if story is not None:
            first.setdefault(story, cycle)
        classification = (
            "unresolved"
            if story is None
            else "legacy_duplicate"
            if cycle != first[story]
            else "first_send"
            if role == "lead"
            else "first_cycle_follow_on"
        )
        records.append(
            dict(
                story_id=story,
                cycle_id=cycle,
                approval_id=aid,
                target=due,
                state=state,
                post_id=post_id,
                submitted_at=created,
                classification=classification,
            )
        )
    unresolved = [
        r[0]
        for r in db.execute(
            "SELECT approval_id FROM approvals LEFT JOIN publication_bindings USING(approval_id) WHERE story_id IS NULL"
        )
    ]
    return dict(
        schema=SCHEMA_VERSION,
        approvals=db.execute("SELECT count(*) FROM approvals").fetchone()[0],
        intents=len(posts),
        bindings=db.execute("SELECT count(*) FROM publication_bindings").fetchone()[0],
        unresolved_approvals=sorted(unresolved),
        posts=records,
    )


def preview(path: Path, drafts: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="pros-history-") as temporary:
        target = Path(temporary) / "ledger.sqlite"
        copy_database(path, target)
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        report = migrate(target, drafts)
        return {"mode": "disposable-copy", "source_copy_sha256": before, **report}
