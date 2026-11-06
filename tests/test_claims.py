import pytest
from pydantic import ValidationError

from public_research_os.evidence.models import Claim, require_approved_claim


def test_approved_claim_passes_mechanical_gate(approved_claim):
    require_approved_claim(approved_claim)


def test_candidate_is_not_approved(approved_claim):
    candidate = approved_claim.model_copy(update={"status": "candidate"})
    with pytest.raises(ValueError, match="not approved"):
        require_approved_claim(candidate)


def test_numerator_exceeding_denominator_is_rejected(approved_claim):
    bad = approved_claim.model_dump(mode="json")
    bad["metric"]["numerator"] = 11
    with pytest.raises(ValidationError):
        Claim.model_validate(bad)


def test_numeric_claim_without_metric_is_rejected(approved_claim):
    bad = approved_claim.model_dump(mode="json")
    bad["metric"] = None
    with pytest.raises(ValidationError):
        Claim.model_validate(bad)


def test_extra_unreviewed_fields_rejected(approved_claim):
    bad = approved_claim.model_dump(mode="json")
    bad["publish_anyway"] = True
    with pytest.raises(ValidationError):
        Claim.model_validate(bad)


def test_source_path_traversal_rejected(approved_claim):
    bad = approved_claim.model_dump(mode="json")
    bad["sources"][0]["path"] = "../private.txt"
    with pytest.raises(ValidationError):
        Claim.model_validate(bad)


def test_https_required(approved_claim):
    bad = approved_claim.model_dump(mode="json")
    bad["sources"][0]["url"] = "http://example.org/synthetic"
    with pytest.raises(ValidationError):
        Claim.model_validate(bad)
