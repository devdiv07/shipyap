import pytest
from publication_fake import CHANNEL, DUE, NOW
from pydantic import ValidationError

from public_research_os.publication.approval import Candidate, approval_id, confirmation_phrase
from public_research_os.publication.errors import PublicationError, PublicationErrorCode
from public_research_os.publication.models import ContentPackage
from public_research_os.publication.service import confirm


def test_fingerprint_is_stable(package):
    serialized = package.model_dump_json()
    round_trip = ContentPackage.model_validate_json(serialized)
    assert approval_id(package.posts[0].text, CHANNEL, DUE) == approval_id(
        round_trip.posts[0].text, CHANNEL, DUE
    )


def test_tampering_invalidates_fingerprint(package):
    approved = approval_id(package.posts[0].text, CHANNEL, DUE)
    tampered_post = package.posts[0].model_copy(update={"text": "Different text"})
    changed = package.model_copy(update={"posts": (tampered_post,)})
    candidate = Candidate(changed.posts[0].text, CHANNEL, DUE, changed.workspace_id, "a" * 64)
    with pytest.raises(PublicationError) as raised:
        confirm(candidate, confirmation_phrase(approved), NOW)
    assert raised.value.code == PublicationErrorCode.APPROVAL_DECLINED


def test_invalid_timezone_rejected(package):
    bad = package.model_dump(mode="json")
    bad["created_at"] = "2026-10-17T00:00:00"
    with pytest.raises(ValidationError):
        ContentPackage.model_validate(bad)


def test_media_hash_format_required(package):
    bad = package.model_dump(mode="json")
    bad["posts"][0]["media_sha256"] = ["not-a-hash"]
    with pytest.raises(ValidationError):
        ContentPackage.model_validate(bad)


def test_package_requires_expiry_after_creation(package):
    bad = package.model_dump(mode="json")
    bad["expires_at"] = bad["created_at"]
    with pytest.raises(ValidationError):
        ContentPackage.model_validate(bad)
