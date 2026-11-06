"""Tests for scripts/check_expected_skips.py, using synthetic JUnit reports only.

Authentic reports are rendered from the reviewed inventory with the platform outcomes the guard
defines; every adversarial case is a mutation of one. One test runs the pinned pytest on a tiny
sample project to prove the parser accepts the XML that pytest really emits.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_expected_skips.py"
_spec = importlib.util.spec_from_file_location("check_expected_skips", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PASSED, SKIPPED = guard.PASSED, guard.SKIPPED
PLATFORMS = guard.PLATFORMS
INVENTORY, _inventory_problems = guard.load_inventory()
assert not _inventory_problems, _inventory_problems

MODULE = "tests.test_evidence_check"
SYMLINK_TESTS = (
    (MODULE, "test_file_symlink_escape"),
    (MODULE, "test_directory_symlink_escape"),
    (MODULE, "test_symlink_inside_root_is_allowed"),
)
PARAMETRIZED = sorted(i for i in INVENTORY if i[1].endswith("]"))
PLAIN = sorted(i for i in INVENTORY if i not in guard.SECURITY_TESTS and not i[1].endswith("]"))
EVERYWHERE = {platform: PASSED for platform in PLATFORMS}


def authentic(platform: str) -> list[tuple]:
    """(classname, name, outcome) for every reviewed test, as a real run on `platform` reports."""
    return [
        (*identity, guard.SECURITY_TESTS.get(identity, EVERYWHERE)[platform])
        for identity in sorted(INVENTORY)
    ]


def with_outcomes(cases: list[tuple], outcomes: dict[tuple, str]) -> list[tuple]:
    return [(c, n, outcomes.get((c, n), o)) for c, n, o in cases]


def without(cases: list[tuple], *identities: tuple) -> list[tuple]:
    return [case for case in cases if case[:2] not in identities]


def _attrs(**attributes) -> str:
    return "".join(f" {k}={quoteattr(v)}" for k, v in attributes.items() if v is not None)


def case_xml(classname, name, outcome=PASSED) -> str:
    head = f"<testcase{_attrs(classname=classname, name=name, time='0.001')}"
    if outcome == PASSED:
        return head + " />"
    if outcome in (SKIPPED, "xfail"):
        kind = "pytest.skip" if outcome == SKIPPED else "pytest.xfail"
        return head + f'><skipped type="{kind}" message="why" /></testcase>'
    if outcome == "both":
        return (
            head + '><skipped type="pytest.skip" message="a" /><failure message="b" /></testcase>'
        )
    return head + f'><{outcome} message="boom">trace</{outcome}></testcase>'


def report(
    tmp_path: Path,
    cases: list[tuple],
    *,
    suite: dict | None = None,
    root: dict | None = None,
    root_tag: str = "testsuites",
    suite_name: str | None = "pytest",
    suites: int = 1,
    name: str = "junit.xml",
) -> Path:
    """Write a pytest-shaped report. Counters are computed from the cases unless overridden."""
    counters = {
        "tests": len(cases),
        "failures": sum(c[2] == "failure" for c in cases),
        "errors": sum(c[2] == "error" for c in cases),
        "skipped": sum(c[2] in (SKIPPED, "xfail") for c in cases),
    }
    counters = {k: str(v) for k, v in counters.items()} | (suite or {})
    body = '<properties><property name="note" value="x" /></properties>'
    body += "".join(case_xml(*case) for case in cases)
    one = f"<testsuite{_attrs(name=suite_name, **counters, time='1.0')}>{body}</testsuite>"
    root_attributes = _attrs(name="pytest tests", **(root or {}))
    text = f'<?xml version="1.0" encoding="utf-8"?><{root_tag}{root_attributes}>{one * suites}'
    path = tmp_path / name
    path.write_text(text + f"</{root_tag}>", encoding="utf-8")
    return path


def problems_for(tmp_path, cases, platform, **options) -> list[str]:
    return guard.check(report(tmp_path, cases, **options), platform)[1]


def has(problems: list[str], prefix: str) -> bool:
    return any(problem.startswith(prefix) for problem in problems)


def fmt(identity: tuple) -> str:
    return guard.fmt(identity)


# --- authentic results -----------------------------------------------------------------------


def test_linux_accepts_exactly_the_windows_only_skips(tmp_path):
    lines, problems = guard.check(report(tmp_path, authentic("linux")), "linux")
    assert problems == []
    assert any(
        "skipped tests.test_evidence_check::test_windows_junction_escape: why" in x for x in lines
    )
    assert sum(line.startswith("- skipped") for line in lines) == len(guard.WINDOWS_ONLY_TESTS) == 2


def test_windows_accepts_no_skips(tmp_path):
    assert guard.check(report(tmp_path, authentic("win32")), "win32")[1] == []


def test_each_platform_is_validated_independently(tmp_path):
    windows, linux = authentic("win32"), authentic("linux")
    on_linux = problems_for(tmp_path, windows, "linux")
    on_windows = problems_for(tmp_path, linux, "win32")
    assert [p for p in on_linux if not p.startswith("Expected skip did not occur")] == []
    assert len(on_linux) == 2
    assert [p for p in on_windows if not p.startswith("Unexpected skip")] == []
    assert len(on_windows) == 2


def test_committed_inventory_is_canonical_and_covers_the_mandatory_tests():
    text = guard.INVENTORY_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text == guard.inventory_json(sorted(INVENTORY))
    assert set(guard.SECURITY_TESTS) <= INVENTORY
    assert len(INVENTORY) > 300


# --- existing guarantees, now over complete identities ------------------------------------------


def test_unexpected_skip_fails(tmp_path):
    cases = with_outcomes(authentic("win32"), {SYMLINK_TESTS[0]: SKIPPED})
    assert problems_for(tmp_path, cases, "win32") == [f"Unexpected skip: {fmt(SYMLINK_TESTS[0])}"]


def test_local_windows_host_refusing_symlinks_fails(tmp_path):
    """Hosted Windows must create symlinks; a host that refuses them is not a passing run."""
    cases = with_outcomes(authentic("win32"), dict.fromkeys(SYMLINK_TESTS, SKIPPED))
    assert problems_for(tmp_path, cases, "win32") == [
        f"Unexpected skip: {fmt(i)}" for i in sorted(SYMLINK_TESTS)
    ]


def test_linux_with_symlink_skips_fails_and_reports_missing_windows_skips(tmp_path):
    outcomes = dict.fromkeys(SYMLINK_TESTS, SKIPPED) | dict.fromkeys(
        guard.WINDOWS_ONLY_TESTS, PASSED
    )
    problems = problems_for(tmp_path, with_outcomes(authentic("linux"), outcomes), "linux")
    assert len([p for p in problems if p.startswith("Unexpected skip")]) == 3
    assert len([p for p in problems if p.startswith("Expected skip did not occur")]) == 2
    assert len(problems) == 5


def test_missing_expected_skip_fails(tmp_path):
    cases = with_outcomes(authentic("linux"), dict.fromkeys(guard.WINDOWS_ONLY_TESTS, PASSED))
    problems = problems_for(tmp_path, cases, "linux")
    assert len(problems) == len(guard.WINDOWS_ONLY_TESTS)
    assert all(p.startswith("Expected skip did not occur") for p in problems)


def test_empty_report_fails(tmp_path):
    assert "The report contains no test cases" in problems_for(tmp_path, [], "win32")


def test_unknown_platform_fails(tmp_path):
    problems = guard.check(report(tmp_path, authentic("win32")), "darwin")[1]
    assert problems == ["No expected-skip set is defined for platform 'darwin'"]


@pytest.mark.parametrize("argv", [[], ["a", "b", "c"], ["--print-inventory"]])
def test_usage_error_exit_code(argv):
    assert guard.main(argv) == 2


# --- P2-01: ambiguous identities ------------------------------------------------------------------


def test_short_name_from_an_unrelated_module_is_rejected(tmp_path):
    """Codex reproduction: the right short names from another module satisfied the old guard."""
    forged = [
        ("tests.unrelated" if (c, n) in guard.WINDOWS_ONLY_TESTS else c, n, o)
        for c, n, o in authentic("linux")
    ]
    problems = problems_for(tmp_path, forged, "linux")
    for _, name in guard.WINDOWS_ONLY_TESTS:
        assert f"Unexpected skip: tests.unrelated::{name}" in problems
        assert f"Test not in the reviewed inventory: tests.unrelated::{name}" in problems
    for identity in guard.WINDOWS_ONLY_TESTS:
        assert f"Mandatory security test missing: {fmt(identity)}" in problems


def test_duplicate_identities_are_rejected_not_overwritten(tmp_path):
    cases = authentic("linux")
    skips = [c for c in cases if c[2] == SKIPPED]
    problems = problems_for(tmp_path, cases + skips, "linux")
    assert len(problems) == 2
    assert all(p.startswith("Duplicate test identity (2 times)") for p in problems)
    passing = [c for c in cases if c[2] == PASSED][0]
    assert has(problems_for(tmp_path, cases + [passing], "linux"), "Duplicate test identity")


def test_duplicate_skips_cannot_stand_in_for_missing_tests(tmp_path):
    """Three expected skips listed twice and nothing else: the Codex duplicate reproduction."""
    skips = [c for c in authentic("linux") if c[2] == SKIPPED]
    problems = problems_for(tmp_path, skips * 2, "linux")
    assert has(problems, "Duplicate test identity")
    assert has(problems, "Test missing from the run")


def test_fabricated_parameterized_variants_are_rejected(tmp_path):
    fake = [
        (c, f"{n}[fake]", SKIPPED) if (c, n) in guard.WINDOWS_ONLY_TESTS else (c, n, o)
        for c, n, o in authentic("linux")
    ]
    problems = problems_for(tmp_path, fake, "linux")
    for module, name in guard.WINDOWS_ONLY_TESTS:
        assert f"Unexpected skip: {module}::{name}[fake]" in problems
        assert f"Mandatory security test missing: {module}::{name}" in problems


def test_parametrized_variant_is_a_different_identity_from_its_base_name(tmp_path):
    """Replaces the old 'matched by base name' test, which asserted the behaviour behind P2-01."""
    cases = without(authentic("win32"), SYMLINK_TESTS[0])
    cases.append((SYMLINK_TESTS[0][0], f"{SYMLINK_TESTS[0][1]}[a]", SKIPPED))
    problems = problems_for(tmp_path, cases, "win32")
    assert f"Unexpected skip: {fmt(SYMLINK_TESTS[0])}[a]" in problems
    assert f"Mandatory security test missing: {fmt(SYMLINK_TESTS[0])}" in problems


def test_parameter_ids_are_preserved_exactly(tmp_path):
    module, name = PARAMETRIZED[0]
    altered = name[:-1] + "x]"
    cases = [(c, altered if (c, n) == (module, name) else n, o) for c, n, o in authentic("win32")]
    problems = problems_for(tmp_path, cases, "win32")
    assert f"Test not in the reviewed inventory: {module}::{altered}" in problems
    assert f"Test missing from the run: {module}::{name}" in problems


def test_unreviewed_extra_test_is_rejected_until_the_inventory_is_updated(tmp_path):
    cases = [*authentic("win32"), ("tests.test_new_module", "test_new", PASSED)]
    assert problems_for(tmp_path, cases, "win32") == [
        "Test not in the reviewed inventory: tests.test_new_module::test_new"
    ]
    extended = INVENTORY | {("tests.test_new_module", "test_new")}
    report_path = report(tmp_path, cases)
    assert guard.check(report_path, "win32", extended)[1] == []


@pytest.mark.parametrize(
    ("classname", "name"),
    [
        pytest.param(None, "test_x", id="no-classname"),
        pytest.param("tests.test_x", None, id="no-name"),
        pytest.param("", "test_x", id="empty-classname"),
        pytest.param("tests.test_x", "", id="empty-name"),
        pytest.param("tests/test_x.py", "test_x", id="path-classname"),
        pytest.param("tests.test_x", "test x", id="space-in-name"),
        pytest.param("tests.test_x", "test_x\ntest_y", id="newline-in-name"),
        pytest.param("tests.test_x", "1test", id="digit-first"),
        pytest.param("tests.test_x", "test_x[unclosed", id="unclosed-bracket"),
        pytest.param("tests.test_x", "test_x[café]", id="non-ascii-param"),
        pytest.param("tests.test_x", "test_x[" + "a" * 5000 + "]", id="overlong-name"),
        pytest.param("tests..test_x", "test_x", id="empty-module-segment"),
    ],
)
def test_missing_or_malformed_identifiers_are_rejected(tmp_path, classname, name):
    cases = [*authentic("win32"), (classname, name, PASSED)]
    problems = problems_for(tmp_path, cases, "win32", suite={"tests": str(len(cases))})
    assert has(problems, "Malformed test case")
    assert all(len(problem) < 400 for problem in problems)  # long values are truncated in output


# --- P2-02: incomplete or inconsistent reports -------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_filtered_eleven_test_report_is_rejected(tmp_path, platform):
    """Codex reproduction: `pytest -k check_expected_skips` gave 11 passes and 300 deselected."""
    cases = [c for c in authentic(platform) if c[0] == "tests.test_check_expected_skips"]
    assert 5 <= len(cases) < len(INVENTORY) // 2  # one module of many, as with `-k`
    lines, problems = guard.check(report(tmp_path, cases), platform)
    assert any(f"{len(cases)} test cases" in line for line in lines)
    missing = len(INVENTORY) - len(cases)
    assert f"... and {missing - guard.MAX_LISTED} more tests (total {missing})" in problems
    assert len([p for p in problems if p.startswith("Mandatory security test missing")]) == len(
        guard.SECURITY_TESTS
    )


@pytest.mark.parametrize("identity", sorted(guard.SECURITY_TESTS), ids=lambda i: i[1])
@pytest.mark.parametrize("platform", PLATFORMS)
def test_each_missing_mandatory_security_test_is_rejected(tmp_path, platform, identity):
    problems = problems_for(tmp_path, without(authentic(platform), identity), platform)
    assert f"Mandatory security test missing: {fmt(identity)}" in problems
    assert f"Test missing from the run: {fmt(identity)}" in problems


@pytest.mark.parametrize("platform", PLATFORMS)
def test_missing_tests_are_rejected_even_when_the_smaller_totals_are_consistent(tmp_path, platform):
    dropped = [c[:2] for c in authentic(platform) if c[:2] in PLAIN][:5]
    cases = without(authentic(platform), *dropped)
    path = report(tmp_path, cases)  # tests="N-5", skipped=<skips>: internally consistent
    problems = guard.check(path, platform)[1]
    assert [p for p in problems if p.startswith("Test missing from the run")] == [
        f"Test missing from the run: {fmt(identity)}" for identity in sorted(dropped)
    ]
    assert len(problems) == 5


@pytest.mark.parametrize(
    "suite",
    [
        {"tests": "99999"},
        {"tests": "1"},
        {"skipped": "0"},
        {"skipped": "4"},
        {"failures": "1"},
        {"errors": "2"},
        {"tests": None},
        {"skipped": None},
        {"tests": "many"},
        {"tests": "-1"},
        {"tests": "3.0"},
        {"tests": ""},
        {"tests": "0x10"},
    ],
    ids=str,
)
def test_contradictory_or_missing_suite_totals_are_rejected(tmp_path, suite):
    problems = problems_for(tmp_path, authentic("linux"), "linux", suite=suite)
    assert any(("declares" in p) or ("counter" in p) for p in problems), problems


def test_contradictory_root_totals_are_rejected(tmp_path):
    assert has(problems_for(tmp_path, authentic("win32"), "win32", root={"tests": "5"}), "The root")
    consistent = {"tests": str(len(INVENTORY)), "skipped": "0"}
    assert problems_for(tmp_path, authentic("win32"), "win32", root=consistent) == []


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("kind", ["failure", "error"])
def test_reports_with_failure_or_error_elements_are_rejected(tmp_path, platform, kind):
    victim = PLAIN[0]
    cases = with_outcomes(authentic(platform), {victim: kind})
    problems = problems_for(tmp_path, cases, platform)  # counters agree with the elements
    assert f"Test {kind}: {fmt(victim)}" in problems
    hidden = problems_for(tmp_path, cases, platform, suite={"failures": "0", "errors": "0"})
    assert has(hidden, "Test " + kind) and any("declares" in p for p in hidden)


def test_ambiguous_outcome_elements_are_rejected(tmp_path):
    cases = with_outcomes(authentic("win32"), {PLAIN[0]: "both"})
    assert any("Ambiguous outcome" in p for p in problems_for(tmp_path, cases, "win32"))


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("outcome", [SKIPPED, "xfail"])
def test_unexpected_skips_are_rejected_on_both_platforms(tmp_path, platform, outcome):
    cases = with_outcomes(authentic(platform), {PLAIN[0]: outcome})
    assert problems_for(tmp_path, cases, platform) == [f"Unexpected skip: {fmt(PLAIN[0])}"]


def test_expected_skip_must_be_a_real_pytest_skip(tmp_path):
    victim = sorted(guard.WINDOWS_ONLY_TESTS)[0]
    cases = with_outcomes(authentic("linux"), {victim: "xfail"})
    assert problems_for(tmp_path, cases, "linux") == [
        f"Expected skip is not a pytest.skip: {fmt(victim)}"
    ]


# --- malformed documents, platforms and the command line -------------------------------------


def good_suite_attrs() -> str:
    return 'name="pytest" tests="1" failures="0" errors="0" skipped="0"'


MALFORMED_DOCUMENTS = {
    "empty": "",
    "whitespace": "  \n",
    "not-xml": "this is not xml",
    "truncated": '<testsuites name="pytest tests"><testsuite name="pytest"',
    "wrong-root": "<foo><testcase classname='tests.a' name='test_a'/></foo>",
    "bare-testsuite-root": f"<testsuite {good_suite_attrs()}><testcase classname='a' name='t'/></testsuite>",
    "no-suite": "<testsuites/>",
    "two-suites": f"<testsuites><testsuite {good_suite_attrs()}/><testsuite {good_suite_attrs()}/></testsuites>",
    "wrong-suite-name": '<testsuites><testsuite name="other" tests="0" failures="0" errors="0" skipped="0"/></testsuites>',
    "nested-suite": f"<testsuites><testsuite {good_suite_attrs()}><testsuite {good_suite_attrs()}/></testsuite></testsuites>",
    "stray-root-child": f"<testsuites><testsuite {good_suite_attrs()}/><testcase classname='a' name='t'/></testsuites>",
    "doctype": '<!DOCTYPE testsuites [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>',
    "case-outside-suite": "<testsuites><testcase classname='tests.a' name='test_a'/></testsuites>",
}


@pytest.mark.parametrize("document", MALFORMED_DOCUMENTS.values(), ids=MALFORMED_DOCUMENTS)
@pytest.mark.parametrize("platform", PLATFORMS)
def test_empty_or_invalid_documents_are_rejected(tmp_path, platform, document):
    path = tmp_path / "junit.xml"
    path.write_text(document, encoding="utf-8")
    lines, problems = guard.check(path, platform)
    assert problems and lines == []
    assert guard.main([str(path), platform]) == 1


def test_valid_content_under_the_wrong_root_is_rejected(tmp_path):
    path = report(tmp_path, authentic("win32"), root_tag="results")
    assert guard.check(path, "win32") == (
        [],
        ["Unexpected root element <results>; expected <testsuites>"],
    )


@pytest.mark.parametrize("suites", [0, 2])
def test_exactly_one_suite_is_required_so_a_second_suite_cannot_hide_results(tmp_path, suites):
    path = report(tmp_path, authentic("win32"), suites=suites)
    assert guard.check(path, "win32")[1] == [f"Expected exactly one <testsuite>, found {suites}"]


def test_dtd_in_an_otherwise_valid_report_is_rejected(tmp_path):
    path = report(tmp_path, authentic("win32"))
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("<testsuites", '<!DOCTYPE testsuites [<!ENTITY x "y">]><testsuites', 1)
    )
    assert guard.check(path, "win32") == ([], ["The report contains a DTD or entity declaration"])


def test_non_utf8_missing_and_directory_inputs_are_rejected(tmp_path):
    utf16 = tmp_path / "utf16.xml"
    utf16.write_bytes('<testsuites name="x"/>'.encode("utf-16"))
    assert guard.check(utf16, "linux")[1] == ["The report is not UTF-8 text"]
    assert has(guard.check(tmp_path / "absent.xml", "linux")[1], "Cannot read the report")
    assert has(guard.check(tmp_path, "linux")[1], "Cannot read the report")


def test_oversized_report_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "MAX_REPORT_BYTES", 100)
    assert has(
        guard.check(report(tmp_path, authentic("win32")), "linux")[1], "The report is larger"
    )


def test_unknown_platform_wins_over_a_malformed_report(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text("not xml", encoding="utf-8")
    assert guard.check(path, "plan9")[1] == ["No expected-skip set is defined for platform 'plan9'"]
    assert guard.main([str(path), "plan9"]) == 1


def test_main_reports_success_and_failure_without_a_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert guard.main([str(report(tmp_path, authentic("linux"))), "linux"]) == 0
    assert "ERROR" not in capsys.readouterr().err
    assert guard.main([str(report(tmp_path, authentic("linux"))), "win32"]) == 1
    assert "ERROR: Unexpected skip" in capsys.readouterr().err


def test_selection_override_in_the_environment_is_rejected(tmp_path, capsys, monkeypatch):
    path = report(tmp_path, authentic("linux"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k check_expected_skips")
    assert guard.main([str(path), "linux"]) == 1
    assert "PYTEST_ADDOPTS" in capsys.readouterr().err
    assert guard.main(["--print-inventory", str(path)]) == 1
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    assert guard.main([str(path), "linux"]) == 0


# --- R2: nested elements inside permitted containers ----------------------------------------------

SUITE_PROPERTIES = '<properties><property name="note" value="x" /></properties>'
EVIL_CASE = '<testcase classname="tests.evil" name="test_evil">'
FAIL = '<failure message="boom" />'


def mutated(
    tmp_path: Path,
    platform: str = "linux",
    *,
    suite_properties: str | None = None,
    case_children: str | None = None,
    suite_tail: str = "",
    root_tail: str = "",
) -> Path:
    """An authentic report with fragments injected; the declared counters stay as they were."""
    path = report(tmp_path, authentic(platform))
    text = path.read_text(encoding="utf-8")
    if suite_properties is not None:
        assert SUITE_PROPERTIES in text
        text = text.replace(SUITE_PROPERTIES, suite_properties, 1)
    if case_children is not None:
        bare = case_xml(*PLAIN[0], PASSED)
        assert bare in text
        text = text.replace(bare, bare[:-3] + ">" + case_children + "</testcase>", 1)
    text = text.replace("</testsuite>", suite_tail + "</testsuite>", 1)
    text = text.replace("</testsuites>", root_tail + "</testsuites>", 1)
    path.write_text(text, encoding="utf-8")
    return path


def prop_with(child: str) -> str:
    return f'<properties><property name="a" value="b">{child}</property></properties>'


NESTED_ATTACKS = {
    "failure-in-suite-properties": (
        {"suite_properties": f"<properties>{FAIL}</properties>"},
        "Unexpected <failure> inside <properties>",
    ),
    "failure-in-testcase-properties": (
        {"case_children": f"<properties>{FAIL}</properties>"},
        "Unexpected <failure> inside <properties>",
    ),
    "error-in-testcase-properties": (
        {"case_children": '<properties><error message="boom" /></properties>'},
        "Unexpected <error> inside <properties>",
    ),
    "testcase-in-suite-properties": (
        {"suite_properties": f"<properties>{EVIL_CASE}</testcase></properties>"},
        "Unexpected <testcase> inside <properties>",
    ),
    "testcase-with-error-in-suite-properties": (
        {
            "suite_properties": f'<properties>{EVIL_CASE}<error message="x" /></testcase></properties>'
        },
        "Unexpected <testcase> inside <properties>",
    ),
    "testcase-with-error-in-testcase": (
        {"case_children": f'{EVIL_CASE}<error message="boom" /></testcase>'},
        "Unexpected <testcase> inside <testcase>",
    ),
    "testcase-in-testcase-properties": (
        {"case_children": f"<properties>{EVIL_CASE}</testcase></properties>"},
        "Unexpected <testcase> inside <properties>",
    ),
    "error-in-system-out": (
        {"case_children": '<system-out><error message="boom" /></system-out>'},
        "Unexpected <error> inside <system-out>",
    ),
    "skipped-in-system-err": (
        {"case_children": '<system-err><skipped type="pytest.skip" /></system-err>'},
        "Unexpected <skipped> inside <system-err>",
    ),
    "failure-in-suite-property": (
        {"suite_properties": prop_with(FAIL)},
        "Unexpected <failure> inside <property>",
    ),
    "testcase-in-testcase-property": (
        {"case_children": prop_with(EVIL_CASE + "</testcase>")},
        "Unexpected <testcase> inside <property>",
    ),
    "testcase-in-system-out": (
        {"case_children": f"<system-out>{EVIL_CASE}</testcase></system-out>"},
        "Unexpected <testcase> inside <system-out>",
    ),
    "failure-in-system-err": (
        {"case_children": f"<system-err>{FAIL}</system-err>"},
        "Unexpected <failure> inside <system-err>",
    ),
    "output-nested-in-output": (
        {"case_children": "<system-out><system-out /></system-out>"},
        "Unexpected <system-out> inside <system-out>",
    ),
    "failure-in-skipped": (
        {"case_children": f'<skipped type="pytest.skip">{FAIL}</skipped>'},
        "Unexpected <failure> inside <skipped>",
    ),
    "unknown-in-failure": (
        {"case_children": '<failure message="x"><extra /></failure>'},
        "Unexpected <extra> inside <failure>",
    ),
    "unknown-under-testsuites": (
        {"root_tail": "<extra />"},
        "Unexpected <extra> inside <testsuites>",
    ),
    "unknown-in-testsuite": (
        {"suite_tail": "<extra />"},
        "Unexpected <extra> inside <testsuite>",
    ),
    "unknown-in-suite-properties": (
        {"suite_properties": "<properties><extra /></properties>"},
        "Unexpected <extra> inside <properties>",
    ),
    "unknown-in-property": (
        {"suite_properties": prop_with("<extra />")},
        "Unexpected <extra> inside <property>",
    ),
    "unknown-in-testcase": (
        {"case_children": "<extra />"},
        "Unexpected <extra> inside <testcase>",
    ),
    "unknown-in-system-out": (
        {"case_children": "<system-out><extra /></system-out>"},
        "Unexpected <extra> inside <system-out>",
    ),
    "namespaced-failure": (
        {"case_children": '<x:failure xmlns:x="urn:x" />'},
        "Unexpected <{urn:x}failure> inside <testcase>",
    ),
    "properties-misplaced-under-testsuites": (
        {"root_tail": SUITE_PROPERTIES},
        "Unexpected <properties> inside <testsuites>",
    ),
    "skipped-misplaced-in-testsuite": (
        {"suite_tail": '<skipped type="pytest.skip" />'},
        "Unexpected <skipped> inside <testsuite>",
    ),
    "failure-misplaced-in-testsuite": (
        {"suite_tail": FAIL},
        "Unexpected <failure> inside <testsuite>",
    ),
    "testsuite-nested-in-testsuite": (
        {"suite_tail": '<testsuite name="pytest" />'},
        "Unexpected <testsuite> inside <testsuite>",
    ),
    "testcase-misplaced-under-testsuites": (
        {"root_tail": '<testcase classname="tests.evil" name="test_evil" />'},
        "Unexpected <testcase> inside <testsuites>",
    ),
    "second-suite-properties": (
        {"suite_tail": SUITE_PROPERTIES},
        "Too many <properties> elements inside <testsuite> (at most 1)",
    ),
    "second-testcase-properties": (
        {"case_children": SUITE_PROPERTIES * 2},
        "Too many <properties> elements inside <testcase> (at most 1)",
    ),
    "second-failure": (
        {"case_children": FAIL * 2},
        "Too many <failure> elements inside <testcase> (at most 1)",
    ),
    "text-in-properties": (
        {"suite_properties": "<properties>stray text</properties>"},
        "Unexpected text inside <properties>",
    ),
    "text-after-property": (
        {"suite_properties": '<properties><property name="a" value="b" />tail</properties>'},
        "Unexpected text after <property> inside <properties>",
    ),
}


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("attack", NESTED_ATTACKS.values(), ids=NESTED_ATTACKS)
def test_nested_or_misplaced_elements_are_rejected(tmp_path, monkeypatch, platform, attack):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    options, expected = attack
    path = mutated(tmp_path, platform, **options)
    problems = guard.check(path, platform)[1]
    assert expected in problems, problems
    assert guard.main([str(path), platform]) == 1


def test_nested_failure_in_testcase_properties_cannot_hide_a_failure(tmp_path):
    """R2 reproduction 1: <failure/> in <properties> inside an existing, passing test case."""
    path = mutated(tmp_path, case_children=f"<properties>{FAIL}</properties>")
    assert guard.check(path, "linux")[1] == ["Unexpected <failure> inside <properties>"]


def test_nested_testcase_in_suite_properties_cannot_smuggle_a_case(tmp_path):
    """R2 reproduction 2: a new <testcase><error/></testcase> inside suite-level <properties>."""
    fragment = f'<properties>{EVIL_CASE}<error message="boom" /></testcase></properties>'
    problems = guard.check(mutated(tmp_path, suite_properties=fragment), "linux")[1]
    assert problems == ["Unexpected <testcase> inside <properties>"]


def test_a_misplaced_element_is_not_descended_into_or_counted(tmp_path):
    """Hidden cases never become cases: only the structure error is reported."""
    path = mutated(tmp_path, suite_properties=f"<properties>{EVIL_CASE}</testcase></properties>")
    cases, problems = guard.parse_report(path)
    assert len(cases) == len(INVENTORY)
    assert problems == ["Unexpected <testcase> inside <properties>"]


def test_repeated_structure_problems_are_summarised_not_repeated(tmp_path):
    fragment = "<properties>" + FAIL * 50 + "</properties>"
    problems = guard.check(mutated(tmp_path, suite_properties=fragment), "linux")[1]
    assert problems == ["Unexpected <failure> inside <properties> (50 times)"]


def test_legitimate_properties_and_output_elements_are_accepted(tmp_path):
    """The shapes pytest really writes: properties/property, and repeated output pairs."""
    properties = (
        '<properties><property name="a" value="1" /><property name="b" value="2" /></properties>'
    )
    output = "<system-out>some text</system-out><system-err />" * 2
    for platform in PLATFORMS:
        path = mutated(tmp_path, platform, suite_properties=properties)
        assert guard.check(path, platform)[1] == []
        path = mutated(tmp_path, platform, case_children=properties + output)
        assert guard.check(path, platform)[1] == []
    cases, problems = guard.parse_report(mutated(tmp_path, case_children=output))
    assert problems == [] and len(cases) == len(INVENTORY)


OK_CASE = '<testcase classname="x" name="test_ok"/>'


def suite_xml(body: str, *, tests: int, skipped: int = 0) -> str:
    return (
        '<testsuites><testsuite name="pytest" errors="0" failures="0"'
        f' skipped="{skipped}" tests="{tests}">{body}</testsuite></testsuites>'
    )


def skips(classname: str, suffix: str = "") -> str:
    return "".join(
        f'<testcase classname="{classname or c}" name="{n}{suffix}">'
        '<skipped type="pytest.skip" message="why"/></testcase>'
        for c, n in sorted(guard.WINDOWS_ONLY_TESTS)
    )


R1_FALSE_PASSES = {  # the R1 audit's reports, as minimal raw documents
    "unrelated-module": ("linux", suite_xml(skips("unrelated.mod"), tests=3, skipped=3)),
    "duplicated-skips": ("linux", suite_xml(skips("") * 2, tests=6, skipped=6)),
    "fabricated-parameters": ("linux", suite_xml(skips("", "[fake]"), tests=3, skipped=3)),
    "contradictory-totals": ("win32", suite_xml(OK_CASE, tests=99999)),
    "failure-element": (
        "win32",
        suite_xml(OK_CASE + '<testcase classname="x" name="t"><failure/></testcase>', tests=2),
    ),
    "error-element": (
        "win32",
        suite_xml(OK_CASE + '<testcase classname="x" name="t"><error/></testcase>', tests=2),
    ),
    "foreign-root": ("win32", f"<foo>{OK_CASE}</foo>"),
}


@pytest.mark.parametrize("scenario", R1_FALSE_PASSES.values(), ids=R1_FALSE_PASSES)
def test_r1_audit_false_passes_stay_rejected(tmp_path, monkeypatch, scenario):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    platform, document = scenario
    path = tmp_path / "junit.xml"
    path.write_text(document, encoding="utf-8")
    assert guard.check(path, platform)[1]
    assert guard.main([str(path), platform]) == 1


# --- the inventory file and its single generator -------------------------------------------------

ENTRY = '["tests.test_a", "test_a"]'


def write_inventory(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(text, encoding="utf-8")
    return path


def inventory_text(rows: str, schema: str = "1", extra: str = "") -> str:
    return f'{{"schema": {schema}, "tests": [{rows}]{extra}}}'


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        inventory_text(""),
        inventory_text(ENTRY, schema="2"),
        inventory_text(ENTRY, schema="true"),
        inventory_text(ENTRY, extra=', "other": 1'),
        '{"schema": 1, "schema": 1, "tests": [["tests.a", "t"]]}',
        inventory_text('["tests.b", "t"], ["tests.a", "t"]'),
        inventory_text('["tests.a", "t"], ["tests.a", "t"]'),
        inventory_text('["tests.a", "t", "x"]'),
        inventory_text('["tests.a"]'),
        inventory_text('["tests/a.py", "t"]'),
        inventory_text('["tests.a", 7]'),
        inventory_text('"tests.a::t"'),
    ],
    ids=range(15),
)
def test_tampered_inventories_are_rejected(tmp_path, text):
    identities, problems = guard.load_inventory(write_inventory(tmp_path, text))
    assert identities == frozenset() and len(problems) == 1


def test_inventory_missing_a_mandatory_test_is_reported(tmp_path):
    identities, problems = guard.load_inventory(write_inventory(tmp_path, inventory_text(ENTRY)))
    assert identities == {("tests.test_a", "test_a")}
    assert len(problems) == len(guard.SECURITY_TESTS)
    assert all("lacks mandatory security test" in p for p in problems)


def test_missing_inventory_file_fails_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "INVENTORY_PATH", tmp_path / "absent.json")
    problems = guard.check(report(tmp_path, authentic("win32")), "win32")[1]
    assert "Inventory absent.json cannot be loaded: FileNotFoundError" in problems


def test_print_inventory_is_platform_independent_and_matches_the_committed_file(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    outputs = []
    for platform in PLATFORMS:
        assert guard.main(["--print-inventory", str(report(tmp_path, authentic(platform)))]) == 0
        outputs.append(capsys.readouterr())
    assert outputs[0].out == outputs[1].out == guard.inventory_json(sorted(INVENTORY))
    assert "0 added, 0 removed" in outputs[0].err


@pytest.mark.parametrize("bad", ["duplicate", "failure", "malformed", "empty"])
def test_print_inventory_refuses_unclean_reports(tmp_path, capsys, monkeypatch, bad):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    cases = authentic("win32")
    cases = {
        "duplicate": [*cases, cases[0]],
        "failure": with_outcomes(cases, {PLAIN[0]: "failure"}),
        "malformed": [*cases, ("tests.a", "bad name", PASSED)],
        "empty": [],
    }[bad]
    assert guard.main(["--print-inventory", str(report(tmp_path, cases))]) == 1
    assert capsys.readouterr().out == ""


def test_a_filtered_run_is_visible_as_removals_in_print_inventory(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    kept = authentic("win32")[:20]
    assert guard.main(["--print-inventory", str(report(tmp_path, kept))]) == 0
    assert f"{len(INVENTORY) - 20} removed" in capsys.readouterr().err


# --- parser vs the XML the pinned pytest really emits ----------------------------------------------

SAMPLE = """
import pytest

