"""Pinned source manifest, version 1.

Scope: a v1 manifest describes exactly ONE repository (canonical HTTPS URL) at ONE pinned
revision, plus selected repository-relative files and their expected SHA-256 digests.
It is an ASSERTION made by whoever wrote it, not third-party verification: the URL is never
fetched, and a matching digest proves byte equality with a local snapshot only, never
scientific truth or human approval. Multi-repository manifests are out of scope.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from public_research_os.evidence.policy import (
    PATH_MESSAGES,
    URL_MESSAGES,
    check_relative_path,
    check_source_url,
)


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManifestFile(FrozenStrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def portable_relative_path(cls, value: str) -> str:
        violation = check_relative_path(value)
        if violation is not None:
            raise ValueError(PATH_MESSAGES[violation])
        return value


class SourceManifest(FrozenStrictModel):
    manifest_version: Literal["1"]
    # Same pattern as Claim.workspace_id. A contract field only, not authorization.
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    source_url: HttpUrl
    revision_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[ManifestFile, ...] = Field(min_length=1, max_length=1000)

    @field_validator("source_url")
    @classmethod
    def plain_https_url(cls, value: HttpUrl) -> HttpUrl:
        violation = check_source_url(value)
        if violation is not None:
            raise ValueError(URL_MESSAGES[violation])
        return value

    @field_validator("files")
    @classmethod
    def unique_paths(cls, files: tuple[ManifestFile, ...]) -> tuple[ManifestFile, ...]:
        # casefold() also rejects paths that collide on case-insensitive filesystems.
        folded = [entry.path.casefold() for entry in files]
        if len(set(folded)) != len(folded):
            raise ValueError("manifest file paths must be unique, ignoring case")
        return files

    def file_for(self, path: str) -> ManifestFile | None:
        """Exact, case-sensitive lookup."""
        for entry in self.files:
            if entry.path == path:
                return entry
        return None
