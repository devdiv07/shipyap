"""Fixed error codes for story discovery and the backlog. As elsewhere, nothing taken from an input
file, the snapshot, a path or an OS error is put into a message; `details` carry only schema
locations built from known field names and Pydantic's fixed constraint text."""

from enum import StrEnum


class StoryErrorCode(StrEnum):
    SNAPSHOT_REPOSITORY_INVALID = "SNAPSHOT_REPOSITORY_INVALID"
    FINDINGS_UNREADABLE = "FINDINGS_UNREADABLE"
    FINDINGS_TOO_LARGE = "FINDINGS_TOO_LARGE"
    FINDINGS_INVALID_JSON = "FINDINGS_INVALID_JSON"
    FINDINGS_INVALID = "FINDINGS_INVALID"
    FINDINGS_SNAPSHOT_UNKNOWN = "FINDINGS_SNAPSHOT_UNKNOWN"
    FINDINGS_WORKSPACE_MISMATCH = "FINDINGS_WORKSPACE_MISMATCH"
    BACKLOG_UNUSABLE = "BACKLOG_UNUSABLE"
    BACKLOG_WRITE_FAILED = "BACKLOG_WRITE_FAILED"
    STORY_UNKNOWN = "STORY_UNKNOWN"


MESSAGES: dict[StoryErrorCode, str] = {
    StoryErrorCode.SNAPSHOT_REPOSITORY_INVALID: (
        "a snapshot's source URL is not a github.com OWNER/REPOSITORY URL"
    ),
    StoryErrorCode.FINDINGS_UNREADABLE: "findings file could not be read as a regular file",
    StoryErrorCode.FINDINGS_TOO_LARGE: "findings file exceeds the size limit",
    StoryErrorCode.FINDINGS_INVALID_JSON: "findings file is not strict UTF-8 JSON",
    StoryErrorCode.FINDINGS_INVALID: "findings file does not match the findings schema",
    StoryErrorCode.FINDINGS_SNAPSHOT_UNKNOWN: (
        "a findings file names a snapshot manifest that no --snapshot supplied"
    ),
    StoryErrorCode.FINDINGS_WORKSPACE_MISMATCH: (
        "a findings file's workspace_id differs from its snapshot's"
    ),
    StoryErrorCode.BACKLOG_UNUSABLE: (
        "the story backlog is missing, not a story backlog or unreadable"
    ),
    StoryErrorCode.BACKLOG_WRITE_FAILED: "the story backlog could not be updated; nothing was saved",
    StoryErrorCode.STORY_UNKNOWN: "no story with that identifier is in the backlog",
}


class StoryError(Exception):
    """Code, fixed message and optional sanitized schema details. Never input values."""

    def __init__(self, code: StoryErrorCode, details: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.details = details
