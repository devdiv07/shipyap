"""Bounded, deterministic source selection; a heuristic, not research significance detection.

Read-only unauthenticated REST through the existing transport. A complete recursive tree is
required; truncated trees, 300-file comparisons and rewritten history need owner attention.
Commit messages never become evidence. Only pinned file bytes enter ingestion and the model.
"""

import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from public_research_os.automation.config import AutomationError
from public_research_os.editorial.sources import SOURCE_FILE_CAP_BYTES, SOURCE_TOTAL_CAP_BYTES
from public_research_os.evidence.policy import check_relative_path
from public_research_os.ingestion.errors import IngestionError
from public_research_os.ingestion.github import (
    DEFAULT_LIMITS,
    GithubReader,
    Transport,
    _Budget,
    _TreePayload,
)
from public_research_os.stories.roles import DOCS as _DOCS
from public_research_os.stories.roles import LATER as _LATER
from public_research_os.stories.roles import is_test, role  # noqa: F401 (re-exported)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _SHA(_Payload):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class _Commit(_Payload):
    tree: _SHA


class _Head(_SHA):
    commit: _Commit


class _Change(_Payload):
    filename: str
    status: str
    changes: int = Field(ge=0)
    patch: str | None = None


class _Comparison(_Payload):
    status: str
    base_commit: _SHA
    merge_base_commit: _SHA
    files: list[_Change]


@dataclass(frozen=True)
class Selection:
    revision: str
    paths: tuple[str, ...]
    unchanged: bool = False


_TEXT = frozenset(
    {
        ".md",
        ".rst",
        ".txt",
        ".py",
        ".rs",
        ".go",
        ".js",
        ".ts",
        ".tsx",
        ".c",
        ".cpp",
        ".h",
        ".java",
        ".jl",
        ".r",
        ".ipynb",
        ".sh",
    }
)
_EXCLUDED = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "generated",
        "__pycache__",
        ".git",
        ".github",
        ".venv",
        "venv",
        "coverage",
        "site-packages",
        ".claude",
        ".agents",
        ".cursor",
        ".codex",
    }
)
# Instructions written for AI coding assistants describe how to work, not what was found.
_AGENT_FILES = re.compile(
    r"^(claude|agents|gemini|copilot-instructions|\.cursorrules|prompt[\w-]*)\.(md|txt)$"
)
_LATER_RANK = (
    re.compile(r"deviation|correct|errat|retract|current[_-]?state"),
    re.compile(r"canary|decision|postmortem"),
    re.compile(r"attempt"),
    re.compile(r"result|finding|final|summary"),
)
_GENERIC = frozenset(
    {
        "experiment",
        "experiments",
        "evidence",
        "report",
        "results",
        "notes",
        "readme",
        "analysis",
        "data",
        "outputs",
        "research",
        "final",
        "summary",
        "model",
        "models",
    }
)
MAX_SELECTED = 6
# Every selected file is also embedded in the draft brief, which a model must read whole.
SELECTION_BUDGET_BYTES = 80 * 1024
_BORING = frozenset(
    {
        "license",
        "license.md",
        "license.txt",
        "changelog.md",
        "package-lock.json",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "pnpm-lock.yaml",
    }
)


def eligible(path: str) -> bool:
    p = PurePosixPath(path.lower())
    return (
        check_relative_path(path) is None
        and len(p.parts) <= 6
        and not _EXCLUDED.intersection(p.parts)
        and p.name not in _BORING
        and p.suffix in _TEXT
        and not p.name.endswith((".min.js", ".d.ts", ".generated.py"))
        and not _AGENT_FILES.match(p.name)
        and p.name != "__init__.py"
    )


def priority(path: str) -> tuple[int, str]:
    lower = path.lower()
    if re.search(r"(?:report|research|experiment|benchmark|paper|analysis|finding)", lower):
        score = 0
    elif lower.endswith((".md", ".rst", ".txt")) and not lower.startswith("readme"):
        score = 1
    elif PurePosixPath(lower).name.startswith("readme"):
        score = 2
    else:
        score = 3
    return score, path


def meaningful(change: _Change) -> bool:
    if change.status not in {"added", "modified", "renamed"} or change.changes < 2:
        return False
    if change.patch is None:
        return change.changes >= 3  # content is still checked by ingestion/editorial
    added, removed = [], []
    for line in change.patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    a = "".join("".join(added).split())
    b = "".join("".join(removed).split())
    return a != b and max(len(a), len(b)) >= 40


_STOP = frozenset({"md", "rst", "txt", "py", "src", "docs", "doc", "test", "tests", "the", "and"})


def _tokens(path: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", path.lower()) if len(t) >= 3 and t not in _STOP}


def _named_in(text: str, candidates: list) -> list:
    """Candidates that `text` names: by full path, by a trailing part of it of two or more
    components (`harness/world.py`), or by a file name that is unique among them."""
    names: dict[str, int] = {}
    for e in candidates:
        base = PurePosixPath(e.path).name
        names[base] = names.get(base, 0) + 1
    found = []
    for e in candidates:
        parts = PurePosixPath(e.path).parts
        tails = ["/".join(parts[k:]) for k in range(len(parts) - 1)]
        if names[parts[-1]] == 1:
            tails.append(parts[-1])
        if any(re.search(rf"(?<![\w.-]){re.escape(tail)}\b", text) for tail in tails):
            found.append(e)
    return found


def _later_rank(path: str) -> tuple[int, int, str]:
    lower = path.lower()
    name = PurePosixPath(lower).name  # the record's own name, not its folder
    if re.search(r"result|finding", name):
        # A results document ("RESULTS_CORRECTED") is a result, even when it was corrected;
        # a deviation, an erratum or a canary says what changed and why.
        strength = len(_LATER_RANK) - 1
    else:
        strength = next(
            (i for i, rx in enumerate(_LATER_RANK) if rx.search(name)), len(_LATER_RANK)
        )
    stale = 1 if re.search(r"invalid|original|unrun|draft", lower) else 0
    return stale, strength, path


_ALWAYS_LATER = re.compile(r"current[_-]?state|corrections?\b|corrections\.|errat")


def _stem_words(path: str) -> list[str]:
    stem = PurePosixPath(path.lower()).stem
    return [w for w in re.split(r"[^a-z0-9]+", stem) if len(w) >= 3 and w not in _STOP]


def evidence_set(candidates: list, lead_text: str | None = None, read=None) -> list:
    """Candidates reordered into one coherent evidence set (the caller keeps the first files
    that fit, at most MAX_SELECTED). A lead document is followed by:
    1. later records that can supersede it: deviations, corrections, canaries, results that
       share its subject, plus any repository-wide current-state or corrections record;
    2. the code those documents point at: named by path, or whose file-name words all occur
       in their text (`responses_adapter.py` for a record about "the Responses API adapter");
       code named by later records ranks above code named only by the lead;
    3. the test sharing the most subject words.
    The rest keep their priority order. `read(entry)` returns a document's text or None and is
    used for at most three later records. Deterministic for the same files and texts."""
    docs = [e for e in candidates if e.path.lower().endswith(_DOCS)]
    if not docs:
        return candidates
    lead = docs[0]
    subject = _tokens(lead.path) - _GENERIC
    named_by_lead = _named_in(lead_text, candidates) if lead_text else []
    later = sorted(
        (
            e
            for e in docs[1:]
            if (_LATER.search(e.path.lower()) and (subject & _tokens(e.path) or e in named_by_lead))
            or _ALWAYS_LATER.search(e.path.lower())
        ),
        key=lambda e: _later_rank(e.path),
    )[:3]
    later_text = " ".join(t for t in (read(e) if read else None for e in later) if t)
    later_counts: dict[str, int] = {}
    for word in re.findall(r"[a-z0-9]+", later_text.lower()):
        later_counts[word] = later_counts.get(word, 0) + 1
    later_words = set(later_counts)
    lead_words = set(re.findall(r"[a-z0-9]+", (lead_text or "").lower()))
    others = [e for e in candidates if not e.path.lower().endswith(_DOCS)]
    code = [e for e in others if not is_test(e.path)]
    tests = [e for e in others if is_test(e.path)]
    named_by_later = _named_in(later_text, code) if later_text else []

    def later_score(e) -> int:
        """How strongly the later records point at this code: being named counts a little,
        being what they talk about (every word of its file name, counted) counts more."""
        words = _stem_words(e.path)
        talked = (
            sum(later_counts.get(w, 0) for w in words)
            if words and all(w in later_words for w in words)
            else 0
        )
        return talked + (6 if e in named_by_later else 0)

    def lead_score(e) -> int:
        words = _stem_words(e.path)
        in_lead = bool(words) and all(w in lead_words for w in words)
        return (3 if e in named_by_lead else 0) + (1 if in_lead else 0)

    ranked_code = sorted(
        code, key=lambda e: (-later_score(e), -lead_score(e), -len(_stem_words(e.path)))
    )
    pointed = [e for e in ranked_code if later_score(e) or lead_score(e)]
    if not pointed:
        # No text to go on (unreadable, or names nothing): the code sharing the subject's words.
        best = max(code, key=lambda e: len(subject & _tokens(e.path)), default=None)
        if best is not None and subject & _tokens(best.path):
            pointed = [best]
    # Six slots: the plan, two later records, two code files, one test; a third later record
    # only when there is no second code file.
    chosen = [lead, *later[:2], *pointed[:2]]
    words = (_tokens(lead.path) | set().union(*(_tokens(e.path) for e in chosen))) - _GENERIC
    named_tests = [e for e in named_by_lead + _named_in(later_text, tests) if e in tests]
    test = (
        named_tests[0]
        if named_tests
        else max(tests, key=lambda e: len(words & _tokens(e.path)), default=None)
    )
    if test is not None and (named_tests or words & _tokens(test.path)):
        chosen.append(test)
    chosen += later[2:3]
    unique = list(dict.fromkeys(chosen))
    return unique + [e for e in candidates if e not in unique]


def select_sources(repo: str, previous: str | None, transport: Transport) -> Selection:
    reader = GithubReader(transport)
    budget = _Budget(transport, DEFAULT_LIMITS, time.monotonic)
    base = f"/repos/{repo}"
    head = reader._json(budget, f"{base}/commits/HEAD", _Head, 256 * 1024)
    if head.sha == previous:
        return Selection(head.sha, (), True)
    changed: set[str] | None = None
    if previous:
        comparison = reader._json(
            budget,
            f"{base}/compare/{previous}...{head.sha}?per_page=1",
            _Comparison,
            2 * 1024 * 1024,
        )
        if (
            comparison.status != "ahead"
            or comparison.base_commit.sha != previous
            or comparison.merge_base_commit.sha != previous
        ):
            raise AutomationError("HISTORY_CHANGED")
        if len(comparison.files) >= 300:
            raise AutomationError("COMPARE_FILE_LIMIT")
        changed = {f.filename for f in comparison.files if meaningful(f)}
        if not changed:
            return Selection(head.sha, ())
    tree = reader._json(
        budget,
        f"{base}/git/trees/{head.commit.tree.sha}?recursive=1",
        _TreePayload,
        DEFAULT_LIMITS.max_tree_json_bytes,
    )
    if tree.sha != head.commit.tree.sha or tree.truncated:
        raise AutomationError("TREE_INCOMPLETE")
    candidates = sorted(
        (
            e
            for e in tree.tree
            if (
                e.type == "blob"
                and e.mode in {"100644", "100755"}
                and e.size is not None
                and 40 <= e.size <= SOURCE_FILE_CAP_BYTES
                and eligible(e.path)
                and (changed is None or e.path in changed)
            )
        ),
        key=lambda e: priority(e.path),
    )

    def read(entry) -> str | None:
        # Up to three extra reads (the lead and two later records), to find the files they
        # name. Verified against the Git object id like any ingested file; never evidence here.
        try:
            return reader._blob(budget, f"{base}/git", entry.path, entry).data.decode(
                "utf-8", "replace"
            )
        except IngestionError:
            return None

    docs = [e for e in candidates if e.path.lower().endswith(_DOCS)]
    lead_text = read(docs[0]) if docs else None
    paths, size = [], 0
    for entry in evidence_set(candidates, lead_text, read):
        if size + entry.size > min(SOURCE_TOTAL_CAP_BYTES, SELECTION_BUDGET_BYTES):
            continue
        # Reuse the ingestion path/case-collision contract for the growing selection.
        from public_research_os.ingestion.request import IngestionRequest

        try:
            IngestionRequest(
                request_version="1",
                workspace_id="selection",
                owner=repo.split("/")[0],
                repo=repo.split("/")[1],
                revision_sha=head.sha,
                paths=tuple([*paths, entry.path]),
            )
        except ValidationError:
            continue
        paths.append(entry.path)
        size += entry.size
        if len(paths) == MAX_SELECTED:
            break
    return Selection(head.sha, tuple(paths))
