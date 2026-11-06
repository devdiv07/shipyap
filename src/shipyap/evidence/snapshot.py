"""The only filesystem reads of the evidence check. Read-only; nothing is executed or listed.

Threat model for snapshot reads:
- Manifest and claim paths are untrusted. Each path must pass the portable policy, and the
  joined path must resolve (following symlinks and junctions) inside the resolved snapshot
  root, otherwise PATH_ESCAPE. Only manifest-listed paths referenced by the claim are opened.
- The snapshot root is a local directory the operator controls and is ASSUMED IMMUTABLE for
  the duration of a check. Path resolution and file opening are separate steps, so a
  concurrent writer could race them (TOCTOU). Mitigations below narrow that window: the
  opened handle must be the same regular file (device, inode, size, mtime) that was resolved,
  and after hashing the path is resolved and checked again. They do NOT eliminate races,
  e.g. an intermediate directory swapped and restored between checks, or an in-place rewrite
  that preserves size and mtime. No TOCTOU resistance is claimed.
- Identity uses os.stat st_dev/st_ino; filesystems that report zero inode numbers weaken it.
"""

import hashlib
import os
import stat
from pathlib import Path

from shipyap.evidence.report import ErrorCode, SourceReason
from shipyap.evidence.validation import DigestObservation, UnreadableInput

INPUT_CAP_BYTES = 1024 * 1024
SOURCE_CAP_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024

_Identity = tuple[int, int, int, int]


def read_input(path: Path, cap: int = INPUT_CAP_BYTES) -> bytes | UnreadableInput:
    """Read an operator-designated input file; errors are codes, never OS messages."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # also avoids blocking on FIFOs
            return UnreadableInput(ErrorCode.INPUT_UNREADABLE)
        with open(path, "rb") as handle:
            data = handle.read(cap + 1)
    except (OSError, ValueError):
        return UnreadableInput(ErrorCode.INPUT_UNREADABLE)
    if len(data) > cap:
        return UnreadableInput(ErrorCode.INPUT_TOO_LARGE)
    return data


def observe_digest(root: Path, relative: str, cap: int = SOURCE_CAP_BYTES) -> DigestObservation:
    """SHA-256 of `relative` inside `root`, or a failure reason. Never follows paths out."""
    try:
        root_resolved = root.resolve(strict=True)
        candidate = root_resolved / relative
        target = candidate.resolve()
        if not _contained(target, root_resolved):
            return _failed(SourceReason.PATH_ESCAPE)
        before = os.stat(target)
    except FileNotFoundError:
        return _failed(SourceReason.FILE_NOT_FOUND)
    except (OSError, RuntimeError, ValueError):
        return _failed(SourceReason.SOURCE_UNREADABLE)
    if not stat.S_ISREG(before.st_mode):
        return _failed(SourceReason.NOT_A_REGULAR_FILE)
    if before.st_size > cap:
        return _failed(SourceReason.FILE_TOO_LARGE)

    observation = _hash_open_file(target, _identity(before), cap)
    if observation.sha256 is not None and not _unchanged(
        candidate, target, root_resolved, _identity(before)
    ):
        return _failed(SourceReason.SOURCE_CHANGED)
    return observation


def _hash_open_file(target: Path, expected: _Identity, cap: int) -> DigestObservation:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        return _failed(SourceReason.SOURCE_UNREADABLE)
    try:
        observation = _hash_descriptor(descriptor, expected, cap)
    finally:
        closed = _close_once(descriptor)
    if not closed and observation.sha256 is not None:
        # The digest is not trusted if the handle could not be released cleanly. An
        # earlier failure stays the primary reason; either way there is no SOURCE_MATCH.
        return _failed(SourceReason.SOURCE_CLOSE_FAILED)
    return observation


def _hash_descriptor(descriptor: int, expected: _Identity, cap: int) -> DigestObservation:
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            return _failed(SourceReason.SOURCE_CHANGED)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, _CHUNK_BYTES):
            total += len(chunk)
            if total > cap:
                return _failed(SourceReason.FILE_TOO_LARGE)
            digest.update(chunk)
    except OSError:
        return _failed(SourceReason.SOURCE_UNREADABLE)
    return DigestObservation(sha256=digest.hexdigest(), failure=None)


def _close_once(descriptor: int) -> bool:
    """Never retried: after a failed close the descriptor state is unspecified (Linux releases
    it), so a second close could hit an unrelated descriptor that reused the number."""
    try:
        os.close(descriptor)
    except OSError:  # message and filename are deliberately dropped
        return False
    return True


def _unchanged(candidate: Path, target: Path, root: Path, expected: _Identity) -> bool:
    try:
        again = candidate.resolve()
        return again == target and _contained(again, root) and _identity(os.stat(again)) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _contained(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _identity(result: os.stat_result) -> _Identity:
    return (result.st_dev, result.st_ino, result.st_size, result.st_mtime_ns)


def _failed(reason: SourceReason) -> DigestObservation:
    return DigestObservation(sha256=None, failure=reason)
