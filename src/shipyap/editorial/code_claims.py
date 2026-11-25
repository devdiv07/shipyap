"""C1: claims about code must stay within what the cited lines do. Pure: no I/O, no model, no NLP.

Two narrow, deterministic rules for a sentence that cites CODE lines (role from the path,
`stories.roles`):

- CODE_OUTCOME_UNSUPPORTED (blocking). Code shows what a program is written to do, not what
  happened when it ran. A sentence whose source evidence is CODE only (no TEST or RESULT lines,
  no contributor item) and that states a runtime outcome ("recorded", "confirmed", "minted",
  "succeeded", "written on-chain", "the on-chain record") is refused. Allowed wording describes
  the code: "the handler calls X", "on error it falls back to Y". Words inside double quotes or
  backticks, a word naming a value ("the minted flag"), and a sentence that negates or
  conditions it before the word ("if a call is proposed, it is recorded", "nothing waits for it
  to be confirmed") are not outcome claims.
- CODE_MEANING_INCOMPLETE (a warning here; `review.gate` blocks it until a fact check was sent
  the lines). A sentence that says what a value means ("X reflects Y", "shows which", "describes",
  "tell whether", "show as") is judged against every place that value is set. For each code
  identifier the sentence names that occurs in its cited CODE lines, every assignment of it in the
  CODE files the draft cites is found: `x = `, a `x:` key, a destructured binding, a React-style
  `setX(` call, and a call of a function destructured from the same call as `x` (a hook's
  `writeContract` reassigns its `data: hash`). One level of dependency is followed: the
  identifiers read by a declaration of `x`. Any assignment outside the sentence's own cited lines
  is reported as "path:n-n". These lines are added to the fact check; the model, not this module,
  judges whether they contradict the sentence.

A heuristic over text, never a parser and never proof: it can miss an assignment, and it flags
for review, it does not decide that a sentence is wrong.
"""

import re
from collections.abc import Iterable, Sequence

from shipyap.stories.roles import role

MAX_GAP_LINES = 8

_OUTCOME = re.compile(
    r"\b(?:succeed(?:s|ed)|minted|recorded|confirmed|completed|processed|persisted)\b"
    r"|\b(?:written|stored|saved)\s+(?:on[- ]?chain|to\s+(?:the\s+)?"
    r"(?:chain|blockchain|ledger|database|disk))\b"
    r"|\bon[- ]?chain\s+records?\b",
    re.IGNORECASE,
)
# "the minted flag", "a recorded status": the word names a value, it does not claim an outcome.
_VALUE_NOUN = re.compile(
    r"\s+(?:flag|state|status|field|variable|value|label|badge|toast|message|event|column|"
    r"property|prop|key|check|boolean)s?\b",
    re.IGNORECASE,
)
# A sentence that negates or conditions the outcome describes what the code is written to do
# ("if the model proposes a call, it is recorded", "nothing waits for it to be confirmed").
_CONDITION = re.compile(
    r"\b(?:not|never|no|nothing|without|before|until|unless|if|when|whenever|whether|once|"
    r"cannot|can't|doesn't|isn't|won't)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|`[^`]*`")

_MEANING = re.compile(
    r"\b(?:reflect(?:s|ed|ing)?|describ(?:e|es|ed|ing)|indicat(?:e|es|ed|ing)|"
    r"signal(?:s|led|ling)?|means)\b"
    r"|\btell(?:s|ing)?\b(?:\s+\S+){0,3}?\s+(?:whether|which|if)\b"
    r"|\b(?:show|shows|shown|showing|see|seen)\s+(?:as|which|whether)\b"
    r"|\bmark(?:s|ed)?\b(?:\s+\S+){0,3}?\s+as\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_CODE_STRING = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`")
_COMMENT = re.compile(r"//.*$|#.*$|/\*.*?\*/")
_DECLARE = re.compile(r"\b(?:const|let|var)\s+([{\[][^=]*[}\]]|[A-Za-z_$][\w$]*)\s*(?::[^=]*)?=")
_KEYWORDS = frozenset(
    "const let var function return if else for while await async new import export from "
    "true false null undefined this class extends typeof try catch finally".split()
)


def outcome_claims(prose: str) -> list[str]:
    """The runtime-outcome phrases a sentence states (none inside double quotes or backticks)."""
    text = _QUOTED.sub(lambda m: " " * len(m.group()), prose)
    found = []
    for match in _OUTCOME.finditer(text):
        if _VALUE_NOUN.match(text, match.end()):
            continue
        start = max((m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())), default=0)
        if _CONDITION.search(text, start, match.start()):
            continue
        found.append(match.group())
    return found


def code_only(paths: Iterable[str], author_refs: Sequence[str]) -> bool:
    """CODE evidence and no TEST, RESULT or contributor item beside it."""
    roles = {role(path) for path in paths}
    return "CODE" in roles and not roles & {"TEST", "RESULT"} and not author_refs


def means_claim(prose: str) -> bool:
    return _MEANING.search(_QUOTED.sub(" ", prose)) is not None


def _masked(line: str) -> str:
    """Code with string literals and comments blanked, so only code is matched."""
    line = _CODE_STRING.sub(lambda m: " " * len(m.group()), line)
    return _COMMENT.sub("", line)


def _bound_names(pattern: str) -> list[str]:
    """Names bound by a declaration target: `x`, `{ a, b: c, d = 1 }`, `[x, setX]`."""
    if not pattern.startswith(("{", "[")):
        return [pattern]
    inner = pattern[1:-1]
    names = []
    for part in inner.split(","):
        part = part.split("=")[0]
        part = part.split(":")[-1]
        name = _WORD.search(part)
        if name:
            names.append(name.group())
    return names


def _statement(lines: Sequence[str], index: int, limit: int = 10) -> str:
    """The masked statement starting at `lines[index]`, up to a balanced line end."""
    parts, depth = [], 0
    for line in lines[index : index + limit]:
        code = _masked(line)
        parts.append(code)
        depth += sum(code.count(c) for c in "({[") - sum(code.count(c) for c in ")}]")
        if depth <= 0 and (code.rstrip().endswith((";", ")", "}")) or depth < 0):
            break
    return "\n".join(parts)


def assignments(
    name: str, files: dict[str, Sequence[str]]
) -> tuple[set[tuple[str, int]], set[str]]:
    """Where `name` is set in `files` ((path, line) pairs), and the identifiers its declarations
    read (one level of dependency)."""
    word = re.escape(name)
    setter = (  # React's `const [x, setX]`: only for a lower-case value name
        rf"|set{re.escape(name[:1].upper() + name[1:])}\s*\(" if name[:1].islower() else ""
    )
    direct = re.compile(rf"(?<![\w$])(?:{word}\s*=(?![=>])|{word}\s*:(?!:){setter})")
    found: set[tuple[str, int]] = set()
    reads: set[str] = set()
    for path, lines in files.items():
        siblings: set[str] = set()
        binding: set[int] = set()  # the lines that bind `name` (and its siblings)
        for number, line in enumerate(lines, 1):
            code = _masked(line)
            declared = _DECLARE.search(code)
            if declared and name in _bound_names(declared.group(1)):
                found.add((path, number))
                binding.add(number)
                siblings |= set(_bound_names(declared.group(1))) - {name}
                statement = _statement(lines, number - 1)
                right = statement.split("=", 1)[1] if "=" in statement else ""
                reads |= {w for w in _WORD.findall(right) if w not in _KEYWORDS} - {name}
            elif direct.search(code) and not code.lstrip().startswith(("import ", "from ")):
                found.add((path, number))
        if siblings:
            call = re.compile(rf"(?<![\w$.])(?:{'|'.join(map(re.escape, sorted(siblings)))})\s*\(")
            found |= {
                (path, number)
                for number, line in enumerate(lines, 1)
                if number not in binding and call.search(_masked(line))
            }
    return found, reads


def meaning_gaps(
    prose: str,
    cited: Sequence[tuple[str, int, int]],
    files: dict[str, Sequence[str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(identifiers, "path:n-n" lines) for a meaning claim: the assignments of the values it
    names that lie outside its own cited lines. `cited` is the sentence's located references,
    `files` the CODE files the draft cites. Empty when the sentence makes no meaning claim."""
    code_refs = [(p, s, e) for p, s, e in cited if p in files]
    if not code_refs or not means_claim(prose):
        return (), ()
    cited_words: set[str] = set()
    for path, start, end in code_refs:
        for line in files[path][start - 1 : end]:
            cited_words |= set(_WORD.findall(line))
    named = [
        w
        for w in dict.fromkeys(_WORD.findall(_QUOTED.sub(" ", prose)))
        if w in cited_words and w not in _KEYWORDS
    ]
    inside = {(p, n) for p, s, e in code_refs for n in range(s, e + 1)}
    identifiers, gaps = [], set()
    for name in named:
        found, reads = assignments(name, files)
        if not found:
            continue  # not a value set anywhere in these files (an English word, an import)
        for read in sorted(reads):
            dependency, _ = assignments(read, files)
            found |= dependency
        outside = found - inside
        if outside:
            identifiers.append(name)
            gaps |= outside
    ordered = sorted(gaps)[:MAX_GAP_LINES]
    return tuple(identifiers), tuple(f"{p}:{n}-{n}" for p, n in ordered)


def gap_labels(detail: str | None) -> tuple[str, ...]:
    """The "path:n-n" lines a CODE_MEANING_INCOMPLETE finding's detail lists."""
    if not detail or "; " not in detail:
        return ()
    return tuple(label for label in detail.split("; ", 1)[1].split(", ") if label)
