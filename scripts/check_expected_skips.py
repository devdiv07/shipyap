"""CI guard: the pytest JUnit report must match a reviewed full-suite inventory and platform outcomes.

Usage:
    check_expected_skips.py JUNIT_XML [PLATFORM]    PLATFORM defaults to sys.platform
    check_expected_skips.py --print-inventory JUNIT_XML

A report is accepted only when ALL of the following hold:
- Structure: root <testsuites> holding exactly one <testsuite name="pytest">; every element of
  the whole tree is checked against an explicit parent-child grammar (_GRAMMAR, the pytest 9.1.1
  xunit2 format only, not arbitrary JUnit dialects), so an unknown element, or an outcome element
  or <testcase> placed inside <properties>, <property> or an output container, is rejected rather
  than ignored; the suite counters (tests/failures/errors/skipped) equal what its <testcase>
  elements contain.
- Identity: every case has a well-formed, unique (classname, name) pair. The complete pytest
  parameter id is part of the name; nothing is shortened or de-parameterized.
- Outcome: no failure or error element; every case passed except the reviewed skips of the platform.
- Completeness: the set of identities equals scripts/expected_test_inventory.json, a reviewed
  full-suite inventory committed to the repository. A subset, a superset, a test from the wrong
  module or an invented parameter id is rejected. The test count alone is never the criterion.
- Mandatory security tests (SECURITY_TESTS) must exist under their expected module and have
  their platform-specific outcome.

Limits (do not overstate this guard):
- A JUnit report is ordinary generated data, not an authenticated test attestation. Any process
  that can write the file can forge a perfectly consistent report. The guard catches mistakes,
  filtered runs and sloppy fabrication, not a determined attacker with write access to the runner.
- It does not show that test bodies are meaningful or that they ran against this exact commit.
- The inventory is trusted repository content. A change that edits tests and the inventory
  together is only as safe as its review; the inventory diff is the thing to review.
- pytest's own exit status stays the primary gate; this guard runs after it.
- The inventory is platform independent: both platforms must collect the same tests.
  Platform-dependent collection would need the inventory format extended.

Nothing here regenerates expectations from the report under test. --print-inventory only prints
what a report contains, for a maintainer to run on an unfiltered full run and review as a diff;
the CI workflow never calls it. Standard library only.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

Identity = tuple[str, str]  # (JUnit classname, complete JUnit name including any [param id])

PASSED, SKIPPED = "passed", "skipped"
PLATFORMS = ("win32", "linux")
INVENTORY_PATH = Path(__file__).resolve().with_name("expected_test_inventory.json")
INVENTORY_SCHEMA = 1
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_LISTED = 10  # entries listed per bulk problem; the count is always complete

_EVIDENCE_CHECK = "tests.test_evidence_check"
_EVERYWHERE = {"win32": PASSED, "linux": PASSED}
_WINDOWS_ONLY = {"win32": PASSED, "linux": SKIPPED}

# Mandatory security tests: complete identity -> required outcome per platform. Hosted Windows is
# expected to create symlinks, so nothing may be skipped there. Every inventory test not listed
# here must pass on both platforms.
SECURITY_TESTS: dict[Identity, dict[str, str]] = {
    (_EVIDENCE_CHECK, "test_file_symlink_escape"): _EVERYWHERE,
    (_EVIDENCE_CHECK, "test_directory_symlink_escape"): _EVERYWHERE,
    (_EVIDENCE_CHECK, "test_symlink_inside_root_is_allowed"): _EVERYWHERE,
    (_EVIDENCE_CHECK, "test_windows_junction_escape"): _WINDOWS_ONLY,
    (_EVIDENCE_CHECK, "test_windows_trailing_dot_alias_cannot_reach_file"): _WINDOWS_ONLY,
}
WINDOWS_ONLY_TESTS = frozenset(i for i, o in SECURITY_TESTS.items() if o == _WINDOWS_ONLY)
EXPECTED_SKIPS: dict[str, frozenset[Identity]] = {
    platform: frozenset(i for i, o in SECURITY_TESTS.items() if o[platform] == SKIPPED)
    for platform in PLATFORMS
}

_CLASSNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# pytest escapes non-ASCII parameter ids, so printable ASCII is the complete alphabet.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[\x20-\x7e]*\])?")
_COUNTER = re.compile(r"[0-9]{1,9}")
_MAX_IDENTIFIER = 4096  # the longest real id today is ~1100 characters
_SHOWN = 120  # longest identifier fragment echoed into a message
_OUTCOME_TAGS = ("skipped", "failure", "error")
# The complete element grammar of the supported report (pytest 9.1.1, junit_family=xunit2):
# parent tag -> {permitted child tag: maximum occurrences, None = any number}. A tag that is
# not a key is unknown wherever it appears; a known tag is only valid under the parents listed
# here, so an outcome element or <testcase> hidden inside a container is rejected, not ignored.
_GRAMMAR: dict[str, dict[str, int | None]] = {
    "testsuites": {"testsuite": None},
    "testsuite": {"properties": 1, "testcase": None},
    "properties": {"property": None},
    "property": {},
    "testcase": {
        "properties": 1,
        "skipped": 1,
        "failure": 1,
        "error": 1,
        "system-out": None,  # pytest 9.1.1 with junit_logging=all writes several per skipped test
        "system-err": None,
    },
    "skipped": {},
    "failure": {},
    "error": {},
    "system-out": {},
    "system-err": {},
}
_TEXT_ELEMENTS = frozenset(("skipped", "failure", "error", "system-out", "system-err"))


@dataclass(frozen=True)
class Case:
    identity: Identity
    outcome: str  # passed | skipped | failure | error | invalid
    skip_type: str = ""
    skip_message: str = ""


def _short(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}...({len(text)} chars)"


def fmt(identity: Identity) -> str:
    return _short(f"{identity[0]}::{identity[1]}")


def identity_problem(classname: object, name: object) -> str | None:
    """Return why (classname, name) is not a well-formed pytest JUnit identity, or None."""
    if not isinstance(classname, str) or not isinstance(name, str):
        return "missing classname or name"
    if len(classname) > _MAX_IDENTIFIER or len(name) > _MAX_IDENTIFIER:
        return "identifier too long"
    if not _CLASSNAME.fullmatch(classname):
        return "malformed classname"
    if not _NAME.fullmatch(name):
        return "malformed name"
    return None


def _read_root(junit_xml: Path) -> tuple[ET.Element | None, list[str]]:
    try:
        with junit_xml.open("rb") as handle:
            data = handle.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        return None, [f"Cannot read the report: {exc.strerror or type(exc).__name__}"]
    if len(data) > MAX_REPORT_BYTES:
        return None, [f"The report is larger than {MAX_REPORT_BYTES} bytes"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["The report is not UTF-8 text"]
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None, ["The report contains a DTD or entity declaration"]
    try:
        return ET.fromstring(data), []
    except (ET.ParseError, ValueError) as exc:
        return None, [f"Malformed or empty XML: {exc}"]


def _counter(element: ET.Element, attribute: str, label: str, problems: list[str]) -> int | None:
    value = element.get(attribute)
    if value is None:
        problems.append(f"{label} has no {attribute!r} counter")
        return None
    if not _COUNTER.fullmatch(value):
        problems.append(f"{label} counter {attribute}={value!r} is not a number")
        return None
    return int(value)


def structure_problems(root: ET.Element) -> list[str]:
    """Validate every element of the tree against _GRAMMAR (iteratively; the grammar bounds depth).

    Unknown or misplaced elements are reported and not descended into, so nothing inside them is
    ever interpreted as a test case or an outcome.
    """
    found: Counter[str] = Counter()
    pending = [root]
    while pending:
        parent = pending.pop()
        allowed = _GRAMMAR[parent.tag]
        if parent.tag not in _TEXT_ELEMENTS and (parent.text or "").strip():
            found[f"Unexpected text inside <{parent.tag}>"] += 1
        seen: Counter[str] = Counter()
        for child in parent:
            if (child.tail or "").strip():
                found[f"Unexpected text after <{child.tag}> inside <{parent.tag}>"] += 1
            if child.tag not in allowed:
                found[f"Unexpected <{child.tag}> inside <{parent.tag}>"] += 1
                continue
            seen[child.tag] += 1
            pending.append(child)
        for tag, limit in allowed.items():
            if limit is not None and seen[tag] > limit:
                found[f"Too many <{tag}> elements inside <{parent.tag}> (at most {limit})"] += 1
    return [m if n == 1 else f"{m} ({n} times)" for m, n in sorted(found.items())]


def _case(element: ET.Element, problems: list[str]) -> Case | None:
    classname, name = element.get("classname"), element.get("name")
    bad = identity_problem(classname, name)
    if bad:
        problems.append(
            f"Malformed test case ({bad}): "
            f"classname={_short(repr(classname))} name={_short(repr(name))}"
        )
        return None
    identity = (classname, name)
    tags = [child.tag for child in element]  # already validated against _GRAMMAR
    markers = [tag for tag in tags if tag in _OUTCOME_TAGS]
    if len(markers) > 1:
        problems.append(f"Ambiguous outcome elements {markers} in {fmt(identity)}")
        return Case(identity, "invalid")
    if not markers:
        return Case(identity, PASSED)
    marker = element.find(markers[0])
    if markers[0] == "skipped":
        return Case(identity, SKIPPED, marker.get("type", ""), marker.get("message", ""))
    return Case(identity, markers[0])


def parse_report(junit_xml: Path) -> tuple[list[Case], list[str]]:
    """Return (cases, structural problems). cases is empty when the document is unusable."""
    root, problems = _read_root(junit_xml)
    if root is None:
        return [], problems
    if root.tag != "testsuites":
        return [], [f"Unexpected root element <{root.tag}>; expected <testsuites>"]
    problems += structure_problems(root)
    suites = [child for child in root if child.tag == "testsuite"]
    if len(suites) != 1:
        return [], [*problems, f"Expected exactly one <testsuite>, found {len(suites)}"]
    suite = suites[0]
    if suite.get("name") != "pytest":
        problems.append(f"Unexpected suite name {suite.get('name')!r}; expected 'pytest'")

    cases = []
    for child in suite:
        if child.tag == "testcase":  # only direct children of the one suite are test cases
            case = _case(child, problems)
            if case is not None:
                cases.append(case)

    derived = {
        "tests": sum(1 for child in suite if child.tag == "testcase"),
        "skipped": sum(1 for c in cases if c.outcome == SKIPPED),
        "failures": sum(1 for c in cases if c.outcome == "failure"),
        "errors": sum(1 for c in cases if c.outcome == "error"),
    }
    for element, label in ((suite, "The suite"), (root, "The root")):
        for attribute, actual in derived.items():
            if label == "The root" and attribute not in root.attrib:
                continue  # pytest writes no root counters; if present they must still agree
            declared = _counter(element, attribute, label, problems)
            if declared is not None and declared != actual:
                problems.append(f"{label} declares {attribute}={declared} but contains {actual}")

    for identity, count in sorted(Counter(c.identity for c in cases).items()):
        if count > 1:
            problems.append(f"Duplicate test identity ({count} times): {fmt(identity)}")
    for case in cases:
        if case.outcome in ("failure", "error"):
            problems.append(f"Test {case.outcome}: {fmt(case.identity)}")
    return cases, problems


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def load_inventory(path: Path | None = None) -> tuple[frozenset[Identity], list[str]]:
    """Load and validate the reviewed inventory. Returns an empty set with problems if unusable."""
    path = path or INVENTORY_PATH
    label = f"Inventory {path.name}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as exc:
        return frozenset(), [f"{label} cannot be loaded: {type(exc).__name__}"]
    if not isinstance(data, dict) or set(data) != {"schema", "tests"}:
        return frozenset(), [f"{label} must be an object with exactly 'schema' and 'tests'"]
    if data["schema"] != INVENTORY_SCHEMA or isinstance(data["schema"], bool):
        return frozenset(), [f"{label} has an unsupported schema"]
    entries = data["tests"]
    if not isinstance(entries, list) or not entries:
        return frozenset(), [f"{label} 'tests' must be a non-empty list"]
    identities = []
    for position, entry in enumerate(entries):
        if not (isinstance(entry, list) and len(entry) == 2) or identity_problem(*entry):
            return frozenset(), [f"{label} entry {position} is not a valid [classname, name]"]
        identities.append((entry[0], entry[1]))
    for earlier, later in zip(identities, identities[1:], strict=False):
        if earlier >= later:
            return frozenset(), [f"{label} is not strictly sorted and unique near {fmt(later)}"]
    inventory = frozenset(identities)
    return inventory, [
        f"{label} lacks mandatory security test {fmt(identity)}"
        for identity in sorted(set(SECURITY_TESTS) - inventory)
    ]


def inventory_json(identities: list[Identity]) -> str:
    """Render the canonical inventory file: sorted, one entry per line for reviewable diffs."""
    rows = ",\n".join(f"    {json.dumps(list(i))}" for i in sorted(set(identities)))
    return f'{{\n  "schema": {INVENTORY_SCHEMA},\n  "tests": [\n{rows}\n  ]\n}}\n'


def _bulk(prefix: str, identities: list[Identity], noun: str) -> list[str]:
    lines = [f"{prefix}: {fmt(i)}" for i in identities[:MAX_LISTED]]
    if len(identities) > MAX_LISTED:
        lines.append(
            f"... and {len(identities) - MAX_LISTED} more {noun} (total {len(identities)})"
        )
    return lines


def check(
    junit_xml: Path, platform: str, inventory: frozenset[Identity] | None = None
) -> tuple[list[str], list[str]]:
    """Return (report lines, problems). inventory defaults to the reviewed repository file."""
    if platform not in PLATFORMS:
        return [], [f"No expected-skip set is defined for platform {platform!r}"]
    cases, problems = parse_report(junit_xml)
    if not cases and problems:
        return [], problems  # unusable document: a comparison would only bury the real error
    if inventory is None:
        inventory, inventory_problems = load_inventory()
        problems += inventory_problems
    if not cases:
        problems.append("The report contains no test cases")
    observed = {case.identity: case for case in cases}  # duplicates were already reported

    expected_skips = EXPECTED_SKIPS[platform]
    skipped = [c for c in observed.values() if c.outcome == SKIPPED]
    lines = [
        f"Platform {platform}: {len(cases)} test cases, {len(skipped)} skipped,"
        f" {len(inventory)} in the reviewed inventory"
    ]
    lines += [
        f"- skipped {fmt(c.identity)}: {c.skip_message}"
        for c in sorted(skipped, key=lambda c: c.identity)
    ]

    for case in sorted(skipped, key=lambda c: c.identity):
        if case.identity not in expected_skips:
            problems.append(f"Unexpected skip: {fmt(case.identity)}")
        elif case.skip_type != "pytest.skip":
            problems.append(f"Expected skip is not a pytest.skip: {fmt(case.identity)}")
    for identity in sorted(expected_skips):
        case = observed.get(identity)
        if case is not None and case.outcome == PASSED:
            problems.append(f"Expected skip did not occur: {fmt(identity)}")

    for identity in sorted(set(SECURITY_TESTS) - set(observed)):
        problems.append(f"Mandatory security test missing: {fmt(identity)}")
    if inventory:
        problems += _bulk("Test missing from the run", sorted(inventory - set(observed)), "tests")
        problems += _bulk(
            "Test not in the reviewed inventory", sorted(set(observed) - inventory), "tests"
        )
    return lines, problems


def print_inventory(junit_xml: Path) -> int:
    """Print the canonical inventory of a structurally clean report (maintainer use, not CI)."""
    cases, problems = parse_report(junit_xml)
    if not cases and not problems:
        problems.append("The report contains no test cases")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    new = {c.identity for c in cases}
    old, _ = load_inventory()
    print(
        f"Inventory of {len(new)} tests: {len(new - old)} added, {len(old - new)} removed vs the"
        " committed file. Run this only on an unfiltered full run and review the diff.",
        file=sys.stderr,
    )
    # Bytes, so the file is identical (LF) on every OS and never picks up CRLF on Windows.
    sys.stdout.buffer.write(inventory_json(sorted(new)).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str]) -> int:
    printing = argv[:1] == ["--print-inventory"]
    if not (len(argv) == 2 if printing else len(argv) in (1, 2)):
        print(
            "usage: check_expected_skips.py JUNIT_XML [PLATFORM]\n"
            "       check_expected_skips.py --print-inventory JUNIT_XML",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("PYTEST_ADDOPTS"):
        print("ERROR: PYTEST_ADDOPTS is set and may have reduced the test scope", file=sys.stderr)
        return 1
    if printing:
        return print_inventory(Path(argv[1]))
    lines, problems = check(Path(argv[0]), argv[1] if len(argv) == 2 else sys.platform)
    print("\n".join(lines))
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
