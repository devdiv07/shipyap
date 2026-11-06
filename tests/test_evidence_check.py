import copy
import hashlib
import json
import os
import sys
from functools import partial
from pathlib import Path

import pytest
from pydantic import ValidationError

from public_research_os.evidence import snapshot
from public_research_os.evidence.report import (
    NOTICES,
    ErrorCode,
    EvidenceLabel,
    GateResult,
    Outcome,
    SourceReason,
    ValidationReport,
    render_json,
    render_record,
    render_text,
)
from public_research_os.evidence.snapshot import observe_digest
from public_research_os.evidence.validation import (
    DigestObservation,
    EvidenceCheck,
    UnreadableInput,
    check_evidence,
)

L = EvidenceLabel


def run(claim_data, manifest_data, root: Path | None, **observe_kwargs) -> EvidenceCheck:
    observer = partial(observe_digest, root, **observe_kwargs) if root is not None else None
    return check_evidence(
        json.dumps(claim_data).encode(), json.dumps(manifest_data).encode(), observer
    )


def rendered(check: EvidenceCheck) -> str:
    parts = [render_json(check.report), render_text(check.report)]
    if check.record is not None:
        parts.append(render_record(check.record))
    return "\n".join(parts)


def only_source(check: EvidenceCheck):
    assert len(check.report.sources) == 1
    return check.report.sources[0]


def error_codes(check: EvidenceCheck) -> set[ErrorCode]:
    return {error.code for error in check.report.errors}


# --- happy path -----------------------------------------------------------------------


def test_approved_claim_with_matching_digest_passes_mechanical_gate(
    claim_data, manifest_data, snapshot_root
):
    check = run(claim_data, manifest_data, snapshot_root)
    report = check.report
    assert report.outcome == Outcome.CHECKED
    assert report.labels == (L.VALID_STRUCTURE, L.SOURCE_MATCH, L.UNKNOWN_SEMANTIC_SUPPORT)
    assert report.mechanical_gate == GateResult.PASS
    source = only_source(check)
    assert source.reason == SourceReason.DIGEST_MATCH
    assert source.observed_sha256 == source.expected_sha256
    assert check.record is not None
    assert check.record.review_state == "PENDING_HUMAN_REVIEW"
    assert check.record.semantic_support == "UNKNOWN"


def test_label_set_is_exactly_the_six_contract_labels():
    assert {label.value for label in EvidenceLabel} == {
        "VALID_STRUCTURE",
        "SOURCE_MATCH",
        "SOURCE_MISMATCH",
        "MISSING_SOURCE",
        "NOT_APPROVED",
        "UNKNOWN_SEMANTIC_SUPPORT",
    }
    assert "VERIFIED" not in EvidenceLabel.__members__


# --- provenance mismatches --------------------------------------------------------------


def test_wrong_digest_is_source_mismatch(claim_data, manifest_data, snapshot_root):
    manifest_data["files"][0]["sha256"] = "0" * 64
    check = run(claim_data, manifest_data, snapshot_root)
    source = only_source(check)
    assert (source.label, source.reason) == (L.SOURCE_MISMATCH, SourceReason.DIGEST_MISMATCH)
    assert source.observed_sha256 is not None
    assert check.report.mechanical_gate == GateResult.FAIL


def test_wrong_revision_is_source_mismatch_and_not_hashed(claim_data, manifest_data, snapshot_root):
    manifest_data["revision_sha"] = "b" * 40
    check = run(claim_data, manifest_data, snapshot_root)
    source = only_source(check)
    assert (source.label, source.reason) == (L.SOURCE_MISMATCH, SourceReason.REVISION_MISMATCH)
    assert source.observed_sha256 is None
    assert check.report.mechanical_gate == GateResult.FAIL


def test_uppercase_claim_revision_matches_lowercase_manifest(
    claim_data, manifest_data, snapshot_root
):
    claim_data["sources"][0]["revision_sha"] = "A" * 40
    assert only_source(run(claim_data, manifest_data, snapshot_root)).label == L.SOURCE_MATCH


def test_other_repository_fails_closed_as_unsupported(claim_data, manifest_data, snapshot_root):
    claim_data["sources"][0]["url"] = "https://example.org/another-repo"
    check = run(claim_data, manifest_data, snapshot_root)
    source = only_source(check)
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.UNSUPPORTED_REPOSITORY)
    assert "single repository" in source.diagnostic
    assert source.observed_sha256 is None
    assert check.report.mechanical_gate == GateResult.FAIL


