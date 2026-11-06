import json

import pytest
from github_fake import OWNER, REPO, REVISION, WORKSPACE, request_bytes, request_document

from public_research_os.evidence.manifest import SourceManifest
from public_research_os.evidence.models import Claim
from public_research_os.evidence.report import ErrorCode
from public_research_os.evidence.validation import UnreadableInput
from public_research_os.ingestion.errors import IngestErrorCode, IngestionError
from public_research_os.ingestion.request import (
    IngestionRequest,
    RepoRef,
    parse_allow_repo,
    parse_request,
    require_allowed,
)

SENTINEL = "SENTINEL-secret-value"


def code_of(data: bytes | UnreadableInput) -> IngestErrorCode:
    with pytest.raises(IngestionError) as caught:
        parse_request(data)
    assert SENTINEL not in str(caught.value)
    return caught.value.code


def test_valid_request_and_canonical_source_identity():
    request = parse_request(request_bytes(["docs/a.json", "README.md"]))
    assert request.canonical_url == f"https://github.com/{OWNER}/{REPO}"
    assert request.revision_sha == REVISION
    assert request.workspace_id == WORKSPACE
    assert request.paths == ("docs/a.json", "README.md")


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"url": "https://example.org/x"}, id="extra-url-field"),
        pytest.param({"request_version": "2"}, id="version"),
        pytest.param({"workspace_id": "Not Valid"}, id="workspace"),
        pytest.param({"revision_sha": REVISION.upper()}, id="sha-uppercase"),
        pytest.param({"revision_sha": REVISION[:39]}, id="sha-short"),
        pytest.param({"revision_sha": "main"}, id="sha-branch-name"),
        pytest.param({"revision_sha": REVISION + "\n"}, id="sha-newline"),
        pytest.param({"owner": "-bad"}, id="owner-leading-hyphen"),
        pytest.param({"owner": "a" * 40}, id="owner-too-long"),
        pytest.param({"owner": "own/er"}, id="owner-slash"),
        pytest.param({"repo": "repo.git"}, id="repo-dot-git"),
        pytest.param({"repo": ".."}, id="repo-dotdot"),
        pytest.param({"repo": "re po"}, id="repo-space"),
        pytest.param({"owner": 7}, id="owner-not-a-string"),
    ],
)
def test_malformed_request_is_rejected(override):
    document = request_document(["README.md"], **override)
    assert code_of(json.dumps(document).encode()) == IngestErrorCode.REQUEST_INVALID


def test_rejected_values_are_never_echoed():
    for field, value in (
        ("owner", f"{SENTINEL}/x"),  # SENTINEL alone is a syntactically valid owner name
        ("workspace_id", SENTINEL),
        ("revision_sha", SENTINEL),
        ("request_version", SENTINEL),
        (SENTINEL, "x"),  # an unexpected key
    ):
        with pytest.raises(IngestionError) as caught:
            parse_request(json.dumps(request_document(["README.md"], **{field: value})).encode())
        assert SENTINEL not in str(caught.value)
        assert SENTINEL not in repr(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
    with pytest.raises(IngestionError) as caught:
        parse_request(request_bytes([f"../{SENTINEL}"]))
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.json",
        "a/../b.json",
        "/etc/passwd",
        "C:/x.json",
        "a\\b.json",
        "file.json:stream",
        "CON",
        "dir/NUL.txt",
        "trailing.",
        "trailing ",
        "a//b.json",
        "./a.json",
        "",
        "x" * 1025,
        "e\u0301.json",  # not NFC
        "ctrl\x07.json",
        "a/b/c/d/e/f/g.json",  # 7 segments
    ],
)
def test_path_abuse_is_rejected_before_any_work(path):
    assert code_of(request_bytes([path])) == IngestErrorCode.PATH_POLICY


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["a.json", "b.json", "c.json", "d.json", "e.json", "f.json", "g.json"],
        ["a.json", "a.json"],
        ["a.json", "A.JSON"],
        ["Docs/a.json", "docs/b.json"],  # one directory, two spellings
        ["docs", "docs/a.json"],  # file and directory of the same name
    ],
    ids=["none", "seven", "duplicate", "case-duplicate", "dir-case", "file-vs-dir"],
)
def test_path_set_rules(paths):
    assert code_of(request_bytes(paths)) == IngestErrorCode.PATH_POLICY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"not json", IngestErrorCode.REQUEST_INVALID_JSON),
        (b'{"owner": "a", "owner": "b"}', IngestErrorCode.REQUEST_INVALID_JSON),
        (b'{"x": NaN}', IngestErrorCode.REQUEST_INVALID_JSON),
        (b"\xff\xfe", IngestErrorCode.REQUEST_INVALID_JSON),
        (b"[]", IngestErrorCode.REQUEST_INVALID),
    ],
)
def test_strict_json_is_enforced(raw, expected):
    assert code_of(raw) == expected


@pytest.mark.parametrize(
    ("unreadable", "expected"),
    [
        (ErrorCode.INPUT_UNREADABLE, IngestErrorCode.REQUEST_UNREADABLE),
        (ErrorCode.INPUT_TOO_LARGE, IngestErrorCode.REQUEST_TOO_LARGE),
    ],
)
def test_unreadable_request_input(unreadable, expected):
    assert code_of(UnreadableInput(unreadable)) == expected


def test_allowlist_is_exact_and_independent_of_the_request():
    request = parse_request(request_bytes(["README.md"]))
    require_allowed(request, parse_allow_repo(f"{OWNER}/{REPO}"))
    for other in (f"{OWNER}/other", "other/" + REPO, f"{OWNER.upper()}/{REPO}"):
        with pytest.raises(IngestionError) as caught:
            require_allowed(request, parse_allow_repo(other))
        assert caught.value.code == IngestErrorCode.REPOSITORY_NOT_ALLOWED


@pytest.mark.parametrize(
    "value", ["", "owner", "owner/", "/repo", "a/b/c", "o/r.git", "o/..", "o wner/r", "o/r\n"]
)
def test_malformed_allowlist_is_rejected(value):
    with pytest.raises(IngestionError) as caught:
        parse_allow_repo(value)
    assert caught.value.code == IngestErrorCode.ALLOWLIST_INVALID
    assert parse_allow_repo("o/r") == RepoRef(owner="o", repo="r")


def test_workspace_id_pattern_matches_the_claim_and_manifest_contracts(claim_data, manifest_data):
    """The pattern is duplicated by necessity (evidence/* is frozen); fail loudly on drift."""
    candidates = ["ab", "a", "A1", "a_b-c", "-ab", "a" * 63, "a" * 64, "ab ", "ab\n", "é1", "a-"]
    for candidate in candidates:
        verdicts = {
            accepts(IngestionRequest, request_document(["README.md"], workspace_id=candidate)),
            accepts(SourceManifest, {**manifest_data, "workspace_id": candidate}),
            accepts(Claim, {**claim_data, "workspace_id": candidate}),
        }
        assert len(verdicts) == 1, candidate


def accepts(model, document) -> bool:
    try:
        model.model_validate(document)
    except ValueError:  # pydantic ValidationError is a ValueError
        return False
    return True
