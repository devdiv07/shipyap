import json
import os
from pathlib import Path

import pytest
from github_fake import FakeGithub, request_bytes

from public_research_os.evidence.manifest import SourceManifest
from public_research_os.evidence.report import canonical_json
from public_research_os.evidence.strict_json import loads_strict
from public_research_os.ingestion.assemble import (
    MANIFEST_NAME,
    RECORD_NAME,
    SNAPSHOT_DIR,
    SnapshotPlan,
    build_plan,
)
from public_research_os.ingestion.errors import IngestErrorCode, IngestionError
from public_research_os.ingestion.github import GithubReader
from public_research_os.ingestion.request import parse_request
from public_research_os.ingestion.writer import (
    preflight_destination,
    publish_snapshot,
    verify_completion,
)

FILES = {
    "README.md": b"# Synthetic readme\n",
    "docs/notes.md": b"synthetic notes\n",
    "docs/deep/data.json": b'{"passed": 3, "total": 10}\n',
}
PATHS = ["README.md", "docs/notes.md", "docs/deep/data.json"]
EXPECTED_TREE = {
    "snapshot",
    "snapshot/README.md",
    "snapshot/docs",
    "snapshot/docs/notes.md",
    "snapshot/docs/deep",
    "snapshot/docs/deep/data.json",
    MANIFEST_NAME,
    RECORD_NAME,
}


def make_plan(paths=PATHS) -> SnapshotPlan:
    request = parse_request(request_bytes(paths))
    return build_plan(request, GithubReader(FakeGithub(FILES)).fetch(request))