def test_one_supported_and_one_unsupported_source_still_fails(
    claim_data, manifest_data, snapshot_root
):
    extra = dict(claim_data["sources"][0], url="https://example.org/another-repo")
    claim_data["sources"].append(extra)
    report = run(claim_data, manifest_data, snapshot_root).report
    assert [s.label for s in report.sources] == [L.SOURCE_MATCH, L.MISSING_SOURCE]
    assert L.SOURCE_MATCH in report.labels and L.MISSING_SOURCE in report.labels
    assert report.mechanical_gate == GateResult.FAIL


@pytest.mark.parametrize("claim_path", ["other.json", "Results.json", "RESULTS.JSON"])
def test_path_absent_from_manifest_including_case_only_difference(
    claim_data, manifest_data, snapshot_root, claim_path
):
    claim_data["sources"][0]["path"] = claim_path
    source = only_source(run(claim_data, manifest_data, snapshot_root))
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.PATH_NOT_IN_MANIFEST)


def test_workspace_mismatch_rejected_without_any_file_access(claim_data, manifest_data):
    manifest_data["workspace_id"] = "other_workspace"
    calls = []

    def spy(path: str) -> DigestObservation:
        calls.append(path)
        raise AssertionError("snapshot must not be read for a cross-workspace input")

    check = check_evidence(json.dumps(claim_data).encode(), json.dumps(manifest_data).encode(), spy)
    assert calls == []
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) == {ErrorCode.WORKSPACE_MISMATCH}
    assert L.VALID_STRUCTURE not in check.report.labels
    assert check.report.sources == ()
    assert check.record is None
    assert "other_workspace" not in rendered(check)


# --- missing evidence fails closed -------------------------------------------------------


def test_missing_file_on_disk(claim_data, manifest_data, snapshot_root):
    (snapshot_root / "results.json").unlink()
    source = only_source(run(claim_data, manifest_data, snapshot_root))
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.FILE_NOT_FOUND)


def test_no_source_root_fails_closed(claim_data, manifest_data):
    check = run(claim_data, manifest_data, None)
    source = only_source(check)
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.NO_SOURCE_ROOT)
    assert check.report.mechanical_gate == GateResult.FAIL


def test_directory_at_source_path(claim_data, manifest_data, snapshot_root):
    (snapshot_root / "results.json").unlink()
    (snapshot_root / "results.json").mkdir()
    source = only_source(run(claim_data, manifest_data, snapshot_root))
    assert source.reason == SourceReason.NOT_A_REGULAR_FILE


def test_file_over_size_cap(claim_data, manifest_data, snapshot_root):
    source = only_source(run(claim_data, manifest_data, snapshot_root, cap=4))
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.FILE_TOO_LARGE)
    assert source.observed_sha256 is None


def test_file_replaced_during_check_is_detected(
    claim_data, manifest_data, snapshot_root, monkeypatch
):
    """Simulates a snapshot swap between hashing and the post-hash re-check."""
    original = snapshot._hash_open_file
    target = snapshot_root / "results.json"

    def hash_then_swap(*args, **kwargs):
        observation = original(*args, **kwargs)
        replacement = snapshot_root / "replacement.tmp"
        replacement.write_bytes(b'{"passed": 9, "total": 10}')
        os.replace(replacement, target)
        return observation

    monkeypatch.setattr(snapshot, "_hash_open_file", hash_then_swap)
    source = only_source(run(claim_data, manifest_data, snapshot_root))
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.SOURCE_CHANGED)
    assert source.observed_sha256 is None


# --- source descriptor read/close failures (Codex R1 P2-01) -----------------------------

FAULT_SENTINEL = "SENTINEL_PRIVATE_SOURCE"


