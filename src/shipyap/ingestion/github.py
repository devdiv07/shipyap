"""Read-only GitHub Git Data reader. All network access happens behind the injected `Transport`.

How every returned byte is tied to the one pinned commit:
1. `GET git/commits/{sha}`: the response `sha` must equal the pinned 40-hex request value.
2. `GET git/trees/{tree_sha}` (never `recursive`), one directory level at a time starting at the
   commit's tree: each response `sha` must equal the id that was asked for, and `truncated` must
   be exactly false.
3. The entry for each requested path must be a regular file (`100644` or `100755`). Symlinks
   (`120000`), submodules (`160000` / type `commit`) and directories are refused BEFORE any
   content is requested.
4. `GET git/blobs/{blob_sha}` with the raw media type; the Git object id is recomputed from the
   received bytes and must equal the tree entry's id.

Provenance limit: tree listings are provider-supplied metadata. Blob bytes are independently
hashed against the ids those listings gave, but complete tree membership (that no other entry
exists, or that each tree id really hashes to its listing) is not independently proven.

Nothing in a response is used to build a URL or a local name: request paths come only from the
validated request and the host is fixed in the transport. File bytes are never interpreted.
"""

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.request import IngestionRequest

JSON_ACCEPT = "application/vnd.github+json"
RAW_ACCEPT = "application/vnd.github.raw+json"
API_VERSION = "2022-11-28"  # the documented default; recorded in every ingestion record

_REGULAR_MODES = frozenset({"100644", "100755"})
_TREE_MODE = "040000"
_SYMLINK_MODE = "120000"
_SUBMODULE_MODE = "160000"
_MAX_RETRY_AFTER = 86_400


@dataclass(frozen=True)
class Limits:
    """Fixed budgets. Defaults are code constants; a request can never change them."""

    max_files: int = 6
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 6 * 1024 * 1024
    max_commit_json_bytes: int = 256 * 1024
    max_tree_json_bytes: int = 1024 * 1024
    max_requests: int = 40  # 6 files across several directories, plus selection reads
    socket_timeout_seconds: float = 10.0
    deadline_seconds: float = 60.0


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]  # lower-case names; only a small reviewed subset is kept
    body: bytes  # already capped by the transport


class Transport(Protocol):
    def get(
        self, path: str, accept: str, max_bytes: int, deadline: float
    ) -> TransportResponse:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class FetchedFile:
    path: str
    mode: str
    blob_sha: str
    size: int
    data: bytes


@dataclass(frozen=True)
class FetchedSnapshot:
    commit_sha: str
    tree_sha: str
    files: tuple[FetchedFile, ...]


class _Payload(BaseModel):
    # Provider payloads carry many fields we do not use, so unknown keys are ignored here
    # (the repo's own models forbid them). Every field we DO use is strictly typed.
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class _TreeRef(_Payload):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class _CommitPayload(_Payload):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: _TreeRef


class _TreeEntry(_Payload):
    path: str
    mode: str
    type: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int | None = Field(default=None, ge=0)


class _TreePayload(_Payload):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    truncated: bool
    tree: list[_TreeEntry]


class _Budget:
    """The one choke point for requests: count and deadline are enforced here, not by callers."""

    def __init__(self, transport: Transport, limits: Limits, clock: Callable[[], float]) -> None:
        self._transport = transport
        self._limits = limits
        self._clock = clock
        self._deadline = clock() + limits.deadline_seconds
        self._requests = 0

    def get(self, path: str, accept: str, max_bytes: int) -> TransportResponse:
        if self._clock() >= self._deadline:
            raise IngestionError(IngestErrorCode.TIMEOUT)
        if self._requests >= self._limits.max_requests:
            raise IngestionError(IngestErrorCode.REQUEST_BUDGET_EXCEEDED)
        self._requests += 1
        return self._transport.get(path, accept, max_bytes, self._deadline)


