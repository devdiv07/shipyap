import hashlib
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from public_research_os.evidence.models import Claim
from public_research_os.publication.models import ContentPackage, PostVariant

SNAPSHOT_BYTES = b'{"passed": 3, "total": 10}'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Offline guarantee for every test: any socket connect or DNS lookup fails loudly."""

    def deny(*_args, **_kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture
def snapshot_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "results.json").write_bytes(SNAPSHOT_BYTES)
    return root


@pytest.fixture
def manifest_data() -> dict:
    """Synthetic v1 manifest matching the approved_claim fixture's single source."""
    return {
        "manifest_version": "1",
        "workspace_id": "demo_workspace",
        "source_url": "https://example.org/synthetic",
        "revision_sha": "a" * 40,
        "files": [{"path": "results.json", "sha256": hashlib.sha256(SNAPSHOT_BYTES).hexdigest()}],
    }


@pytest.fixture
def claim_data(approved_claim) -> dict:
    return approved_claim.model_dump(mode="json")


@pytest.fixture
def approved_claim() -> Claim:
    return Claim.model_validate(
        {
            "workspace_id": "demo_workspace",
            "claim_id": "TEST-001",
            "kind": "numeric",
            "statement": "Exactly 3 of 10 synthetic runs passed.",
            "status": "approved",
            "sources": [
                {
                    "url": "https://example.org/synthetic",
                    "revision_sha": "a" * 40,
                    "path": "results.json",
                }
            ],
            "metric": {"numerator": 3, "denominator": 10, "unit": "runs"},
            "limitations": ["Synthetic fixture, not real world data."],
        }
    )


@pytest.fixture
def package() -> ContentPackage:
    now = datetime(2026, 10, 17, tzinfo=UTC)
    return ContentPackage(
        package_id="TEST-PACK-01",
        workspace_id="demo_workspace",
        posts=(
            PostVariant(
                channel="linkedin", text="Synthetic fixture only.", claim_ids=("TEST-001",)
            ),
        ),
        created_at=now,
        expires_at=now + timedelta(days=2),
    )