class DescriptorFaults:
    """Wraps the real os.open/os.read/os.close used by snapshot.py. Only descriptors opened
    on `target` are affected, and the real call always runs first, so the production
    descriptor lifecycle executes unchanged; faults are injected on top of it."""

    def __init__(self, monkeypatch, target: Path, *, fail_read=False, fail_close=False):
        self.opened: list[int] = []
        self.closes: list[int] = []
        live: set[int] = set()  # descriptor numbers can be reused after a close
        wanted = os.path.normcase(os.path.realpath(target))
        real_open, real_read, real_close = os.open, os.read, os.close

        def tracking_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if os.path.normcase(os.path.realpath(path)) == wanted:
                self.opened.append(descriptor)
                live.add(descriptor)
            return descriptor

        def faulty_read(descriptor, size):
            if fail_read and descriptor in live:
                raise OSError(5, "synthetic read failure", FAULT_SENTINEL)
            return real_read(descriptor, size)

        def faulty_close(descriptor):
            ours = descriptor in live or descriptor in self.opened
            live.discard(descriptor)
            if ours:  # recorded before the real call, so a double close is counted too
                self.closes.append(descriptor)
            real_close(descriptor)  # really released before any synthetic failure
            if ours and fail_close:
                raise OSError(5, "synthetic close failure", FAULT_SENTINEL)

        monkeypatch.setattr(snapshot.os, "open", tracking_open)
        monkeypatch.setattr(snapshot.os, "read", faulty_read)
        monkeypatch.setattr(snapshot.os, "close", faulty_close)

    def assert_each_closed_exactly_once(self) -> None:
        assert len(self.opened) == 1
        assert self.closes == self.opened  # one close per open: no leak, no double close
        for descriptor in self.opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)


@pytest.mark.parametrize(
    ("fail_read", "fail_close", "reason"),
    [
        (False, False, SourceReason.DIGEST_MATCH),
        (False, True, SourceReason.SOURCE_CLOSE_FAILED),
        (True, False, SourceReason.SOURCE_UNREADABLE),
        (True, True, SourceReason.SOURCE_UNREADABLE),  # primary failure is preserved
    ],
    ids=["read-ok-close-ok", "read-ok-close-fails", "read-fails-close-ok", "both-fail"],
)
def test_source_descriptor_lifecycle(
    claim_data, manifest_data, snapshot_root, monkeypatch, fail_read, fail_close, reason
):
    faults = DescriptorFaults(
        monkeypatch, snapshot_root / "results.json", fail_read=fail_read, fail_close=fail_close
    )
    check = run(claim_data, manifest_data, snapshot_root)
    faults.assert_each_closed_exactly_once()
    source = only_source(check)
    assert source.reason == reason
    assert FAULT_SENTINEL not in rendered(check)
    if reason == SourceReason.DIGEST_MATCH:
        assert check.report.mechanical_gate == GateResult.PASS
        return
    assert (source.label, source.observed_sha256) == (L.MISSING_SOURCE, None)
    assert L.SOURCE_MATCH not in check.report.labels
    assert check.report.mechanical_gate == GateResult.FAIL
    assert check.record is not None and check.record.mechanical_gate == GateResult.FAIL


def test_identity_failure_is_primary_when_close_also_fails(
    claim_data, manifest_data, snapshot_root, monkeypatch
):
    """The file grows between the pre-open stat and fstat (SOURCE_CHANGED), then close fails."""
    target = snapshot_root / "results.json"
    faults = DescriptorFaults(monkeypatch, target, fail_close=True)
    tracking_open = snapshot.os.open
    wanted = os.path.normcase(os.path.realpath(target))

    def grow_then_open(path, flags, *args, **kwargs):
        if os.path.normcase(os.path.realpath(path)) == wanted:
            with open(target, "ab") as handle:
                handle.write(b" ")
        return tracking_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(snapshot.os, "open", grow_then_open)
    check = run(claim_data, manifest_data, snapshot_root)
    faults.assert_each_closed_exactly_once()
    assert only_source(check).reason == SourceReason.SOURCE_CHANGED
    assert check.report.mechanical_gate == GateResult.FAIL


def test_close_failure_shared_by_repeated_path_never_passes(
    claim_data, manifest_data, snapshot_root, monkeypatch
):
    claim_data["sources"].append(dict(claim_data["sources"][0]))
    faults = DescriptorFaults(monkeypatch, snapshot_root / "results.json", fail_close=True)
    report = run(claim_data, manifest_data, snapshot_root).report
    faults.assert_each_closed_exactly_once()  # the path is observed once and cached
    assert [s.reason for s in report.sources] == [SourceReason.SOURCE_CLOSE_FAILED] * 2
    assert report.mechanical_gate == GateResult.FAIL


def test_unexpected_close_exception_is_not_swallowed(snapshot_root, monkeypatch):
    """Only OSError is a close failure; other exceptions are defects and propagate."""
    real_close = os.close

    def close_then_defect(descriptor):
        real_close(descriptor)
        raise RuntimeError("synthetic defect")

    monkeypatch.setattr(snapshot.os, "close", close_then_defect)
    with pytest.raises(RuntimeError):
        observe_digest(snapshot_root, "results.json")


# --- lone Unicode surrogates (Codex R1 P2-02) -------------------------------------------