class GithubReader:
    def __init__(
        self,
        transport: Transport,
        limits: Limits = DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._limits = limits
        self._clock = clock

    def fetch(self, request: IngestionRequest) -> FetchedSnapshot:
        """All requested files at the pinned commit, or an IngestionError. Never partial."""
        if len(request.paths) > self._limits.max_files:
            raise IngestionError(IngestErrorCode.REQUEST_INVALID)
        budget = _Budget(self._transport, self._limits, self._clock)
        base = f"/repos/{request.owner}/{request.repo}/git"

        commit = self._json(
            budget,
            f"{base}/commits/{request.revision_sha}",
            _CommitPayload,
            self._limits.max_commit_json_bytes,
        )
        if commit.sha != request.revision_sha:
            raise IngestionError(IngestErrorCode.RESPONSE_MISMATCH)

        trees: dict[str, _TreePayload] = {}
        located = [
            (path, self._locate(budget, base, commit.tree.sha, path, trees))
            for path in request.paths
        ]
        # Declared sizes are checked before any content is requested.
        if sum(entry.size or 0 for _, entry in located) > self._limits.max_total_bytes:
            raise IngestionError(IngestErrorCode.RESPONSE_TOO_LARGE)

        files = tuple(self._blob(budget, base, path, entry) for path, entry in located)
        return FetchedSnapshot(commit_sha=commit.sha, tree_sha=commit.tree.sha, files=files)

    def _locate(
        self,
        budget: _Budget,
        base: str,
        root_tree: str,
        path: str,
        trees: dict[str, _TreePayload],
    ) -> _TreeEntry:
        *directories, name = path.split("/")
        tree_sha = root_tree
        for segment in directories:
            entry = _find(self._tree(budget, base, tree_sha, trees), segment)
            _require_directory(entry)
            tree_sha = entry.sha
        entry = _find(self._tree(budget, base, tree_sha, trees), name)
        _require_regular_file(entry)
        if entry.size is None:
            raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
        if entry.size > self._limits.max_file_bytes:
            raise IngestionError(IngestErrorCode.FILE_TOO_LARGE)
        return entry

    def _tree(
        self, budget: _Budget, base: str, tree_sha: str, trees: dict[str, _TreePayload]
    ) -> _TreePayload:
        cached = trees.get(tree_sha)
        if cached is not None:
            return cached
        payload = self._json(
            budget, f"{base}/trees/{tree_sha}", _TreePayload, self._limits.max_tree_json_bytes
        )
        if payload.sha != tree_sha:
            raise IngestionError(IngestErrorCode.RESPONSE_MISMATCH)
        if payload.truncated:
            raise IngestionError(IngestErrorCode.TREE_TRUNCATED)
        trees[tree_sha] = payload
        return payload

    def _blob(self, budget: _Budget, base: str, path: str, entry: _TreeEntry) -> FetchedFile:
        size = entry.size or 0
        response = budget.get(f"{base}/blobs/{entry.sha}", RAW_ACCEPT, size)
        _raise_for_status(response)
        data = response.body
        if len(data) != size:
            raise IngestionError(IngestErrorCode.RESPONSE_MISMATCH)
        if git_blob_id(data) != entry.sha:
            raise IngestionError(IngestErrorCode.BLOB_DIGEST_MISMATCH)
        return FetchedFile(path=path, mode=entry.mode, blob_sha=entry.sha, size=size, data=data)

    def _json[M: BaseModel](self, budget: _Budget, path: str, model: type[M], max_bytes: int) -> M:
        response = budget.get(path, JSON_ACCEPT, max_bytes)
        _raise_for_status(response)
        try:
            return model.model_validate(loads_strict(response.body))
        except (StrictJsonError, ValidationError):
            raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE) from None


def git_blob_id(data: bytes) -> str:
    """Git object id of a blob: SHA-1 over `blob <size>\\0` plus the bytes (identity, not a
    security boundary of its own; Git and GitHub define it)."""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _find(listing: _TreePayload, name: str) -> _TreeEntry:
    matches = [entry for entry in listing.tree if entry.path == name]
    if len(matches) > 1:
        raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
    if not matches:
        raise IngestionError(IngestErrorCode.PATH_NOT_FOUND)
    return matches[0]


def _refuse_link_or_submodule(entry: _TreeEntry) -> None:
    if entry.mode == _SYMLINK_MODE:
        raise IngestionError(IngestErrorCode.SYMLINK_REFUSED)
    if entry.mode == _SUBMODULE_MODE or entry.type == "commit":
        raise IngestionError(IngestErrorCode.SUBMODULE_REFUSED)


def _require_directory(entry: _TreeEntry) -> None:
    _refuse_link_or_submodule(entry)
    if entry.type == "tree" and entry.mode == _TREE_MODE:
        return
    if entry.type == "tree":
        raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
    raise IngestionError(IngestErrorCode.PATH_NOT_FOUND)  # a file where a directory was asked


def _require_regular_file(entry: _TreeEntry) -> None:
    _refuse_link_or_submodule(entry)
    if entry.type == "tree":
        raise IngestionError(IngestErrorCode.NOT_A_FILE)
    if entry.type != "blob" or entry.mode not in _REGULAR_MODES:
        raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)


def _raise_for_status(response: TransportResponse) -> None:
    status = response.status
    if status == 200:
        return
    if 300 <= status < 400:
        raise IngestionError(IngestErrorCode.REDIRECT_REFUSED)
    if status == 404:
        raise IngestionError(IngestErrorCode.NOT_FOUND)
    if status == 429 or (status == 403 and _is_rate_limited(response)):
        raise IngestionError(
            IngestErrorCode.RATE_LIMITED, retry_after_seconds=_retry_after(response)
        )
    if status == 403:
        raise IngestionError(IngestErrorCode.ACCESS_DENIED)
    if 500 <= status < 600:
        raise IngestionError(IngestErrorCode.SERVER_ERROR)
    if 200 < status < 300:
        raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
    raise IngestionError(IngestErrorCode.HTTP_ERROR)


def _is_rate_limited(response: TransportResponse) -> bool:
    headers = response.headers
    if headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers:
        return True
    # Secondary limits can arrive as a bare 403 whose message names the limit (wording is
    # UNVERIFIED). The message is only classified, never stored or shown.
    try:
        document = loads_strict(response.body)
    except StrictJsonError:
        return False
    message = document.get("message") if isinstance(document, dict) else None
    return isinstance(message, str) and "rate limit" in message.casefold()


def _retry_after(response: TransportResponse) -> int | None:
    value = response.headers.get("retry-after", "")
    if value.isascii() and value.isdigit() and len(value) <= 9:
        return min(int(value), _MAX_RETRY_AFTER)
    return None