def tree(path: Path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*")}


def make_dir_link(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed) or a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def code_of(call, *args) -> IngestErrorCode:
    with pytest.raises(IngestionError) as caught:
        call(*args)
    assert caught.value.__cause__ is None
    return caught.value.code


@pytest.fixture
def parent(tmp_path) -> Path:
    folder = tmp_path / "parent"
    folder.mkdir()
    return folder


@pytest.fixture
def published(parent) -> Path:
    out = parent / "snap"
    publish_snapshot(out, make_plan())
    return out


# --- assembly: deterministic, existing v1 contract --------------------------------------------


def test_plan_is_deterministic_sorted_and_free_of_machine_data():
    first, second = make_plan(PATHS), make_plan(list(reversed(PATHS)))
    assert first == second  # request order does not matter
    assert [name for name, _ in first.entries] == [
        "snapshot/README.md",
        "snapshot/docs/deep/data.json",
        "snapshot/docs/notes.md",
        MANIFEST_NAME,
        RECORD_NAME,  # the completion record is always last
    ]
    entries = dict(first.entries)
    manifest = SourceManifest.model_validate(loads_strict(entries[MANIFEST_NAME]))
    assert entries[MANIFEST_NAME].decode() == canonical_json(manifest.model_dump(mode="json"))
    assert [f.path for f in manifest.files] == sorted(f.path for f in manifest.files)
    record = json.loads(entries[RECORD_NAME])
    assert record["status"] == "COMPLETE" and record["record_version"] == "1"
    blob = (entries[MANIFEST_NAME] + entries[RECORD_NAME]).decode()
    assert "tmp" not in blob.lower() and "\\" not in blob and "T00:" not in blob  # no paths/times


def test_a_source_url_that_would_not_round_trip_is_refused_before_it_reaches_a_manifest(
    monkeypatch,
):
    request = parse_request(request_bytes(["README.md"]))
    fetched = GithubReader(FakeGithub(FILES)).fetch(request)
    # Pydantic lowercases the host; the checker compares normalized strings, so this would
    # produce a manifest that no claim could ever match.
    monkeypatch.setattr(
        type(request), "canonical_url", property(lambda self: "https://GitHub.com/o/r")
    )
    assert code_of(build_plan, request, fetched) == IngestErrorCode.MANIFEST_INVALID


def test_plan_rejects_a_snapshot_that_does_not_match_the_request():
    request = parse_request(request_bytes(["README.md"]))
    fetched = GithubReader(FakeGithub(FILES)).fetch(parse_request(request_bytes(["docs/notes.md"])))
    assert code_of(build_plan, request, fetched) == IngestErrorCode.MANIFEST_INVALID


# --- publishing -------------------------------------------------------------------------------


def test_publish_creates_exactly_the_planned_tree_and_the_record_last(parent, monkeypatch):
    created: list[str] = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            created.append(Path(path).name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    out = parent / "snap"
    publish_snapshot(out, make_plan())
    monkeypatch.undo()

    assert tree(parent) == {"snap"} | {f"snap/{name}" for name in EXPECTED_TREE}
    assert created[-1] == RECORD_NAME and created[-2] == MANIFEST_NAME
    for name, data in make_plan().entries:
        assert (out / name).read_bytes() == data
    verify_completion(out)


@pytest.mark.parametrize("kind", ["file", "directory", "empty-directory", "link"])
def test_preexisting_destination_is_refused_before_anything_is_written(parent, tmp_path, kind):
    out = parent / "snap"
    if kind == "file":
        out.write_bytes(b"precious")
    elif kind == "link":
        target = tmp_path / "elsewhere"
        target.mkdir()
        make_dir_link(out, target)
    else:
        out.mkdir()
        if kind == "directory":
            (out / "keep.txt").write_bytes(b"precious")
    before = tree(tmp_path)
    assert code_of(preflight_destination, out) == IngestErrorCode.OUTPUT_EXISTS
    assert code_of(publish_snapshot, out, make_plan()) == IngestErrorCode.OUTPUT_EXISTS
    assert tree(tmp_path) == before
    if kind == "file":
        assert out.read_bytes() == b"precious"
    if kind == "directory":
        assert (out / "keep.txt").read_bytes() == b"precious"


def test_parent_must_be_an_existing_real_directory(tmp_path, parent):
    (tmp_path / "a-file").write_bytes(b"x")
    real = tmp_path / "real"
    real.mkdir()
    linked_parent = tmp_path / "linked"
    make_dir_link(linked_parent, real)
    for bad in (
        tmp_path / "missing" / "snap",
        tmp_path / "a-file" / "snap",
        linked_parent / "snap",
        parent / "..",
        Path("."),
    ):
        assert code_of(preflight_destination, bad) == IngestErrorCode.OUTPUT_PARENT_INVALID
    assert tree(real) == set()


STAGES = {
    "mkdir-subdir": ("mkdir", 4, IngestErrorCode.OUTPUT_WRITE_FAILED),
    "create-second-file": ("open", 2, IngestErrorCode.OUTPUT_WRITE_FAILED),
    "write": ("write", 2, IngestErrorCode.OUTPUT_WRITE_FAILED),
    "fsync": ("fsync", 3, IngestErrorCode.OUTPUT_WRITE_FAILED),
    "close": ("close", 2, IngestErrorCode.OUTPUT_WRITE_FAILED),
    "silent-corruption": ("corrupt", 1, IngestErrorCode.OUTPUT_VERIFY_FAILED),
    "rename": ("rename", 1, IngestErrorCode.OUTPUT_PUBLISH_FAILED),
}


@pytest.mark.parametrize("stage", list(STAGES))
def test_a_failure_at_any_stage_leaves_nothing_under_the_final_name(parent, monkeypatch, stage):
    function, nth, expected = STAGES[stage]
    calls = {"count": 0}

    def trips() -> bool:
        calls["count"] += 1
        return calls["count"] == nth

    real = {name: getattr(os, name) for name in ("mkdir", "open", "write", "fsync", "close")}

    def failing_mkdir(*args, **kwargs):
        if trips():
            raise OSError("injected SENTINEL")
        return real["mkdir"](*args, **kwargs)

    def failing_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and trips():
            raise OSError("injected SENTINEL")
        return real["open"](path, flags, *args, **kwargs)

    def failing_write(descriptor, data):
        if trips():
            raise OSError("injected SENTINEL")
        return real["write"](descriptor, data)

    def corrupting_write(descriptor, data):
        if trips():  # the bytes reach the disk altered, and the write "succeeds"
            return real["write"](descriptor, b"X" + bytes(data)[1:])
        return real["write"](descriptor, data)

    def failing_fsync(descriptor):
        if trips():
            raise OSError("injected SENTINEL")
        return real["fsync"](descriptor)

    def failing_close(descriptor):
        real["close"](descriptor)
        if trips():
            raise OSError("injected SENTINEL")

    def failing_rename(*args, **kwargs):
        raise PermissionError("injected SENTINEL")

    patches = {
        "mkdir": failing_mkdir,
        "open": failing_open,
        "write": failing_write,
        "corrupt": corrupting_write,
        "fsync": failing_fsync,
        "close": failing_close,
        "rename": failing_rename,
    }
    target = "write" if function == "corrupt" else function
    monkeypatch.setattr(os, target, patches[function])

    out = parent / "snap"
    with pytest.raises(IngestionError) as caught:
        publish_snapshot(out, make_plan())
    monkeypatch.undo()
    assert caught.value.code == expected
    assert "SENTINEL" not in str(caught.value) and caught.value.__cause__ is None
    assert list(parent.iterdir()) == []  # no final directory, and the staging area is gone


def test_a_destination_that_appears_before_publishing_is_never_replaced(parent, monkeypatch):
    """An EMPTY directory is the case that matters: Windows refuses to rename onto it, but POSIX
    rename(2) would silently replace it, so only the explicit recheck stops that."""
    out = parent / "snap"
    real_open = os.open

    def open_then_plant(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(path).name == RECORD_NAME:
            out.mkdir()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_then_plant)
    assert code_of(publish_snapshot, out, make_plan()) == IngestErrorCode.OUTPUT_PUBLISH_FAILED
    monkeypatch.undo()
    assert tree(parent) == {"snap"}  # the planted directory is still there, still empty


def test_a_destination_with_content_that_appears_before_publishing_is_untouched(
    parent, monkeypatch
):
    out = parent / "snap"
    real_open = os.open

    def open_then_plant(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(path).name == RECORD_NAME:
            out.mkdir()
            (out / "keep.txt").write_bytes(b"precious")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_then_plant)
    assert code_of(publish_snapshot, out, make_plan()) == IngestErrorCode.OUTPUT_PUBLISH_FAILED
    monkeypatch.undo()
    assert tree(parent) == {"snap", "snap/keep.txt"}
    assert (out / "keep.txt").read_bytes() == b"precious"


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(
            (("snapshot/Docs/a.json", b"1"), ("snapshot/docs/b.json", b"2")), id="case-collision"
        ),
        pytest.param((("snapshot/../escape.txt", b"1"),), id="dotdot"),
        pytest.param((("snapshot/a:stream", b"1"),), id="stream-name"),
        pytest.param((("/absolute.txt", b"1"),), id="absolute"),
        pytest.param((("snapshot/CON", b"1"),), id="reserved-name"),
    ],
)
def test_writer_refuses_unsafe_names_even_from_a_hand_built_plan(parent, entries):
    plan = SnapshotPlan(
        entries=entries, manifest_sha256="0" * 64, commit_sha="1" * 40, file_count=1
    )
    assert code_of(publish_snapshot, parent / "snap", plan) == IngestErrorCode.OUTPUT_WRITE_FAILED
    assert list(parent.iterdir()) == []
    assert not (parent.parent / "escape.txt").exists()