SURROGATE_MARK = "SURROGATE-MARK"


def with_escape(document: dict, escape: str) -> bytes:
    """Serialize, then swap the marker for a raw JSON escape such as \\ud800."""
    text = json.dumps(document)
    assert SURROGATE_MARK in text
    return text.replace(SURROGATE_MARK, escape).encode()


CLAIM_SURROGATE_SITES = {
    "limitations": lambda c: c.update(limitations=[f"synthetic {SURROGATE_MARK}"]),
    "limitations-second-item": lambda c: c["limitations"].append(SURROGATE_MARK),
    "forbidden_interpretations": lambda c: c.update(
        forbidden_interpretations=[f"synthetic {SURROGATE_MARK}"]
    ),
    "statement": lambda c: c.update(statement=f"Synthetic statement {SURROGATE_MARK}"),
    "nested-dict-value": lambda c: c["metric"].update(unit=f"runs {SURROGATE_MARK}"),
    "array-of-dicts-value": lambda c: c["sources"][0].update(path=f"r{SURROGATE_MARK}.json"),
    "dict-key": lambda c: c.update({f"k{SURROGATE_MARK}": "v"}),
    "nested-dict-key": lambda c: c["metric"].update({SURROGATE_MARK: 1}),
    "nested-arrays": lambda c: c.update(extra=[["ok", [SURROGATE_MARK]]]),
}
MANIFEST_SURROGATE_SITES = {
    "files.path": lambda m: m["files"][0].update(path=f"r{SURROGATE_MARK}.json"),
    "source_url": lambda m: m.update(source_url=f"https://example.org/{SURROGATE_MARK}"),
    "workspace_id": lambda m: m.update(workspace_id=SURROGATE_MARK),
    "dict-key": lambda m: m.update({SURROGATE_MARK: 1}),
}


def assert_invalid_unicode(check: EvidenceCheck, which: str) -> None:
    assert check.report.outcome == Outcome.REJECTED
    assert check.report.mechanical_gate == GateResult.FAIL
    assert check.record is None
    assert check.report.sources == ()
    (error,) = check.report.errors
    assert (error.input, error.code, error.location) == (
        which,
        ErrorCode.INVALID_UNICODE,
        "<document>",
    )
    output = rendered(check)
    assert "synthetic" not in output and "\\ud" not in output


@pytest.mark.parametrize("escape", ["\\ud800", "\\udc00"], ids=["high", "low"])
@pytest.mark.parametrize("site", list(CLAIM_SURROGATE_SITES))
def test_claim_lone_surrogate_rejected(claim_data, manifest_data, snapshot_root, site, escape):
    CLAIM_SURROGATE_SITES[site](claim_data)
    observed = []

    def spy(path: str) -> DigestObservation:
        observed.append(path)
        return observe_digest(snapshot_root, path)

    check = check_evidence(with_escape(claim_data, escape), json.dumps(manifest_data).encode(), spy)
    assert_invalid_unicode(check, "claim")
    assert observed == []  # rejected before any snapshot read


@pytest.mark.parametrize("escape", ["\\ud800", "\\udc00"], ids=["high", "low"])
@pytest.mark.parametrize("site", list(MANIFEST_SURROGATE_SITES))
def test_manifest_lone_surrogate_rejected(claim_data, manifest_data, snapshot_root, site, escape):
    MANIFEST_SURROGATE_SITES[site](manifest_data)
    check = check_evidence(
        json.dumps(claim_data).encode(),
        with_escape(manifest_data, escape),
        partial(observe_digest, snapshot_root),
    )
    assert_invalid_unicode(check, "manifest")


def test_audit_reproduction_surrogate_in_approved_claim(claim_data, manifest_data, snapshot_root):
    """Codex R1 P2-02: `"limitations": ["synthetic \\ud800"]` produced a mechanical PASS."""
    text = json.dumps(claim_data).replace(
        json.dumps(claim_data["limitations"]), '["synthetic \\ud800"]'
    )
    assert '"limitations": ["synthetic \\ud800"]' in text
    check = check_evidence(
        text.encode(), json.dumps(manifest_data).encode(), partial(observe_digest, snapshot_root)
    )
    assert_invalid_unicode(check, "claim")


