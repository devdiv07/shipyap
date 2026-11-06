"""Fixed error codes for read-only GitHub ingestion.

Every failure is one of these codes with a fixed message. Nothing derived from the request,
the response body, a URL, a local path, an OS error or a token is ever put into an error, so
a failure cannot leak a secret or a private path into stderr or a log.
"""

from enum import StrEnum


class IngestErrorCode(StrEnum):
    # Malformed request or allowlist.
    REQUEST_UNREADABLE = "REQUEST_UNREADABLE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    REQUEST_INVALID_JSON = "REQUEST_INVALID_JSON"
    REQUEST_INVALID = "REQUEST_INVALID"
    PATH_POLICY = "PATH_POLICY"
    ALLOWLIST_INVALID = "ALLOWLIST_INVALID"
    REPOSITORY_NOT_ALLOWED = "REPOSITORY_NOT_ALLOWED"
    # Missing source, or an entry that is not a plain file.
    NOT_FOUND = "NOT_FOUND"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    SYMLINK_REFUSED = "SYMLINK_REFUSED"
    SUBMODULE_REFUSED = "SUBMODULE_REFUSED"
    NOT_A_FILE = "NOT_A_FILE"
    # Resource limits.
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    REQUEST_BUDGET_EXCEEDED = "REQUEST_BUDGET_EXCEEDED"
    # Provider and network behaviour.
    RATE_LIMITED = "RATE_LIMITED"
    ACCESS_DENIED = "ACCESS_DENIED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    TLS_ERROR = "TLS_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    REDIRECT_REFUSED = "REDIRECT_REFUSED"
    HTTP_ERROR = "HTTP_ERROR"
    # Provider data that does not match what was asked for.
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    RESPONSE_MISMATCH = "RESPONSE_MISMATCH"
    TREE_TRUNCATED = "TREE_TRUNCATED"
    BLOB_DIGEST_MISMATCH = "BLOB_DIGEST_MISMATCH"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    # Local output.
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_PARENT_INVALID = "OUTPUT_PARENT_INVALID"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
    OUTPUT_VERIFY_FAILED = "OUTPUT_VERIFY_FAILED"
    OUTPUT_PUBLISH_FAILED = "OUTPUT_PUBLISH_FAILED"
    INCOMPLETE_SNAPSHOT = "INCOMPLETE_SNAPSHOT"


MESSAGES: dict[IngestErrorCode, str] = {
    IngestErrorCode.REQUEST_UNREADABLE: "request file could not be read as a regular file",
    IngestErrorCode.REQUEST_TOO_LARGE: "request file exceeds the size limit",
    IngestErrorCode.REQUEST_INVALID_JSON: "request is not strict UTF-8 JSON",
    IngestErrorCode.REQUEST_INVALID: "request does not match the ingestion request schema",
    IngestErrorCode.PATH_POLICY: "request paths violate the repository path policy",
    IngestErrorCode.ALLOWLIST_INVALID: "--allow-repo must be exactly OWNER/REPO",
    IngestErrorCode.REPOSITORY_NOT_ALLOWED: "request repository is not the allowlisted repository",
    IngestErrorCode.NOT_FOUND: "repository or commit not found, or not public",
    IngestErrorCode.PATH_NOT_FOUND: "a requested path does not exist at the pinned commit",
    IngestErrorCode.SYMLINK_REFUSED: "a requested path is a symlink; links are never followed",
    IngestErrorCode.SUBMODULE_REFUSED: "a requested path is a submodule; submodules are refused",
    IngestErrorCode.NOT_A_FILE: "a requested path is not a regular file",
    IngestErrorCode.FILE_TOO_LARGE: "a requested file exceeds the per-file size limit",
    IngestErrorCode.RESPONSE_TOO_LARGE: "a response exceeded its byte limit",
    IngestErrorCode.REQUEST_BUDGET_EXCEEDED: "the request budget for one ingestion was exceeded",
    IngestErrorCode.RATE_LIMITED: "GitHub rate limit reached; nothing was retried",
    IngestErrorCode.ACCESS_DENIED: "GitHub refused access (HTTP 403)",
    IngestErrorCode.TIMEOUT: "a network operation or the overall deadline timed out",
    IngestErrorCode.NETWORK_ERROR: "network connection failed",
    IngestErrorCode.TLS_ERROR: "TLS connection or certificate verification failed",
    IngestErrorCode.SERVER_ERROR: "GitHub returned a server error",
    IngestErrorCode.REDIRECT_REFUSED: "GitHub answered with a redirect; redirects are never followed",
    IngestErrorCode.HTTP_ERROR: "GitHub returned an unexpected HTTP status",
    IngestErrorCode.UNEXPECTED_RESPONSE: "GitHub response did not have the expected shape",
    IngestErrorCode.RESPONSE_MISMATCH: "GitHub response does not match the pinned request",
    IngestErrorCode.TREE_TRUNCATED: "a directory listing was truncated by the provider",
    IngestErrorCode.BLOB_DIGEST_MISMATCH: "received file bytes do not match their Git object id",
    IngestErrorCode.MANIFEST_INVALID: "the manifest could not be built from verified data",
    IngestErrorCode.OUTPUT_EXISTS: "output path already exists; nothing was written",
    IngestErrorCode.OUTPUT_PARENT_INVALID: "output parent must be an existing real directory",
    IngestErrorCode.OUTPUT_WRITE_FAILED: "could not create or write a snapshot file",
    IngestErrorCode.OUTPUT_VERIFY_FAILED: "written snapshot failed verification; not published",
    IngestErrorCode.OUTPUT_PUBLISH_FAILED: "could not publish the snapshot directory",
    IngestErrorCode.INCOMPLETE_SNAPSHOT: "directory is not a complete, consistent snapshot",
}

# Worth running the same read-only command again later. Everything else needs a changed input.
RETRYABLE = frozenset(
    {
        IngestErrorCode.RATE_LIMITED,
        IngestErrorCode.TIMEOUT,
        IngestErrorCode.NETWORK_ERROR,
        IngestErrorCode.SERVER_ERROR,
    }
)


class IngestionError(Exception):
    """Code and fixed message only. `retry_after_seconds` is a bounded integer or None."""

    def __init__(self, code: IngestErrorCode, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE
