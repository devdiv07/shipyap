import copy

import pytest
from pydantic import ValidationError

from public_research_os.evidence.manifest import SourceManifest


def test_valid_manifest(manifest_data):
    manifest = SourceManifest.model_validate(manifest_data)
    assert manifest.file_for("results.json") is not None
    assert manifest.file_for("Results.json") is None  # lookup is exact and case-sensitive


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "A" * 64),  # uppercase
        ("sha256", "a" * 63),  # short
        ("sha256", "g" * 64),  # non-hex
        ("sha256", "a" * 64 + "\n"),
        ("path", "../escape.json"),
        ("path", "/etc/passwd"),
        ("path", "C:/x.json"),
        ("path", "CON"),
        ("path", "x."),
    ],
)
def test_malformed_file_entry_rejected(manifest_data, field, value):
    bad = copy.deepcopy(manifest_data)
    bad["files"][0][field] = value
    with pytest.raises(ValidationError):
        SourceManifest.model_validate(bad)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_sha", "a" * 39),
        ("revision_sha", "A" * 40),
        ("revision_sha", "a" * 64),  # SHA-256 object format is out of scope for v1
        ("manifest_version", "2"),
        ("workspace_id", "Demo Workspace"),
        ("source_url", "http://example.org/synthetic"),
        ("source_url", "https://user:SENTINELtok@example.org/synthetic"),
        ("source_url", "https://example.org/synthetic?token=SENTINEL"),
        ("source_url", "https://example.org/synthetic#SENTINEL"),
        ("files", []),
    ],
)
def test_malformed_manifest_rejected(manifest_data, field, value):
    bad = copy.deepcopy(manifest_data)
    bad[field] = value
    with pytest.raises(ValidationError):
        SourceManifest.model_validate(bad)


@pytest.mark.parametrize("second", ["results.json", "Results.json", "RESULTS.JSON"])
def test_duplicate_and_case_colliding_paths_rejected(manifest_data, second):
    bad = copy.deepcopy(manifest_data)
    bad["files"].append({"path": second, "sha256": "b" * 64})
    with pytest.raises(ValidationError, match="unique"):
        SourceManifest.model_validate(bad)


def test_extra_fields_rejected(manifest_data):
    bad = copy.deepcopy(manifest_data)
    bad["verified"] = True
    with pytest.raises(ValidationError):
        SourceManifest.model_validate(bad)