@pytest.mark.parametrize(
    "value",
    [
        "Synthetic \\ud83d\\ude00 escaped pair",
        "Synthetic \U0001f600 raw emoji",
        "Synthetic \U0001d11e supplementary plane",
        "Synthetique données 中文",
    ],
    ids=["escaped-pair", "emoji", "supplementary", "accents-cjk"],
)
def test_valid_unicode_in_free_text_still_passes(claim_data, manifest_data, snapshot_root, value):
    claim_data["limitations"] = [SURROGATE_MARK]
    claim_data["forbidden_interpretations"] = [SURROGATE_MARK]
    claim_data["statement"] = f"Exactly 3 of 10 runs: {SURROGATE_MARK}"
    text = json.dumps(claim_data, ensure_ascii=False).replace(SURROGATE_MARK, value)
    check = check_evidence(
        text.encode("utf-8"),
        json.dumps(manifest_data).encode(),
        partial(observe_digest, snapshot_root),
    )
    assert check.report.errors == ()
    assert check.report.mechanical_gate == GateResult.PASS
    assert check.record is not None
    assert all(ord(ch) < 128 for ch in render_text(check.report))  # ASCII-only output
    if "\\u" in value:
        assert check.record.claim.limitations == ["Synthetic \U0001f600 escaped pair"]


# --- invalid input -----------------------------------------------------------------------


@pytest.mark.parametrize("which", ["claim", "manifest"])
def test_invalid_json_rejected(claim_data, manifest_data, which):
    good = {
        "claim": json.dumps(claim_data).encode(),
        "manifest": json.dumps(manifest_data).encode(),
    }
    good[which] = b'{"workspace_id": "demo_workspace",'
    check = check_evidence(good["claim"], good["manifest"], None)
    assert check.report.outcome == Outcome.REJECTED
    assert ErrorCode.INVALID_JSON in error_codes(check)
    assert {e.input for e in check.report.errors} == {which}


def test_invalid_utf8_rejected(manifest_data):
    check = check_evidence(b"\xff\xfe{}", json.dumps(manifest_data).encode(), None)
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) == {ErrorCode.INVALID_JSON}


# --- duplicate JSON keys (Codex P2-03) ------------------------------------------------


def with_duplicate(document: dict, path: tuple, key: str, earlier_value) -> bytes:
    """Serialize `document`, inserting `key: earlier_value` just before the real `key`
    in the object at `path`. Last-wins parsers would silently keep the real value."""
    marker = "__DUPLICATE_MARKER__"
    target = document
    for step in path:
        target = target[step]
    target[marker] = None
    text = json.dumps(document)
    original = json.dumps(target[key])
    target.pop(marker)
    return text.replace(
        f'"{marker}": null', f'"{key}": {json.dumps(earlier_value)}, "{key}": {original}'
    ).encode()


CLAIM_DUPLICATES = [
    ((), "status", "candidate"),
    ((), "workspace_id", "other_workspace"),
    ((), "claim_id", "OTHER-001"),
    (("sources", 0), "path", "other.json"),
    (("sources", 0), "revision_sha", "b" * 40),
    (("sources", 0), "url", "https://example.org/other"),
    (("metric",), "numerator", 9),
]
MANIFEST_DUPLICATES = [
    ((), "workspace_id", "other_workspace"),
    ((), "revision_sha", "b" * 40),
    ((), "source_url", "https://example.org/other"),
    (("files", 0), "sha256", "0" * 64),
    (("files", 0), "path", "other.json"),
]


@pytest.mark.parametrize(("path", "key", "earlier"), CLAIM_DUPLICATES, ids=repr)
def test_duplicate_claim_key_rejected_never_passes(
    claim_data, manifest_data, snapshot_root, path, key, earlier
):
    claim_bytes = with_duplicate(claim_data, path, key, earlier)
    assert json.loads(claim_bytes) == claim_data  # stdlib last-wins would accept it as valid
    check = check_evidence(
        claim_bytes, json.dumps(manifest_data).encode(), partial(observe_digest, snapshot_root)
    )
    assert check.report.outcome == Outcome.REJECTED
    assert check.report.mechanical_gate == GateResult.FAIL
    assert check.record is None
    assert check.report.sources == ()
    (error,) = check.report.errors
    assert (error.input, error.code, error.location) == ("claim", ErrorCode.DUPLICATE_JSON_KEY, key)


@pytest.mark.parametrize(("path", "key", "earlier"), MANIFEST_DUPLICATES, ids=repr)
def test_duplicate_manifest_key_rejected_never_passes(
    claim_data, manifest_data, snapshot_root, path, key, earlier
):
    manifest_bytes = with_duplicate(manifest_data, path, key, earlier)
    assert json.loads(manifest_bytes) == manifest_data
    check = check_evidence(
        json.dumps(claim_data).encode(), manifest_bytes, partial(observe_digest, snapshot_root)
    )
    assert check.report.mechanical_gate == GateResult.FAIL
    (error,) = check.report.errors
    assert (error.input, error.code, error.location) == (
        "manifest",
        ErrorCode.DUPLICATE_JSON_KEY,
        key,
    )


