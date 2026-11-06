"""Fixed error codes for the offline authoring commands (`draft-brief`, `draft-build`).

As with ingestion, a failure is a code plus a fixed message. Nothing taken from an input file,
the snapshot, a drafter response, a local path or an OS error is put into the message. The
optional `details` carry schema locations built only from known field names and indices, plus
Pydantic's fixed constraint text (never the rejected value), so an author can fix their file.
"""

from enum import StrEnum


class EditorialErrorCode(StrEnum):
    # Source selection.
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    SNAPSHOT_SOURCE_UNSUPPORTED = "SNAPSHOT_SOURCE_UNSUPPORTED"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    # Author contribution.
    CONTRIBUTION_UNREADABLE = "CONTRIBUTION_UNREADABLE"
    CONTRIBUTION_TOO_LARGE = "CONTRIBUTION_TOO_LARGE"
    CONTRIBUTION_INVALID_JSON = "CONTRIBUTION_INVALID_JSON"
    CONTRIBUTION_INVALID = "CONTRIBUTION_INVALID"
    CONTRIBUTION_WORKSPACE_MISMATCH = "CONTRIBUTION_WORKSPACE_MISMATCH"
    CONTRIBUTION_FOCUS_PATH_UNKNOWN = "CONTRIBUTION_FOCUS_PATH_UNKNOWN"
    # Brief and drafter response.
    BRIEF_INCOMPLETE = "BRIEF_INCOMPLETE"
    RESPONSE_UNREADABLE = "RESPONSE_UNREADABLE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_INVALID_JSON = "RESPONSE_INVALID_JSON"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    RESPONSE_BRIEF_MISMATCH = "RESPONSE_BRIEF_MISMATCH"
    # Local output.
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_PARENT_INVALID = "OUTPUT_PARENT_INVALID"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
    OUTPUT_VERIFY_FAILED = "OUTPUT_VERIFY_FAILED"
    OUTPUT_PUBLISH_FAILED = "OUTPUT_PUBLISH_FAILED"


MESSAGES: dict[EditorialErrorCode, str] = {
    EditorialErrorCode.SNAPSHOT_INCOMPLETE: (
        "--snapshot is not a complete, unmodified ingest-github snapshot"
    ),
    EditorialErrorCode.SNAPSHOT_SOURCE_UNSUPPORTED: (
        "a snapshot file is not UTF-8 text or exceeds the size limit for a brief"
    ),
    EditorialErrorCode.SNAPSHOT_MISMATCH: "the brief was built from a different snapshot",
    EditorialErrorCode.CONTRIBUTION_UNREADABLE: (
        "contribution file could not be read as a regular file"
    ),
    EditorialErrorCode.CONTRIBUTION_TOO_LARGE: "contribution file exceeds the size limit",
    EditorialErrorCode.CONTRIBUTION_INVALID_JSON: "contribution is not strict UTF-8 JSON",
    EditorialErrorCode.CONTRIBUTION_INVALID: (
        "contribution does not match the author contribution schema"
    ),
    EditorialErrorCode.CONTRIBUTION_WORKSPACE_MISMATCH: (
        "contribution workspace_id differs from the snapshot's"
    ),
    EditorialErrorCode.CONTRIBUTION_FOCUS_PATH_UNKNOWN: (
        "a focus_paths entry is not a file in the snapshot"
    ),
    EditorialErrorCode.BRIEF_INCOMPLETE: "--brief is not a complete, unmodified draft-brief output",
    EditorialErrorCode.RESPONSE_UNREADABLE: "response file could not be read as a regular file",
    EditorialErrorCode.RESPONSE_TOO_LARGE: "response file exceeds the size limit",
    EditorialErrorCode.RESPONSE_INVALID_JSON: "response is not strict UTF-8 JSON",
    EditorialErrorCode.RESPONSE_INVALID: "response does not match the draft response schema",
    EditorialErrorCode.RESPONSE_BRIEF_MISMATCH: (
        "response brief_sha256 does not match this brief's brief.json"
    ),
    EditorialErrorCode.OUTPUT_EXISTS: "output path already exists; nothing was written",
    EditorialErrorCode.OUTPUT_PARENT_INVALID: "output parent must be an existing real directory",
    EditorialErrorCode.OUTPUT_WRITE_FAILED: "could not create or write a bundle file",
    EditorialErrorCode.OUTPUT_VERIFY_FAILED: "written bundle failed verification; not published",
    EditorialErrorCode.OUTPUT_PUBLISH_FAILED: "could not publish the bundle directory",
}


class EditorialError(Exception):
    """Code, fixed message and optional sanitized schema details. Never input values."""

    def __init__(self, code: EditorialErrorCode, details: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.details = details
