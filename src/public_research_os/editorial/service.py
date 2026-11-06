"""Orchestration of `draft-brief` and `draft-build`.

Order matters. The destination is checked first and every input is verified before anything is
written. A structural problem with an input (incomplete snapshot or brief, malformed
contribution or response, wrong brief) raises EditorialError and writes nothing. A content
problem in a well-formed response (an unlocated quote, an unsupported number, ...) is not an
error: it produces a NEEDS_REVISION bundle, so the reviewer can see exactly what failed.

No step approves anything, contacts a network or calls a model. The drafter response is
produced outside this module: by a person, or by `draft-model` (`drafting/`), which hands its
response bytes to `build_from_response`, the same path `draft-build` uses.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from public_research_os.editorial.brief import (
    build_brief,
    render_brief_json,
    require_same_snapshot,
)
from public_research_os.editorial.bundle import (
    BundleIncomplete,
    preflight_out,
    publish_bundle,
    verify_bundle,
)
from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.grounding import check_draft, render_post
from public_research_os.editorial.inputs import (
    CONTRIBUTION_CAP_BYTES,
    RESPONSE_CAP_BYTES,
    parse_brief,
    parse_contribution,
    parse_response,
)
from public_research_os.editorial.models import Brief, BundleKind, Counts, DraftStatus
from public_research_os.editorial.render import render_brief_md, render_review
from public_research_os.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from public_research_os.evidence.report import canonical_json
from public_research_os.evidence.snapshot import read_input
from public_research_os.evidence.validation import UnreadableInput


@dataclass(frozen=True)
class BriefResult:
    brief_sha256: str
    source_count: int


@dataclass(frozen=True)
class BuildResult:
    draft_status: DraftStatus
    counts: Counts
    post_chars: int


@dataclass(frozen=True)
class PreparedBrief:
    """A verified snapshot and a complete brief bundle built from it. `brief_json` and `brief_md`
    are exactly the bytes whose digests matched the bundle record."""

    snapshot: VerifiedSnapshot
    brief: Brief
    brief_json: bytes
    brief_md: bytes
    brief_sha256: str


def run_brief(snapshot_dir: Path, contribution_path: Path, out: Path) -> BriefResult:
    preflight_out(out)
    snapshot = load_verified_snapshot(snapshot_dir)
    raw = read_input(contribution_path, CONTRIBUTION_CAP_BYTES)
    contribution = parse_contribution(raw)
    brief = build_brief(snapshot, contribution, _sha256(_parsed_bytes(raw)))
    brief_json = render_brief_json(brief)
    brief_sha256 = _sha256(brief_json)
    publish_bundle(
        out,
        BundleKind.BRIEF,
        {
            "brief.json": brief_json,
            "brief.md": render_brief_md(brief, brief_sha256).encode("utf-8"),
        },
        workspace_id=brief.workspace_id,
        snapshot_manifest_sha256=snapshot.manifest_sha256,
        brief_sha256=brief_sha256,
    )
    return BriefResult(brief_sha256=brief_sha256, source_count=len(brief.sources))


def run_build(snapshot_dir: Path, brief_dir: Path, response_path: Path, out: Path) -> BuildResult:
    preflight_out(out)
    prepared = prepare_build(snapshot_dir, brief_dir)
    return build_from_response(prepared, read_input(response_path, RESPONSE_CAP_BYTES), out)


def prepare_build(snapshot_dir: Path, brief_dir: Path) -> PreparedBrief:
    """Verify the snapshot, then the brief bundle, and require that the brief was built from that
    snapshot. Writes nothing; `--out` is the caller's to preflight first."""
    snapshot = load_verified_snapshot(snapshot_dir)
    try:
        bundle = verify_bundle(brief_dir, BundleKind.BRIEF)
    except BundleIncomplete:
        raise EditorialError(EditorialErrorCode.BRIEF_INCOMPLETE) from None
    brief_json = bundle.files["brief.json"]
    brief_sha256 = _sha256(brief_json)
    brief = parse_brief(brief_json)
    if bundle.record.brief_sha256 != brief_sha256:
        raise EditorialError(EditorialErrorCode.BRIEF_INCOMPLETE)
    require_same_snapshot(brief, snapshot)
    return PreparedBrief(
        snapshot=snapshot,
        brief=brief,
        brief_json=brief_json,
        brief_md=bundle.files["brief.md"],
        brief_sha256=brief_sha256,
    )


def build_from_response(
    prepared: PreparedBrief, raw: bytes | UnreadableInput, out: Path
) -> BuildResult:
    """Strictly parse a drafter response, check it against the prepared brief and publish the draft
    bundle. The only path from response bytes to a bundle, whoever wrote the response."""
    brief, brief_json, brief_sha256 = prepared.brief, prepared.brief_json, prepared.brief_sha256
    response = parse_response(raw)
    raw = _parsed_bytes(raw)
    if response.brief_sha256 != brief_sha256:
        raise EditorialError(EditorialErrorCode.RESPONSE_BRIEF_MISMATCH)

    response_sha256 = _sha256(raw)
    amap = check_draft(brief, response, brief_sha256, response_sha256)
    post = render_post(response.segments)
    publish_bundle(
        out,
        BundleKind.DRAFT,
        {
            "assertion-map.json": canonical_json(amap.model_dump(mode="json")).encode("utf-8"),
            "brief.json": brief_json,
            "draft.txt": (post + "\n").encode("utf-8") if post else b"",
            "response.json": raw,
            "review.md": render_review(amap, brief, response).encode("utf-8"),
        },
        workspace_id=brief.workspace_id,
        snapshot_manifest_sha256=prepared.snapshot.manifest_sha256,
        brief_sha256=brief_sha256,
        response_sha256=response_sha256,
        draft_status=amap.draft_status,
        generator=response.generator,
    )
    return BuildResult(
        draft_status=amap.draft_status, counts=amap.counts, post_chars=amap.post_chars
    )


def _parsed_bytes(raw: bytes | UnreadableInput) -> bytes:
    if isinstance(raw, UnreadableInput):  # unreachable: the parsers refuse unreadable input
        raise RuntimeError("unreadable input reached a parsed document")
    return raw


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