# --- the completion rule ----------------------------------------------------------------------


def test_a_published_snapshot_satisfies_the_completion_rule(published):
    verify_completion(published)


def _edit_record(out: Path, edit) -> None:
    record = json.loads((out / RECORD_NAME).read_text(encoding="utf-8"))
    edit(record)
    (out / RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")


def _remove(path: str):
    return lambda out: (out / path).unlink()


def _overwrite(path: str, data: bytes):
    return lambda out: (out / path).write_bytes(data)


def _extra_file(out: Path) -> None:
    (out / SNAPSHOT_DIR / "extra.txt").write_bytes(b"x")


def _extra_top_level(out: Path) -> None:
    (out / "notes.txt").write_bytes(b"x")


def _extra_directory(out: Path) -> None:
    (out / SNAPSHOT_DIR / "empty-dir").mkdir()


def _link_in_snapshot(out: Path) -> None:
    target = out.parent / "link-target"
    target.mkdir()
    make_dir_link(out / SNAPSHOT_DIR / "linked", target)


def _link_as_snapshot_dir(out: Path) -> None:
    (out / SNAPSHOT_DIR / "README.md").unlink()
    for child in sorted(out.glob(f"{SNAPSHOT_DIR}/**/*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    (out / SNAPSHOT_DIR).rmdir()
    target = out.parent / "link-target"
    target.mkdir()
    make_dir_link(out / SNAPSHOT_DIR, target)


def _reformat_manifest(out: Path) -> None:
    """Semantically identical JSON, different bytes: the recorded digest must notice."""
    path = out / MANIFEST_NAME
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")


def _directory_replaced_by_link(out: Path) -> None:
    """The content is all still reachable, but through a link rather than a real directory."""
    moved = out.parent / "moved-docs"
    (out / SNAPSHOT_DIR / "docs").rename(moved)
    make_dir_link(out / SNAPSHOT_DIR / "docs", moved)


MUTATIONS = {
    "record-missing": _remove(RECORD_NAME),
    "manifest-reformatted": _reformat_manifest,
    "directory-replaced-by-link": _directory_replaced_by_link,
    "manifest-missing": _remove(MANIFEST_NAME),
    "file-missing": _remove("snapshot/README.md"),
    "file-changed-same-size": _overwrite("snapshot/README.md", b"# Synthetic readmf\n"),
    "file-truncated": _overwrite("snapshot/README.md", b"# Synth"),
    "file-extended": _overwrite("snapshot/README.md", b"# Synthetic readme\nmore\n"),
    "extra-file-in-snapshot": _extra_file,
    "extra-top-level-file": _extra_top_level,
    "extra-empty-directory": _extra_directory,
    "manifest-replaced": _overwrite(MANIFEST_NAME, b"{}"),
    "manifest-not-json": _overwrite(MANIFEST_NAME, b"\xff"),
    "record-not-json": _overwrite(RECORD_NAME, b"not json"),
    "record-status": lambda out: _edit_record(out, lambda r: r.update(status="PARTIAL")),
    "record-notice-removed": lambda out: _edit_record(out, lambda r: r.update(notices=[])),
    "record-digest": lambda out: _edit_record(out, lambda r: r["files"][0].update(sha256="0" * 64)),
    "record-path-escape": lambda out: _edit_record(
        out, lambda r: r["files"][0].update(path="../outside.txt")
    ),
    "record-workspace": lambda out: _edit_record(out, lambda r: r.update(workspace_id="other_ws")),
    "link-inside-snapshot": _link_in_snapshot,
    "link-as-snapshot-directory": _link_as_snapshot_dir,
}


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_any_inconsistency_breaks_the_completion_rule(published, mutation):
    MUTATIONS[mutation](published)
    assert code_of(verify_completion, published) == IngestErrorCode.INCOMPLETE_SNAPSHOT


def test_completion_rule_rejects_a_missing_or_non_directory_target(tmp_path, published):
    for target in (tmp_path / "missing", published / RECORD_NAME):
        assert code_of(verify_completion, target) == IngestErrorCode.INCOMPLETE_SNAPSHOT
    linked = tmp_path / "linked"
    make_dir_link(linked, published)
    assert code_of(verify_completion, linked) == IngestErrorCode.INCOMPLETE_SNAPSHOT
