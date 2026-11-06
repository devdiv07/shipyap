"""Offline builders for the story discovery tests.

Snapshots are real COMPLETE `ingest-github` outputs made by the Assignment 02 assembler and writer
from synthetic bytes, for any repository and revision. Nothing here touches the network. All
content is synthetic; none of it is a real research result.
"""

import hashlib
import json
from pathlib import Path

from editorial_fake import git_blob_sha

from shipyap.ingestion.assemble import build_plan
from shipyap.ingestion.github import FetchedFile, FetchedSnapshot
from shipyap.ingestion.request import IngestionRequest
from shipyap.ingestion.writer import publish_snapshot

WORKSPACE = "demo_workspace"

# An older repository: its age and last commit play no part in eligibility.
HISTORIC = "Sample-Owner/Capture-Lab"
HISTORIC_ID = HISTORIC.lower()
HISTORIC_REVISION = "1" * 40
HISTORIC_FILES = {
    "README.md": (
        b"# Screenshot evidence study\n"  # 1
        b"\n"  # 2
        b"A screenshot proves capture, not that the right scene was shown.\n"  # 3
        b"The scene was confirmed for 3/10 synthetic artifacts.\n"  # 4
        b"We tested one synthetic configuration only.\n"  # 5
    ),
}

ACTIVE = "acme/agent-evals"
ACTIVE_REVISION = "2" * 40
ACTIVE_FILES = {
    "NOTES.md": (
        b"# Retry budget\n"  # 1
        b"Retries are capped at 2 per task to keep runs comparable.\n"  # 2
        b"Without the cap, flaky tasks dominated wall time.\n"  # 3
        b"Model 453 was used for every run.\n"  # 4
    ),
}


def make_snapshot(
    parent: Path,
    name: str,
    repository: str = HISTORIC,
    revision: str = HISTORIC_REVISION,
    files: dict[str, bytes] | None = None,
    workspace: str = WORKSPACE,
) -> Path:
    files = HISTORIC_FILES if files is None else files
    owner, repo = repository.split("/")
    request = IngestionRequest(
        request_version="1",
        workspace_id=workspace,
        owner=owner,
        repo=repo,
        revision_sha=revision,
        paths=tuple(files),
    )
    fetched = FetchedSnapshot(
        commit_sha=revision,
        tree_sha="b" * 40,
        files=tuple(
            FetchedFile(path=p, mode="100644", blob_sha=git_blob_sha(d), size=len(d), data=d)
            for p, d in files.items()
        ),
    )
    out = parent / name
    publish_snapshot(out, build_plan(request, fetched))
    return out


def manifest_digest(snapshot: Path) -> str:
    return hashlib.sha256((snapshot / "manifest.json").read_bytes()).hexdigest()


def ref(path: str, start: int, end: int, quote: str | None = None) -> dict:
    data: dict = {"path": path, "line_start": start, "line_end": end}
    if quote is not None:
        data["quote"] = quote
    return data


def historic_finding(**overrides) -> dict:
    data = {
        "story_key": "capture-is-not-scene",
        "angle": "A screenshot proves capture, not that the right scene was shown.",
        "support": "The README says so directly and reports the scene confirmed for 3/10.",
        "source_refs": [
            ref(
                "README.md",
                3,
                4,
                "not that the right scene was shown. The scene was confirmed for 3/10",
            )
        ],
    }
    data.update(overrides)
    return data


def active_finding(**overrides) -> dict:
    data = {
        "story_key": "retry-cap-keeps-runs-comparable",
        "angle": "Capping retries at 2 per task kept agent evaluation runs comparable.",
        "support": "The notes state the cap and say flaky tasks dominated wall time without it.",
        "source_refs": [ref("NOTES.md", 2, 3)],
    }
    data.update(overrides)
    return data


def findings(snapshot: Path, items: list[dict], workspace: str = WORKSPACE) -> dict:
    return {
        "findings_version": "1",
        "workspace_id": workspace,
        "snapshot_manifest_sha256": manifest_digest(snapshot),
        "findings": items,
    }


def write_json(path: Path, data: dict) -> Path:
    path.write_bytes(json.dumps(data, indent=2).encode("utf-8"))
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
