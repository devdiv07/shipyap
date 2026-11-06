"""Bundle output: published only through the Assignment 02 staging writer, and checked by the
bundle completion rule. A failed publish leaves nothing; a modified bundle is not accepted."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from public_research_os.editorial.bundle import BundleIncomplete, publish_bundle, verify_bundle
from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.models import RECORD_FILE, BundleKind, BundleRecord

E = EditorialErrorCode
FIELDS = {
    "workspace_id": "demo_workspace",
    "snapshot_manifest_sha256": "e" * 64,
    "brief_sha256": "f" * 64,
}
FILES = {"brief.json": b'{"synthetic": true}\n', "brief.md": "# Synthetic brief — x\n".encode()}


@pytest.fixture
def parent(tmp_path) -> Path:
    folder = tmp_path / "parent"
    folder.mkdir()
    return folder


def publish(out: Path) -> BundleRecord:
    return publish_bundle(out, BundleKind.BRIEF, dict(FILES), **FIELDS)


def test_a_published_bundle_verifies_and_is_deterministic(parent):
    record = publish(parent / "one")
    publish(parent / "two")
    verified = verify_bundle(parent / "one", BundleKind.BRIEF)
    assert verified.record == record and verified.files == FILES
    assert record.review_state == "UNAPPROVED" and record.status == "COMPLETE"
    for name in (*FILES, RECORD_FILE):
        assert (parent / "one" / name).read_bytes() == (parent / "two" / name).read_bytes()
    assert sorted(p.name for p in parent.iterdir()) == ["one", "two"]  # no staging left behind


def test_the_record_is_written_last(parent, monkeypatch):
    created: list[str] = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(Path(path).name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    publish(parent / "out")
    monkeypatch.undo()
    assert created == ["brief.json", "brief.md", RECORD_FILE]


def _tamper(bundle: Path, damage: str) -> None:
    if damage == "extra-file":
        (bundle / "notes.txt").write_bytes(b"planted")
    elif damage == "missing-file":
        (bundle / "brief.md").unlink()
    elif damage == "byte-changed":
        (bundle / "brief.json").write_bytes(b'{"synthetic": false}\n')
    elif damage == "subdirectory":
        (bundle / "nested").mkdir()
    elif damage == "record-edited":
        record = json.loads((bundle / RECORD_FILE).read_bytes())
        record["review_state"] = "APPROVED"
        (bundle / RECORD_FILE).write_bytes(json.dumps(record).encode())


@pytest.mark.parametrize(
    "damage", ["extra-file", "missing-file", "byte-changed", "subdirectory", "record-edited"]
)
def test_a_modified_bundle_is_not_complete(parent, damage):
    publish(parent / "out")
    _tamper(parent / "out", damage)
    with pytest.raises(BundleIncomplete):
        verify_bundle(parent / "out", BundleKind.BRIEF)


def test_a_bundle_of_another_kind_or_a_missing_directory_is_not_complete(parent):
    publish(parent / "out")
    for directory, kind in (
        (parent / "out", BundleKind.DRAFT),
        (parent / "none", BundleKind.BRIEF),
    ):
        with pytest.raises(BundleIncomplete):
            verify_bundle(directory, kind)


STAGES = {
    "create": ("open", E.OUTPUT_WRITE_FAILED),
    "corrupt": ("write", E.OUTPUT_VERIFY_FAILED),
    "rename": ("rename", E.OUTPUT_PUBLISH_FAILED),
}


@pytest.mark.parametrize("stage", list(STAGES))
def test_a_failure_at_any_publish_stage_leaves_nothing_under_the_final_name(
    parent, monkeypatch, stage
):
    function, expected = STAGES[stage]
    real = getattr(os, function)
    calls = {"n": 0}

    def failing(*args, **kwargs):
        calls["n"] += 1
        if stage == "create" and args[1] & os.O_CREAT and calls["n"] >= 2:
            raise OSError("injected SENTINEL")
        if stage == "corrupt" and calls["n"] == 1:
            return real(args[0], b"X" + bytes(args[1])[1:])  # bytes change, write "succeeds"
        if stage == "rename":
            raise PermissionError("injected SENTINEL")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, function, failing)
    with pytest.raises(EditorialError) as caught:
        publish(parent / "out")
    monkeypatch.undo()
    assert caught.value.code == expected
    assert "SENTINEL" not in str(caught.value) and caught.value.__cause__ is None
    assert list(parent.iterdir()) == []


def test_an_existing_destination_or_unusable_parent_is_refused(parent, tmp_path):
    (parent / "out").mkdir()
    for out, expected in (
        (parent / "out", E.OUTPUT_EXISTS),
        (tmp_path / "missing" / "out", E.OUTPUT_PARENT_INVALID),
    ):
        with pytest.raises(EditorialError) as caught:
            publish(out)
        assert caught.value.code == expected
    assert list((parent / "out").iterdir()) == []


def test_a_record_cannot_express_approval_or_mix_bundle_kinds():
    good = BundleRecord(kind=BundleKind.BRIEF, files=publish_files(), **FIELDS).model_dump()
    for change in (
        {"review_state": "APPROVED"},
        {"semantic_support": "SUPPORTED"},
        {"status": "PARTIAL"},
        {"kind": "draft"},  # a draft record needs the draft files and fields
        {"draft_status": "READY_FOR_REVIEW"},  # a brief carries no draft fields
    ):
        with pytest.raises(ValidationError):
            BundleRecord.model_validate({**good, **change})


def publish_files():
    return tuple(
        {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in FILES.items()
    )
