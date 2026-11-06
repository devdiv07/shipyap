"""Brief and draft bundles: flat directories, published through the snapshot writer and checked by
their own completion rule.

The write path is Assignment 02's, reused unchanged through `ingestion.writer.publish_directory`:
- `--out` must not exist;
- files are written into a sibling staging directory with O_EXCL and fsync, the record last;
- the staged tree is verified, then published with one rename;
- a failure removes only what this run created, so nothing appears under the final name.

Only the completion rule is bundle-specific, which is why `verify_bundle` exists: a bundle is a
fixed flat set of files plus `bundle-record.json`, not a `snapshot/` tree with a manifest. It is
read-only. The record is a consistency indicator, not an attestation: anyone who can write the
directory can forge it.
"""

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.models import (
    BUNDLE_FILES,
    RECORD_FILE,
    BundleFile,
    BundleKind,
    BundleRecord,
)
from public_research_os.evidence.report import canonical_json
from public_research_os.evidence.snapshot import read_input
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.evidence.validation import UnreadableInput
from public_research_os.ingestion.errors import IngestErrorCode, IngestionError
from public_research_os.ingestion.writer import preflight_destination, publish_directory

READ_CAP_BYTES = 8 * 1024 * 1024

_OUTPUT_CODES = {
    IngestErrorCode.OUTPUT_EXISTS: EditorialErrorCode.OUTPUT_EXISTS,
    IngestErrorCode.OUTPUT_PARENT_INVALID: EditorialErrorCode.OUTPUT_PARENT_INVALID,
    IngestErrorCode.OUTPUT_WRITE_FAILED: EditorialErrorCode.OUTPUT_WRITE_FAILED,
    IngestErrorCode.OUTPUT_VERIFY_FAILED: EditorialErrorCode.OUTPUT_VERIFY_FAILED,
    IngestErrorCode.OUTPUT_PUBLISH_FAILED: EditorialErrorCode.OUTPUT_PUBLISH_FAILED,
}


class BundleIncomplete(Exception):
    """The directory is not a complete, unmodified bundle of the expected kind."""


@dataclass(frozen=True)
class VerifiedBundle:
    record: BundleRecord
    record_sha256: str
    files: dict[str, bytes]  # exactly the bytes whose digests matched the record


def preflight_out(out: Path) -> None:
    """Fail before any work if `--out` exists or its parent is unusable."""
    try:
        preflight_destination(out)
    except IngestionError as exc:
        raise EditorialError(_output_code(exc)) from None


def publish_bundle(
    out: Path, kind: BundleKind, files: dict[str, bytes], **record_fields: Any
) -> BundleRecord:
    names = BUNDLE_FILES[kind]
    if set(files) != set(names):
        raise ValueError("bundle files do not match the bundle kind")  # programming error
    record = BundleRecord(
        kind=kind,
        files=tuple(
            BundleFile(name=name, sha256=_sha256(files[name]), size=len(files[name]))
            for name in names
        ),
        **record_fields,
    )
    record_bytes = canonical_json(record.model_dump(mode="json")).encode("utf-8")
    entries = [*((name, files[name]) for name in names), (RECORD_FILE, record_bytes)]

    def verify_staged(directory: Path) -> None:
        try:
            verify_bundle(directory, kind)
        except BundleIncomplete:
            # publish_directory reports any IngestionError from here as OUTPUT_VERIFY_FAILED.
            raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT) from None

    try:
        publish_directory(out, entries, verify_staged)
    except IngestionError as exc:
        raise EditorialError(_output_code(exc)) from None
    return record


def verify_bundle(directory: Path, kind: BundleKind) -> VerifiedBundle:
    """Returns only for a complete bundle of `kind`. A real directory must hold exactly the
    kind's files and the record. No links or special files are allowed, the record must be
    strictly valid, and every file's size and SHA-256 must equal the record."""
    try:
        return _verify(directory, kind)
    except (OSError, ValueError, ValidationError, StrictJsonError):
        raise BundleIncomplete from None


def _verify(directory: Path, kind: BundleKind) -> VerifiedBundle:
    info = os.lstat(directory)
    if _is_link_or_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise BundleIncomplete
    names = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            entry_info = entry.stat(follow_symlinks=False)
            if _is_link_or_reparse_point(entry_info) or not stat.S_ISREG(entry_info.st_mode):
                raise BundleIncomplete
            names.add(entry.name)
    if names != {*BUNDLE_FILES[kind], RECORD_FILE}:
        raise BundleIncomplete
    record_bytes = _read(directory / RECORD_FILE)
    record = BundleRecord.model_validate(loads_strict(record_bytes))
    if record.kind != kind:
        raise BundleIncomplete
    files = {}
    for entry in record.files:
        data = _read(directory / entry.name)
        if len(data) != entry.size or not hmac.compare_digest(_sha256(data), entry.sha256):
            raise BundleIncomplete
        files[entry.name] = data
    return VerifiedBundle(record=record, record_sha256=_sha256(record_bytes), files=files)


def _read(path: Path) -> bytes:
    data = read_input(path, READ_CAP_BYTES)
    if isinstance(data, UnreadableInput):
        raise BundleIncomplete
    return data


def _output_code(exc: IngestionError) -> EditorialErrorCode:
    return _OUTPUT_CODES.get(exc.code, EditorialErrorCode.OUTPUT_WRITE_FAILED)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_link_or_reparse_point(info: os.stat_result) -> bool:
    # The same four-line test as evidence.outputs and ingestion.writer, kept local (as they do)
    # so no module imports another's private name.
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISLNK(info.st_mode) or bool(reparse)
