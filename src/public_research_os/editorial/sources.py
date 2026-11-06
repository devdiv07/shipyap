"""Source selection: load a COMPLETE `ingest-github` snapshot as text, or refuse it.

1. The existing completion rule (`ingestion.writer.verify_completion`) must accept the directory.
2. The record and manifest are read again, strictly parsed and cross-checked.
3. Each source file is read once, capped, and its SHA-256 must equal the record's. The bytes that
   become brief text are therefore exactly the bytes that were hashed. A file changed between
   steps 1 and 3 fails here instead of entering a brief.

Files must be strict UTF-8 text within the brief size limits; anything else is refused, never
truncated. Nothing is executed, rendered or interpreted. Source text is data, not instructions.
"""

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.evidence.manifest import SourceManifest
from public_research_os.evidence.snapshot import read_input
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.evidence.validation import UnreadableInput
from public_research_os.ingestion.assemble import (
    MANIFEST_NAME,
    RECORD_NAME,
    SNAPSHOT_DIR,
    IngestionRecord,
)
from public_research_os.ingestion.errors import IngestionError
from public_research_os.ingestion.writer import verify_completion

DOCUMENT_CAP_BYTES = 1024 * 1024
SOURCE_FILE_CAP_BYTES = 256 * 1024
SOURCE_TOTAL_CAP_BYTES = 512 * 1024


@dataclass(frozen=True)
class SourceText:
    path: str
    sha256: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedSnapshot:
    """A snapshot that passed the completion rule, with its files as verified text."""

    workspace_id: str
    source_url: str
    revision_sha: str
    tree_sha: str
    manifest_sha256: str
    record_sha256: str
    sources: tuple[SourceText, ...]

    def paths(self) -> tuple[str, ...]:
        return tuple(source.path for source in self.sources)


def split_lines(text: str) -> tuple[str, ...]:
    """1-based line list: split on LF, drop one trailing CR per line, no phantom final line."""
    if not text:
        return ()
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return tuple(line[:-1] if line.endswith("\r") else line for line in lines)


def load_verified_snapshot(directory: Path) -> VerifiedSnapshot:
    try:
        verify_completion(directory)
    except IngestionError:
        raise _incomplete() from None
    record_bytes = _read(directory / RECORD_NAME, DOCUMENT_CAP_BYTES)
    manifest_bytes = _read(directory / MANIFEST_NAME, DOCUMENT_CAP_BYTES)
    try:
        record = IngestionRecord.model_validate(loads_strict(record_bytes))
        manifest = SourceManifest.model_validate(loads_strict(manifest_bytes))
    except (StrictJsonError, ValidationError):
        raise _incomplete() from None
    if not hmac.compare_digest(_sha256(manifest_bytes), record.manifest_sha256):
        raise _incomplete()
    if (str(manifest.source_url), manifest.revision_sha, manifest.workspace_id) != (
        record.source_url,
        record.revision_sha,
        record.workspace_id,
    ) or [(f.path, f.sha256) for f in manifest.files] != [(f.path, f.sha256) for f in record.files]:
        raise _incomplete()

    if any(entry.size > SOURCE_FILE_CAP_BYTES for entry in record.files) or (
        sum(entry.size for entry in record.files) > SOURCE_TOTAL_CAP_BYTES
    ):
        raise EditorialError(EditorialErrorCode.SNAPSHOT_SOURCE_UNSUPPORTED)
    sources = []
    for entry in record.files:
        data = _read(directory / SNAPSHOT_DIR / entry.path, SOURCE_FILE_CAP_BYTES)
        if len(data) != entry.size or not hmac.compare_digest(_sha256(data), entry.sha256):
            raise _incomplete()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise EditorialError(EditorialErrorCode.SNAPSHOT_SOURCE_UNSUPPORTED) from None
        if "\x00" in text:
            raise EditorialError(EditorialErrorCode.SNAPSHOT_SOURCE_UNSUPPORTED)
        sources.append(SourceText(path=entry.path, sha256=entry.sha256, lines=split_lines(text)))
    return VerifiedSnapshot(
        workspace_id=record.workspace_id,
        source_url=record.source_url,
        revision_sha=record.revision_sha,
        tree_sha=record.tree_sha,
        manifest_sha256=record.manifest_sha256,
        record_sha256=_sha256(record_bytes),
        sources=tuple(sources),
    )


def _read(path: Path, cap: int) -> bytes:
    data = read_input(path, cap)
    if isinstance(data, UnreadableInput):
        # The completion rule already accepted this file, so any read problem now means it
        # changed or vanished: the snapshot is no longer the one that was verified.
        raise _incomplete()
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _incomplete() -> EditorialError:
    return EditorialError(EditorialErrorCode.SNAPSHOT_INCOMPLETE)
