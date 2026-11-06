"""Snapshot writer: staging, verified completion, one publish step. The only ingestion disk I/O.

Why this is not `evidence.outputs.write_outputs`: that writer creates a fixed set of text files
one after another into an existing directory, so a failure part-way leaves earlier files behind
(tracked as OUTPUT-01). Here nothing appears under the final name until everything is written and
verified:

1. `--out` must not exist; its parent must be an existing real directory.
2. A sibling staging directory `.<name>.partial-<random>` is created exclusively. Every file is
   created with O_EXCL (and O_NOFOLLOW where available) from names that were already validated.
   The completion record is written LAST.
3. The staged tree is verified against the completion rule (`verify_completion`) before
   publishing.
4. A single `os.rename` publishes it. On any failure only the entries this run created are
   removed, so a failed run leaves no directory under the final name.

Limits (documented, not defended):
- Operator-controlled filesystem assumption, like the rest of the tool. A concurrent local writer
  can interfere between the final-destination check and the rename. On Windows `os.rename`
  refuses an existing target; on POSIX `rename(2)` replaces an EMPTY directory (UNVERIFIED here,
  not exercised on Linux in this work), so a writer that creates an empty directory at the final
  name in that window could win. No cross-platform atomic no-replace guarantee is claimed.
- Files are fsynced; the parent directory is not, so durability after power loss is not claimed.
- The completion record is a consistency indicator, not an authenticated attestation.
- Nothing makes the published directory immutable. `check-evidence` still assumes the snapshot is
  not modified while it runs.
"""

import hmac
import os
import secrets
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import ValidationError

from public_research_os.evidence.manifest import SourceManifest
from public_research_os.evidence.policy import check_relative_path
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.ingestion.assemble import (
    MANIFEST_NAME,
    RECORD_NAME,
    SNAPSHOT_DIR,
    IngestionRecord,
    SnapshotPlan,
    sha256_hex,
)
from public_research_os.ingestion.errors import IngestErrorCode, IngestionError

_DOCUMENT_CAP_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def preflight_destination(out: Path) -> None:
    """Cheap, side-effect-free checks, run before any network access."""
    if out.name in {"", ".", ".."}:
        raise IngestionError(IngestErrorCode.OUTPUT_PARENT_INVALID)
    try:
        parent = os.lstat(out.parent)
    except (OSError, ValueError):
        raise IngestionError(IngestErrorCode.OUTPUT_PARENT_INVALID) from None
    if _is_link_or_reparse_point(parent) or not stat.S_ISDIR(parent.st_mode):
        raise IngestionError(IngestErrorCode.OUTPUT_PARENT_INVALID)
    try:
        os.lstat(out)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        raise IngestionError(IngestErrorCode.OUTPUT_PARENT_INVALID) from None
    raise IngestionError(IngestErrorCode.OUTPUT_EXISTS)


def publish_snapshot(out: Path, plan: SnapshotPlan) -> None:
    """Create `out` with exactly the planned entries, or raise and leave nothing under `out`."""
    publish_directory(out, plan.entries, verify_completion)


def publish_directory(
    out: Path, entries: Iterable[tuple[str, bytes]], verify: Callable[[Path], None]
) -> None:
    """The staging and publish steps of `publish_snapshot`, for any completion rule.

    Entries are written in order (so a completion record goes last), then `verify` checks the
    staged directory and must raise `IngestionError` if it is not complete; that becomes
    OUTPUT_VERIFY_FAILED. Other exceptions propagate unchanged. Every failure removes exactly
    what this run created and leaves nothing under `out`.
    """
    preflight_destination(out)
    staging = out.parent / f".{out.name}.partial-{secrets.token_hex(8)}"
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        _make_dir(staging, created_dirs)
        made_dirs: dict[str, str] = {}
        for relative, data in entries:
            _write_entry(staging, relative, data, made_dirs, created_files, created_dirs)
        try:
            verify(staging)
        except IngestionError:
            raise IngestionError(IngestErrorCode.OUTPUT_VERIFY_FAILED) from None
        _publish(staging, out)
    except BaseException:
        _cleanup(created_files, created_dirs)  # only entries this run created, by exact path
        raise


def verify_completion(directory: Path) -> None:
    """The machine-readable completion rule. Returns normally only for a complete snapshot.

    Complete means: a real directory holding exactly `snapshot/`, `manifest.json` and
    `ingestion-record.json`; a COMPLETE record; a manifest that hashes to the recorded digest and
    validates as a v1 `SourceManifest` agreeing with the record; and a `snapshot/` tree containing
    exactly the recorded files (no extras, no links) with matching sizes and SHA-256 digests.
    It is NOT proof of authenticity or of anything the bytes say.
    """
    try:
        _verify(directory)
    except (OSError, ValueError, ValidationError, StrictJsonError):
        raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT) from None


# --- writing ----------------------------------------------------------------------------------


def _write_entry(
    staging: Path,
    relative: str,
    data: bytes,
    made_dirs: dict[str, str],
    created_files: list[Path],
    created_dirs: list[Path],
) -> None:
    *directories, name = relative.split("/")
    if any(check_relative_path(segment) is not None for segment in (*directories, name)):
        raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED)
    current, prefix = staging, ""
    for segment in directories:
        prefix = f"{prefix}/{segment}" if prefix else segment
        current = current / segment
        spelling = made_dirs.get(prefix.casefold())
        if spelling is None:
            _make_dir(current, created_dirs)
            made_dirs[prefix.casefold()] = prefix
        elif spelling != prefix:  # the same directory spelled in two cases
            raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED)
    target = current / name
    try:
        descriptor = os.open(target, _WRITE_FLAGS, 0o644)
    except OSError:
        raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED) from None
    created_files.append(target)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        _close_quietly(descriptor)
        raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED) from None
    try:
        os.close(descriptor)
    except OSError:  # a close failure can mean lost data: never continue as if it succeeded
        raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED) from None


def _make_dir(path: Path, created_dirs: list[Path]) -> None:
    try:
        os.mkdir(path)
    except OSError:
        raise IngestionError(IngestErrorCode.OUTPUT_WRITE_FAILED) from None
    created_dirs.append(path)


def _publish(staging: Path, out: Path) -> None:
    try:
        os.lstat(out)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise IngestionError(IngestErrorCode.OUTPUT_PUBLISH_FAILED) from None
    else:  # something appeared at the final name since the preflight: never replace it
        raise IngestionError(IngestErrorCode.OUTPUT_PUBLISH_FAILED)
    try:
        os.rename(staging, out)
    except OSError:
        raise IngestionError(IngestErrorCode.OUTPUT_PUBLISH_FAILED) from None


def _cleanup(created_files: list[Path], created_dirs: list[Path]) -> None:
    """Best effort. Removes exactly what this run created (never a recursive delete)."""
    for path in reversed(created_files):
        try:
            os.unlink(path)
        except OSError:
            pass
    for path in reversed(created_dirs):
        try:
            os.rmdir(path)
        except OSError:
            pass


# --- verification ----------------------------------------------------------------------------


def _verify(directory: Path) -> None:
    _require_real_directory(directory)
    if _listing(directory) != {SNAPSHOT_DIR: True, MANIFEST_NAME: False, RECORD_NAME: False}:
        raise _incomplete()
    record = IngestionRecord.model_validate(
        loads_strict(_read_regular(directory / RECORD_NAME, _DOCUMENT_CAP_BYTES))
    )
    manifest_bytes = _read_regular(directory / MANIFEST_NAME, _DOCUMENT_CAP_BYTES)
    if not hmac.compare_digest(sha256_hex(manifest_bytes), record.manifest_sha256):
        raise _incomplete()
    manifest = SourceManifest.model_validate(loads_strict(manifest_bytes))
    if (str(manifest.source_url), manifest.revision_sha, manifest.workspace_id) != (
        record.source_url,
        record.revision_sha,
        record.workspace_id,
    ):
        raise _incomplete()
    if [(f.path, f.sha256) for f in manifest.files] != [(f.path, f.sha256) for f in record.files]:
        raise _incomplete()

    # Record paths are now known to equal manifest paths, which passed the path policy.
    recorded = {entry.path: entry for entry in record.files}
    files, directories = _walk(directory / SNAPSHOT_DIR)
    implied = {
        "/".join(parts[:depth])
        for parts in (path.split("/") for path in recorded)
        for depth in range(1, len(parts))
    }
    if files != set(recorded) or directories != implied:
        raise _incomplete()
    for path, entry in recorded.items():
        data = _read_regular(directory / SNAPSHOT_DIR / path, entry.size)
        if len(data) != entry.size or not hmac.compare_digest(sha256_hex(data), entry.sha256):
            raise _incomplete()


def _walk(root: Path) -> tuple[set[str], set[str]]:
    """Relative file and directory names under `root`; any link or special entry is a failure."""
    _require_real_directory(root)
    files: set[str] = set()
    directories: set[str] = set()
    pending = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if _is_link_or_reparse_point(info):
                    raise _incomplete()
                if stat.S_ISDIR(info.st_mode):
                    directories.add(relative)
                    pending.append((folder / entry.name, relative))
                elif stat.S_ISREG(info.st_mode):
                    files.add(relative)
                else:
                    raise _incomplete()
    return files, directories


def _listing(directory: Path) -> dict[str, bool]:
    names: dict[str, bool] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if _is_link_or_reparse_point(info):
                raise _incomplete()
            names[entry.name] = stat.S_ISDIR(info.st_mode)
    return names


def _require_real_directory(path: Path) -> None:
    info = os.lstat(path)
    if _is_link_or_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise _incomplete()


def _read_regular(path: Path, cap: int) -> bytes:
    """A regular, non-link file of at most `cap` bytes; reading stops one byte past the cap."""
    info = os.lstat(path)
    if _is_link_or_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise _incomplete()
    descriptor = os.open(path, _READ_FLAGS)
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        _close_quietly(descriptor)
    if total > cap:
        raise _incomplete()
    return b"".join(chunks)


def _incomplete() -> IngestionError:
    return IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT)


def _close_quietly(descriptor: int) -> None:
    """Only for cleanup on paths that already fail, or for read-only descriptors."""
    try:
        os.close(descriptor)
    except OSError:
        pass


def _is_link_or_reparse_point(info: os.stat_result) -> bool:
    # Same test as evidence.outputs; kept local so this module does not import a private name.
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISLNK(info.st_mode) or bool(reparse)