def test_duplicate_status_reproduction_from_audit(claim_data, manifest_data, snapshot_root):
    """Codex P2-03: 'candidate' then 'approved' previously produced a mechanical PASS."""
    text = json.dumps(claim_data).replace(
        '"status": "approved"', '"status": "candidate", "status": "approved"'
    )
    check = check_evidence(
        text.encode(), json.dumps(manifest_data).encode(), partial(observe_digest, snapshot_root)
    )
    assert check.report.mechanical_gate == GateResult.FAIL
    assert error_codes(check) == {ErrorCode.DUPLICATE_JSON_KEY}


def test_duplicate_unknown_key_is_masked(claim_data, manifest_data):
    claim_data["sources"][0]["ghp_SENTINELdup"] = 1
    claim_bytes = with_duplicate(claim_data, ("sources", 0), "ghp_SENTINELdup", 2)
    check = check_evidence(claim_bytes, json.dumps(manifest_data).encode(), None)
    (error,) = check.report.errors
    assert (error.code, error.location) == (ErrorCode.DUPLICATE_JSON_KEY, "<extra>")
    assert "SENTINEL" not in rendered(check)


def test_duplicate_key_inside_array_of_unknown_objects(claim_data, manifest_data):
    claim_bytes = json.dumps(claim_data)[:-1].encode() + b', "extra": [{"k": 1, "k": 2}]}'
    check = check_evidence(claim_bytes, json.dumps(manifest_data).encode(), None)
    assert error_codes(check) == {ErrorCode.DUPLICATE_JSON_KEY}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_standard_numbers_rejected(claim_data, manifest_data, snapshot_root, literal):
    text = json.dumps(claim_data).replace('"numerator": 3', f'"numerator": {literal}')
    check = check_evidence(
        text.encode(), json.dumps(manifest_data).encode(), partial(observe_digest, snapshot_root)
    )
    assert error_codes(check) == {ErrorCode.INVALID_JSON}
    assert check.report.mechanical_gate == GateResult.FAIL


def test_deeply_nested_input_rejected(manifest_data):
    check = check_evidence(
        b"[" * 100_000 + b"]" * 100_000, json.dumps(manifest_data).encode(), None
    )
    assert error_codes(check) == {ErrorCode.INVALID_JSON}


@pytest.mark.parametrize("value", [True, 3.0, "3"])
def test_strict_models_still_apply_after_strict_decoding(
    claim_data, manifest_data, snapshot_root, value
):
    claim_data["metric"]["numerator"] = value
    check = run(claim_data, manifest_data, snapshot_root)
    assert error_codes(check) == {ErrorCode.INVALID_SHAPE}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("workspace_id"),
        lambda c: c.update(status="published"),
        lambda c: c.update(sources=[]),
        lambda c: c.update(limitations=["   "]),
        lambda c: c["metric"].update(numerator=11),  # numerator > denominator
        lambda c: c["metric"].update(denominator=0),
        lambda c: c["metric"].update(numerator="3"),  # strict int
        lambda c: c.update(metric=None),  # numeric claim requires metric
        lambda c: c["sources"][0].update(revision_sha="a" * 39),
    ],
    ids=[
        "missing-workspace",
        "unknown-status",
        "no-sources",
        "blank-limitations",
        "numerator-gt-denominator",
        "zero-denominator",
        "string-numerator",
        "numeric-without-metric",
        "short-revision",
    ],
)
def test_invalid_claim_shape_rejected(claim_data, manifest_data, snapshot_root, mutate):
    mutate(claim_data)
    check = run(claim_data, manifest_data, snapshot_root)
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) == {ErrorCode.INVALID_SHAPE}
    assert check.report.mechanical_gate == GateResult.FAIL
    assert check.record is None


def test_unreadable_input_reported_without_parsing(manifest_data):
    check = check_evidence(
        UnreadableInput(ErrorCode.INPUT_UNREADABLE), json.dumps(manifest_data).encode(), None
    )
    assert error_codes(check) == {ErrorCode.INPUT_UNREADABLE}
    assert check.report.claim_sha256 is None