def test_ok():
    pass

@pytest.mark.parametrize("value", [1, "a b", "x[y]"])
def test_param(value):
    pass

def test_skipped():
    pytest.skip("because")

class TestGroup:
    def test_method(self):
        pass
"""


def run_sample(tmp_path: Path, extra_test: str = "", *extra_args: str) -> Path:
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (sample / "test_sample.py").write_text(SAMPLE + extra_test, encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    junit = tmp_path / "real.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *extra_args,
        ],
        cwd=sample,
        env=environment,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return junit


def test_parser_accepts_real_pytest_output(tmp_path):
    cases, problems = guard.parse_report(run_sample(tmp_path))
    assert problems == []
    outcomes = {c.identity: c.outcome for c in cases}
    assert outcomes == {
        ("test_sample", "test_ok"): PASSED,
        ("test_sample", "test_param[1]"): PASSED,
        ("test_sample", "test_param[a b]"): PASSED,
        ("test_sample", "test_param[x[y]]"): PASSED,
        ("test_sample", "test_skipped"): SKIPPED,
        ("test_sample.TestGroup", "test_method"): PASSED,
    }
    skip = next(c for c in cases if c.outcome == SKIPPED)
    assert (skip.skip_type, skip.skip_message) == ("pytest.skip", "because")


def test_parser_flags_a_real_pytest_failure(tmp_path):
    cases, problems = guard.parse_report(run_sample(tmp_path, "\ndef test_bad():\n    assert 0\n"))
    assert problems == ["Test failure: test_sample::test_bad"]
    assert len(cases) == 7


RICH_SAMPLE = """
import sys

