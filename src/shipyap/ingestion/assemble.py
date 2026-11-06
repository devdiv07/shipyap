"""Pure assembly of the deterministic v1 manifest and the completion record. No I/O.

The manifest is the EXISTING `SourceManifest` (schema v1), built through its own validator and
rendered with the same `canonical_json` the reports use, so identical inputs give identical bytes:
no timestamps, no local paths, no random values. Files are sorted by path.

The record is written last and is the completion marker (see `writer.verify_completion`). It is a
consistency indicator, NOT an authenticated attestation: anyone who can write the directory can
forge it. It says nothing about scientific truth, claim approval or publication readiness.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from shipyap.evidence.manifest import SourceManifest
from shipyap.evidence.policy import PATH_MESSAGES, check_relative_path
from shipyap.evidence.report import canonical_json
from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.github import API_VERSION, FetchedFile, FetchedSnapshot
from shipyap.ingestion.request import IngestionRequest

SNAPSHOT_DIR = "snapshot"
MANIFEST_NAME = "manifest.json"
RECORD_NAME = "ingestion-record.json"
RETRIEVAL_MODE = "github-rest-git-data"

RECORD_NOTICES: tuple[str, ...] = (
    "COMPLETE means the files listed here were written and re-verified against this record. "
    "It is not an authenticated attestation: anyone who can write this directory can forge it.",
    "File bytes were hashed independently. Tree listings are provider-supplied metadata; "
    "complete tree membership is not independently proven.",
    "This is not scientific verification, claim approval or publication readiness.",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordFile(_Frozen):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    git_blob_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    git_mode: Literal["100644", "100755"]

    @field_validator("path")
    @classmethod
    def portable_relative_path(cls, value: str) -> str:
        violation = check_relative_path(value)
        if violation is not None:
            raise ValueError(PATH_MESSAGES[violation])
        return value


class IngestionRecord(_Frozen):
    record_version: Literal["1"] = "1"
    status: Literal["COMPLETE"] = "COMPLETE"
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")  # metadata, not authorization
    source_url: str
    revision_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    retrieval_mode: Literal["github-rest-git-data"] = RETRIEVAL_MODE
    api_version: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[RecordFile, ...] = Field(min_length=1)
    notices: tuple[str, ...] = RECORD_NOTICES

    @model_validator(mode="after")
    def notices_are_fixed(self) -> "IngestionRecord":
        if self.notices != RECORD_NOTICES:
            raise ValueError("record notices are fixed")
        return self


@dataclass(frozen=True)
class SnapshotPlan:
    """What the writer must create, in creation order. The record is always last."""

    entries: tuple[tuple[str, bytes], ...]  # (path relative to the output directory, bytes)
    manifest_sha256: str
    commit_sha: str
    file_count: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(request: IngestionRequest, files: tuple[FetchedFile, ...]) -> SourceManifest:
    try:
        manifest = SourceManifest.model_validate(
            {
                "manifest_version": "1",
                "workspace_id": request.workspace_id,
                "source_url": request.canonical_url,
                "revision_sha": request.revision_sha,
                "files": [{"path": f.path, "sha256": sha256_hex(f.data)} for f in files],
            }
        )
    except ValidationError:
        raise IngestionError(IngestErrorCode.MANIFEST_INVALID) from None
    if str(manifest.source_url) != request.canonical_url:
        raise IngestionError(IngestErrorCode.MANIFEST_INVALID)
    return manifest


def build_plan(request: IngestionRequest, fetched: FetchedSnapshot) -> SnapshotPlan:
    if fetched.commit_sha != request.revision_sha:
        raise IngestionError(IngestErrorCode.RESPONSE_MISMATCH)
    paths = [f.path for f in fetched.files]
    if len(paths) != len(set(paths)) or set(paths) != set(request.paths):
        raise IngestionError(IngestErrorCode.MANIFEST_INVALID)
    files = tuple(sorted(fetched.files, key=lambda f: f.path))

    manifest = build_manifest(request, files)
    manifest_bytes = canonical_json(manifest.model_dump(mode="json")).encode("utf-8")
    manifest_sha256 = sha256_hex(manifest_bytes)
    record = IngestionRecord(
        workspace_id=request.workspace_id,
        source_url=request.canonical_url,
        revision_sha=request.revision_sha,
        tree_sha=fetched.tree_sha,
        api_version=API_VERSION,
        manifest_sha256=manifest_sha256,
        files=tuple(
            RecordFile(
                path=f.path,
                sha256=sha256_hex(f.data),
                size=f.size,
                git_blob_sha1=f.blob_sha,
                git_mode=f.mode,  # a Literal; any other mode fails validation here
            )
            for f in files
        ),
    )
    record_bytes = canonical_json(record.model_dump(mode="json")).encode("utf-8")
    entries = (
        *((f"{SNAPSHOT_DIR}/{f.path}", f.data) for f in files),
        (MANIFEST_NAME, manifest_bytes),
        (RECORD_NAME, record_bytes),
    )
    return SnapshotPlan(
        entries=tuple(entries),
        manifest_sha256=manifest_sha256,
        commit_sha=fetched.commit_sha,
        file_count=len(files),
    )