def test_read_input_missing_directory_and_oversized(tmp_path):
    assert snapshot.read_input(tmp_path / "absent.json") == UnreadableInput(
        ErrorCode.INPUT_UNREADABLE
    )
    assert snapshot.read_input(tmp_path) == UnreadableInput(ErrorCode.INPUT_UNREADABLE)
    big = tmp_path / "big.json"
    big.write_bytes(b"x" * 11)
    assert snapshot.read_input(big, cap=10) == UnreadableInput(ErrorCode.INPUT_TOO_LARGE)


# --- path and URL abuse in claims ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../private.txt",
        "/etc/passwd",
        "a\\b",
        "C:/x.json",
        "CON",
        "nul.txt",
        "x.",
        "a//b",
        "x\x00y",
    ],
    ids=repr,
)
def test_unsafe_claim_path_rejected_and_not_echoed(claim_data, manifest_data, snapshot_root, path):
    claim_data["sources"][0]["path"] = path
    check = run(claim_data, manifest_data, snapshot_root)
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) <= {ErrorCode.INVALID_SHAPE, ErrorCode.PATH_POLICY}
    assert check.report.sources == ()
    assert check.record is None
    for error in check.report.errors:  # the rejected value is never echoed
        assert path not in error.message and path not in error.location


@pytest.mark.parametrize(
    "url",
    [
        "https://user:SENTINELtok@example.org/synthetic",
        "https://SENTINELtok@example.org/synthetic",
        "https://example.org/synthetic?token=SENTINELtok",
        "https://example.org/synthetic#SENTINELtok",
    ],
)
def test_claim_url_with_credentials_query_or_fragment_rejected(
    claim_data, manifest_data, snapshot_root, url
):
    claim_data["sources"][0]["url"] = url
    check = run(claim_data, manifest_data, snapshot_root)
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) == {ErrorCode.URL_POLICY}
    assert "SENTINELtok" not in rendered(check)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:SENTINELtok@example.org/synthetic",
        "https://example.org/synthetic?token=SENTINELtok",
        "https://example.org/synthetic#SENTINELtok",
        "http://example.org/SENTINELtok",
    ],
)
def test_manifest_url_policy_rejected_without_echo(claim_data, manifest_data, url):
    manifest_data["source_url"] = url
    check = run(claim_data, manifest_data, None)
    assert check.report.outcome == Outcome.REJECTED
    assert error_codes(check) == {ErrorCode.INVALID_SHAPE}
    assert "SENTINELtok" not in rendered(check)


def test_secret_like_extra_keys_are_masked(claim_data, manifest_data):
    claim_data["ghp_SENTINELkey"] = 1
    claim_data["sources"][0]["sk_SENTINELnested"] = 2
    check = run(claim_data, manifest_data, None)
    locations = {error.location for error in check.report.errors}
    assert locations == {"<extra>", "sources.0.<extra>"}
    assert "SENTINEL" not in rendered(check)


# --- filesystem escape -----------------------------------------------------------------


@pytest.mark.parametrize("relative", ["../outside.json", "a/../../outside.json"])
def test_observe_digest_blocks_traversal_bypassing_schema(tmp_path, relative):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.json").write_bytes(b"secret")
    assert observe_digest(root, relative) == DigestObservation(None, SourceReason.PATH_ESCAPE)


def test_observe_digest_blocks_absolute_path_bypassing_schema(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"secret")
    assert observe_digest(root, str(outside)) == DigestObservation(None, SourceReason.PATH_ESCAPE)


def _outside_secret(tmp_path: Path) -> tuple[Path, str]:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.json"
    secret.write_bytes(b'{"private": true}')
    return secret, hashlib.sha256(secret.read_bytes()).hexdigest()


def _symlink_or_skip(link: Path, target: Path, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"SECURITY TEST NOT RUN: host refused symlink creation ({type(exc).__name__})")


def _escape_case(claim_data, manifest_data, snapshot_root, relative, digest):
    """Manifest digest equals the OUTSIDE file's digest: a naive checker would say MATCH."""
    claim_data["sources"][0]["path"] = relative
    manifest_data["files"] = [{"path": relative, "sha256": digest}]
    return run(claim_data, manifest_data, snapshot_root)


def test_file_symlink_escape(claim_data, manifest_data, snapshot_root, tmp_path):
    secret, digest = _outside_secret(tmp_path)
    _symlink_or_skip(snapshot_root / "link.json", secret, is_dir=False)
    check = _escape_case(claim_data, manifest_data, snapshot_root, "link.json", digest)
    source = only_source(check)
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.PATH_ESCAPE)
    assert source.observed_sha256 is None
    assert check.report.mechanical_gate == GateResult.FAIL