def test_prop(record_property):
    record_property("key", "value")

def test_output():
    print("to stdout")
    print("to stderr", file=sys.stderr)
"""


def test_parser_accepts_real_pytest_properties_and_output_elements(tmp_path):
    """What pytest 9.1.1 writes with record_property and junit_logging=all must stay supported."""
    junit = run_sample(tmp_path, RICH_SAMPLE, "-o", "junit_logging=all")
    text = junit.read_text(encoding="utf-8")
    assert "<properties><property" in text and "<system-out>" in text and "<system-err>" in text
    cases, problems = guard.parse_report(junit)
    assert problems == []
    assert {c.identity: c.outcome for c in cases}[("test_sample", "test_prop")] == PASSED
    assert len(cases) == 8


def test_real_pytest_failure_with_output_is_still_reported(tmp_path):
    bad = "\ndef test_bad():\n    print('before')\n    assert 0\n"
    junit = run_sample(tmp_path, bad, "-o", "junit_logging=all")
    assert "<failure" in junit.read_text(encoding="utf-8")
    assert guard.parse_report(junit)[1] == ["Test failure: test_sample::test_bad"]


# --- the workflow keeps the pieces this guard depends on ----------------------------------------------


def test_workflow_neutralises_selection_overrides_and_runs_the_guard():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    steps = re.split(r"\n      - name: ", text)
    pytest_step = next(s for s in steps if "-m pytest" in s)
    guard_step = next(s for s in steps if "check_expected_skips.py" in s)
    assert 'PYTEST_ADDOPTS: ""' in pytest_step
    assert re.search(r'pytest-junit\.xml" tests\s*$', pytest_step)  # explicit scope, last argument
    assert "rm -f" in pytest_step  # no stale report can satisfy the guard
    assert "pytest-junit.xml" in pytest_step and "pytest-junit.xml" in guard_step
    assert "continue-on-error" not in text and "|| true" not in text
    assert "contents: read" in text and "persist-credentials: false" in text
