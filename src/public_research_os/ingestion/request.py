"""Validated ingestion request and operator allowlist. Pure: no I/O and no network.

`workspace_id` is an organizational field only; it is never authorization. The allowlist is a
separate, operator-supplied value (`--allow-repo`) so that a request file cannot widen what the
tool is willing to read. The request never carries a URL: the only fetch target is derived from
the validated `owner` and `repo`.
"""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from public_research_os.evidence.policy import PATH_MESSAGES, check_relative_path
from public_research_os.evidence.report import ErrorCode
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.evidence.validation import UnreadableInput
from public_research_os.ingestion.errors import IngestErrorCode, IngestionError

MAX_FILES = 6
MAX_PATH_SEGMENTS = 6
GITHUB_BASE_URL = "https://github.com"

# Conservative shapes; the exact-match allowlist is the real gate. A legitimate name outside
# these patterns fails closed rather than being fetched.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_OWNER_RE = re.compile(_OWNER)
_REPO_RE = re.compile(_REPO)

# Same shape as Claim.workspace_id and SourceManifest.workspace_id (tests assert the parity).
_WORKSPACE = r"^[a-z0-9][a-z0-9_-]{1,62}$"


def _repo_name_allowed(repo: str) -> bool:
    return repo not in {".", ".."} and not repo.lower().endswith(".git")


class IngestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_version: Literal["1"]
    workspace_id: str = Field(pattern=_WORKSPACE)
    owner: str = Field(pattern=f"^{_OWNER}$")
    repo: str = Field(pattern=f"^{_REPO}$")
    # Full lowercase commit id only: no branch, tag, abbreviation or other revision syntax.
    revision_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_FILES)

    @field_validator("repo")
    @classmethod
    def repo_name_is_plain(cls, value: str) -> str:
        if not _repo_name_allowed(value):
            raise ValueError("repository name is not allowed")
        return value

    @field_validator("paths")
    @classmethod
    def paths_are_safe_and_distinct(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            violation = check_relative_path(path)
            if violation is not None:
                raise ValueError(PATH_MESSAGES[violation])
            if len(path.split("/")) > MAX_PATH_SEGMENTS:
                raise ValueError("source path is nested too deeply")
        # casefold() also rejects paths that collide on case-insensitive filesystems.
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("source paths must be unique, ignoring case")
        spellings: dict[str, str] = {}
        for path in paths:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                prefix = "/".join(parts[:depth])
                if spellings.setdefault(prefix.casefold(), prefix) != prefix:
                    raise ValueError("a directory is spelled with two different cases")
        if any(path.casefold() in spellings for path in paths):
            raise ValueError("a source path is also a directory of another source path")
        return paths

    @property
    def canonical_url(self) -> str:
        """Exactly the string `SourceManifest.source_url` and claim sources must carry.

        The checker compares URLs as pydantic's normalized strings (a trailing slash or `.git`
        suffix would not match). `build_manifest` refuses a value that does not round-trip.
        """
        return f"{GITHUB_BASE_URL}/{self.owner}/{self.repo}"


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str


def parse_allow_repo(value: str) -> RepoRef:
    owner, separator, repo = value.partition("/")
    if (
        not separator
        or _OWNER_RE.fullmatch(owner) is None
        or _REPO_RE.fullmatch(repo) is None
        or not _repo_name_allowed(repo)
    ):
        raise IngestionError(IngestErrorCode.ALLOWLIST_INVALID)
    return RepoRef(owner=owner, repo=repo)


def require_allowed(request: IngestionRequest, allowed: RepoRef) -> None:
    """Exact, case-sensitive match. Allowlisting authorizes a read, not trust in the content."""
    if (request.owner, request.repo) != (allowed.owner, allowed.repo):
        raise IngestionError(IngestErrorCode.REPOSITORY_NOT_ALLOWED)


def parse_request(data: bytes | UnreadableInput) -> IngestionRequest:
    """Strict JSON, then strict model. Rejected values are never echoed."""
    if isinstance(data, UnreadableInput):
        too_large = data.code == ErrorCode.INPUT_TOO_LARGE
        raise IngestionError(
            IngestErrorCode.REQUEST_TOO_LARGE if too_large else IngestErrorCode.REQUEST_UNREADABLE
        )
    try:
        value = loads_strict(data)
    except StrictJsonError:
        raise IngestionError(IngestErrorCode.REQUEST_INVALID_JSON) from None
    try:
        return IngestionRequest.model_validate(value)
    except ValidationError as exc:
        details = exc.errors(include_url=False, include_context=False, include_input=False)
        # A rule broken by the paths that WERE given is a path-policy failure; an absent field
        # is a plain shape error.
        in_paths = any(
            detail["loc"][:1] == ("paths",) and detail["type"] != "missing" for detail in details
        )
        raise IngestionError(
            IngestErrorCode.PATH_POLICY if in_paths else IngestErrorCode.REQUEST_INVALID
        ) from None