def test_directory_symlink_escape(claim_data, manifest_data, snapshot_root, tmp_path):
    secret, digest = _outside_secret(tmp_path)
    _symlink_or_skip(snapshot_root / "linkdir", secret.parent, is_dir=True)
    check = _escape_case(claim_data, manifest_data, snapshot_root, "linkdir/secret.json", digest)
    assert only_source(check).reason == SourceReason.PATH_ESCAPE


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions exist only on Windows")
def test_windows_junction_escape(claim_data, manifest_data, snapshot_root, tmp_path):
    import _winapi

    secret, digest = _outside_secret(tmp_path)
    _winapi.CreateJunction(str(secret.parent), str(snapshot_root / "junction"))
    assert (snapshot_root / "junction").is_junction()
    check = _escape_case(claim_data, manifest_data, snapshot_root, "junction/secret.json", digest)
    source = only_source(check)
    assert (source.label, source.reason) == (L.MISSING_SOURCE, SourceReason.PATH_ESCAPE)
    assert source.observed_sha256 is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path normalization behavior")
def test_windows_trailing_dot_alias_cannot_reach_file(claim_data, manifest_data, snapshot_root):
    """On Windows 'results.json.' opens 'results.json'; policy must reject it first."""
    claim_data["sources"][0]["path"] = "results.json."
    manifest_data["files"][0]["path"] = "results.json."
    check = run(claim_data, manifest_data, snapshot_root)
    assert check.report.outcome == Outcome.REJECTED
    assert check.report.sources == ()


def test_symlink_inside_root_is_allowed(claim_data, manifest_data, snapshot_root):
    (snapshot_root / "data").mkdir()
    (snapshot_root / "results.json").rename(snapshot_root / "data" / "results.json")
    _symlink_or_skip(snapshot_root / "results.json", snapshot_root / "data" / "results.json", False)
    assert only_source(run(claim_data, manifest_data, snapshot_root)).label == L.SOURCE_MATCH


# --- approval status -------------------------------------------------------------------


@pytest.mark.parametrize("status", ["candidate", "withdrawn"])
def test_unapproved_claim_never_passes_even_with_matching_digest(
    claim_data, manifest_data, snapshot_root, status
):
    claim_data["status"] = status
    check = run(claim_data, manifest_data, snapshot_root)
    assert only_source(check).label == L.SOURCE_MATCH
    assert L.NOT_APPROVED in check.report.labels
    assert check.report.mechanical_gate == GateResult.FAIL
    assert check.record is not None and check.record.review_state == "PENDING_HUMAN_REVIEW"


def test_report_model_refuses_overstated_gate(claim_data, manifest_data, snapshot_root):
    claim_data["status"] = "candidate"
    data = run(claim_data, manifest_data, snapshot_root).report.model_dump()
    with pytest.raises(ValidationError, match="gate PASS"):
        ValidationReport.model_validate({**data, "mechanical_gate": GateResult.PASS})
    labels = tuple(label for label in data["labels"] if label != L.UNKNOWN_SEMANTIC_SUPPORT)
    with pytest.raises(ValidationError, match="UNKNOWN"):
        ValidationReport.model_validate({**data, "labels": labels})


# --- determinism and hygiene -----------------------------------------------------------


def test_report_serialization_is_stable(claim_data, manifest_data, snapshot_root, tmp_path):
    first = run(claim_data, manifest_data, snapshot_root)
    second = run(copy.deepcopy(claim_data), copy.deepcopy(manifest_data), snapshot_root)
    assert render_json(first.report) == render_json(second.report)
    assert render_text(first.report) == render_text(second.report)
    assert render_record(first.record) == render_record(second.record)
    payload = json.loads(render_json(first.report))
    assert render_json(first.report) == json.dumps(payload, sort_keys=True, indent=2) + "\n"
    assert payload["notices"] == list(NOTICES)
    assert payload["report_schema_version"] == "1"


def test_outputs_contain_no_local_paths_or_environment(
    claim_data, manifest_data, snapshot_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_PROVIDER_TOKEN", "sk-SENTINEL-env-value")
    output = rendered(run(claim_data, manifest_data, snapshot_root))
    assert "SENTINEL" not in output
    for local in {str(tmp_path), str(tmp_path.resolve()), tmp_path.as_posix()}:
        assert local not in output
        assert json.dumps(local)[1:-1] not in output
