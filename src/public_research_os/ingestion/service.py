"""Ingestion orchestration: allowlist, destination preflight, read, assemble, publish, verify.

Order matters: the allowlist and the destination are checked BEFORE any network access, so a
refused request spends none of the (unauthenticated) rate limit. Nothing here ever approves a
claim or labels anything verified; the result only says a consistent snapshot was published.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from public_research_os.ingestion.assemble import build_plan
from public_research_os.ingestion.github import DEFAULT_LIMITS, GithubReader, Limits, Transport
from public_research_os.ingestion.request import IngestionRequest, RepoRef, require_allowed
from public_research_os.ingestion.writer import (
    preflight_destination,
    publish_snapshot,
    verify_completion,
)


@dataclass(frozen=True)
class IngestionResult:
    """A COMPLETE snapshot was published and re-verified. Not an approval of anything."""

    commit_sha: str
    file_count: int
    manifest_sha256: str


def run_ingestion(
    request: IngestionRequest,
    allowed: RepoRef,
    out: Path,
    transport: Transport,
    limits: Limits = DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> IngestionResult:
    require_allowed(request, allowed)
    preflight_destination(out)
    fetched = GithubReader(transport, limits, clock).fetch(request)
    plan = build_plan(request, fetched)
    publish_snapshot(out, plan)
    verify_completion(out)
    return IngestionResult(
        commit_sha=plan.commit_sha,
        file_count=plan.file_count,
        manifest_sha256=plan.manifest_sha256,
    )
