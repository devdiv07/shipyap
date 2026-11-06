"""Build the drafting brief from a verified snapshot and the author's contribution. Pure.

The brief carries everything an external drafter needs and everything `draft-build` checks
against: the snapshot identity, the full numbered text of every snapshot file, the parsed
contribution, the fixed rules and the response schema. It has no timestamps or local paths, so
the same inputs always give byte-identical brief.json and brief.md.
"""

from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.models import (
    AuthorContribution,
    Brief,
    BriefSource,
    SnapshotIdentity,
)
from public_research_os.editorial.sources import VerifiedSnapshot
from public_research_os.evidence.report import canonical_json


def check_contribution(contribution: AuthorContribution, snapshot: VerifiedSnapshot) -> None:
    if contribution.workspace_id != snapshot.workspace_id:
        raise EditorialError(EditorialErrorCode.CONTRIBUTION_WORKSPACE_MISMATCH)
    if not set(contribution.focus_paths) <= set(snapshot.paths()):
        raise EditorialError(EditorialErrorCode.CONTRIBUTION_FOCUS_PATH_UNKNOWN)


def brief_sources(snapshot: VerifiedSnapshot) -> tuple[BriefSource, ...]:
    return tuple(
        BriefSource(path=source.path, sha256=source.sha256, lines=source.lines)
        for source in snapshot.sources
    )


def build_brief(
    snapshot: VerifiedSnapshot, contribution: AuthorContribution, contribution_sha256: str
) -> Brief:
    check_contribution(contribution, snapshot)
    return Brief(
        workspace_id=snapshot.workspace_id,
        snapshot=SnapshotIdentity(
            source_url=snapshot.source_url,
            revision_sha=snapshot.revision_sha,
            tree_sha=snapshot.tree_sha,
            manifest_sha256=snapshot.manifest_sha256,
            record_sha256=snapshot.record_sha256,
        ),
        contribution_sha256=contribution_sha256,
        contribution=contribution,
        sources=brief_sources(snapshot),
    )


def require_same_snapshot(brief: Brief, snapshot: VerifiedSnapshot) -> None:
    """The verified snapshot, not the brief, is authoritative for source text: a brief whose
    identity or embedded lines differ from it was built from something else."""
    identity = (
        snapshot.source_url,
        snapshot.revision_sha,
        snapshot.tree_sha,
        snapshot.manifest_sha256,
        snapshot.record_sha256,
    )
    recorded = brief.snapshot
    if (
        identity
        != (
            recorded.source_url,
            recorded.revision_sha,
            recorded.tree_sha,
            recorded.manifest_sha256,
            recorded.record_sha256,
        )
        or brief.workspace_id != snapshot.workspace_id
        or brief.sources != brief_sources(snapshot)
    ):
        raise EditorialError(EditorialErrorCode.SNAPSHOT_MISMATCH)


def render_brief_json(brief: Brief) -> bytes:
    return canonical_json(brief.model_dump(mode="json")).encode("utf-8")
