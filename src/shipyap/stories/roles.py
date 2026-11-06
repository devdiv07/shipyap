"""What kind of evidence a file is, from its path alone. Used to choose evidence and to tell
the model how to weigh it: a plan states intent when it was written; a later record (results,
canaries, deviations, corrections, a current-state note) can supersede it; code and tests show
what was built and checked. A heuristic, never proof: the owner still reviews every claim."""

import re
from pathlib import PurePosixPath

DOCS = (".md", ".rst", ".txt")
LATER = re.compile(
    r"deviation|correct|errat|canary|attempt|result|finding|current[_-]?state|decision|"
    r"postmortem|retract|invalid|final|summary"
)
PLAN = re.compile(r"protocol|plan|design|proposal|pre-?regist|spec|roadmap|scenario")

ROLE_NOTES = {
    "PLAN": "intent when written; later records override it",
    "RESULT": "a later record: results, canaries, deviations or corrections",
    "CODE": "what was built",
    "TEST": "what was checked",
    "DOC": "documentation",
}


def is_test(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    return (
        name.startswith("test_") or "_test." in name or any(p in {"tests", "test"} for p in parts)
    )


def role(path: str) -> str:
    lower = path.lower()
    if not lower.endswith(DOCS):
        return "TEST" if is_test(lower) else "CODE"
    name = PurePosixPath(lower).name
    if LATER.search(name) or LATER.search(lower) and not PLAN.search(name):
        return "RESULT"
    if PLAN.search(name):
        return "PLAN"
    return "DOC"
