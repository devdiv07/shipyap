"""Voice and readability warnings (C2a). Pure and free: no model, no network, never a block.

Each check is a plain heuristic over the post's words. The lists live in `voice_lists.toml`
beside this file, so a check is extended by adding a line there. The thresholds were set on the
stored real drafts (a8d4e007, c1475bb2, f5b557dd, 5d711e7b; see the C2a working-log entry) so
those posts get a handful of warnings each, not dozens. A warning asks the owner to look again;
it never says the sentence is wrong and never changes the text.

Voice (VOICE_*): a cliché phrase or emoji; an AI-tell word; the "it's not X, it's Y" / "not just
X but Y" contrast; two or more em dashes in one sentence; a rhetorical question as the opening; a
segment opening with an emoji (an emoji bullet); a summary closer ("In short", "Ultimately")
opening the last paragraph; three-item lists used as rhythm (two or more sentences carrying one);
low sentence-length variety across the post.

Structure (C2b): a labelled-section line ("Cause:", "Fix:", "Takeaway:", "TL;DR" ...) and a
Markdown header or bullet/numbered line (VOICE_LABELLED_SECTION, VOICE_MARKDOWN_STRUCTURE). A
warning for p4 drafts like every note here; for voice dry-nerdy (p5) the review gate makes these
two Must fix (`STRUCTURE_CODES`).

Readability (READ_*): an opening sentence over OPENER_MAX_WORDS words or with jargon in it; a
sentence over LONG_SENTENCE_WORDS words; jargon density (identifiers, code names, unexplained
acronyms per word) over JARGON_DENSITY_MAX for the post.
"""

import re
import statistics
import tomllib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from shipyap.editorial.models import Segment

OPENER_MAX_WORDS = 20
LONG_SENTENCE_WORDS = 30
JARGON_DENSITY_MAX = 0.04  # jargon tokens per word, whole post
JARGON_MIN_TOKENS = 4  # a short post with one code name is not dense
FLAT_MIN_SENTENCES = 5
FLAT_MAX_SPREAD = 0.30  # coefficient of variation of sentence lengths, in words, at or below
TRIAD_MIN_SENTENCES = 2
EM_DASH_MAX = 1

CODES = (
    "VOICE_CLICHE",
    "VOICE_AI_WORD",
    "VOICE_CONTRAST",
    "VOICE_EM_DASHES",
    "VOICE_QUESTION_OPENER",
    "VOICE_EMOJI_BULLET",
    "VOICE_SUMMARY_CLOSER",
    "VOICE_TRIADS",
    "VOICE_FLAT_RHYTHM",
    "VOICE_LABELLED_SECTION",
    "VOICE_MARKDOWN_STRUCTURE",
    "READ_OPENER_LONG",
    "READ_OPENER_JARGON",
    "READ_LONG_SENTENCE",
    "READ_JARGON_DENSITY",
)

MESSAGES = {
    "VOICE_CLICHE": "a cliché opener or phrase",
    "VOICE_AI_WORD": "a word that makes posts read as AI-written",
    "VOICE_CONTRAST": 'the "it\'s not X, it\'s Y" / "not just X but Y" pattern',
    "VOICE_EM_DASHES": f"more than {EM_DASH_MAX} em dash in one sentence",
    "VOICE_QUESTION_OPENER": "the post opens with a rhetorical question",
    "VOICE_EMOJI_BULLET": "a line opens with an emoji, used as a bullet",
    "VOICE_SUMMARY_CLOSER": "the last paragraph opens with a summary closer",
    "VOICE_TRIADS": "three-item lists used as rhythm in several sentences",
    "VOICE_FLAT_RHYTHM": "the sentences are all about the same length",
    "VOICE_LABELLED_SECTION": 'a labelled section ("Cause:", "Fix:", "Takeaway:" ...)',
    "VOICE_MARKDOWN_STRUCTURE": "a Markdown header or a bullet or numbered list line",
    "READ_OPENER_LONG": f"the opening sentence is over {OPENER_MAX_WORDS} words",
    "READ_OPENER_JARGON": "the opening sentence uses jargon or code names",
    "READ_LONG_SENTENCE": f"a sentence is over {LONG_SENTENCE_WORDS} words",
    "READ_JARGON_DENSITY": "a lot of jargon for a general reader",
}


@dataclass(frozen=True)
class VoiceNote:
    code: str
    segment_id: str | None  # None: the whole post
    detail: str


@dataclass(frozen=True)
class _Lists:
    cliches: re.Pattern[str] | None
    cliche_emoji: tuple[str, ...]
    ai_words: re.Pattern[str] | None
    closers: re.Pattern[str] | None
    plain_acronyms: frozenset[str]
    formats: dict[str, tuple[str, ...]]
    section_labels: re.Pattern[str] | None
    claim_verbs: re.Pattern[str] | None
    claim_subjects: frozenset[str] = frozenset()
    change_words: re.Pattern[str] | None = None


_LABEL_VERSION = re.compile(r"\bvia draft-model (p[0-9]{1,3})\b")


def version_of(label: str) -> str | None:
    """The drafting prompt version a generator label names ("... via draft-model p5 ..."), or
    None. The tool writes that label (`drafting.prompt.generator_label`); the model cannot."""
    match = _LABEL_VERSION.search(label)
    return match.group(1) if match else None


def is_dry_nerdy(label: str) -> bool:
    """A draft written with prompt p5 (voice dry-nerdy)."""
    return version_of(label) == "p5"


# The two structure notes the review gate makes Must fix for voice dry-nerdy (p5).
STRUCTURE_CODES = ("VOICE_LABELLED_SECTION", "VOICE_MARKDOWN_STRUCTURE")


def _phrase(text: str) -> str:
    out = re.escape(text.strip().casefold())
    out = out.replace(r"\ ", r"\s+").replace("'", "['’]").replace(r"\-", r"[-\s]?")
    return out


def _alternation(phrases: Sequence[str], suffix: str = "") -> re.Pattern[str] | None:
    parts = sorted({_phrase(p) for p in phrases if p.strip()}, key=len, reverse=True)
    if not parts:
        return None
    return re.compile(rf"(?<![\w'’])(?:{'|'.join(parts)}){suffix}(?![\w'’])", re.IGNORECASE)


def _inflected(words: Sequence[str]) -> re.Pattern[str] | None:
    forms: list[str] = []
    for word in words:
        word = word.strip()
        if not word:
            continue
        forms.append(word)
        stem = word[:-1] if word.endswith("e") else word
        forms += [f"{word}s", f"{word}es", f"{word}d", f"{word}ed", f"{stem}ing", f"{stem}ed"]
    return _alternation(forms)


@cache
def lists() -> _Lists:
    """The word lists, read once from `voice_lists.toml` (package data)."""
    raw = tomllib.loads(
        resources.files("shipyap.editorial").joinpath("voice_lists.toml").read_text("utf-8")
    )
    formats = raw.get("formats", {})
    return _Lists(
        cliches=_alternation(raw.get("cliches", [])),
        cliche_emoji=tuple(raw.get("cliche_emoji", [])),
        ai_words=_inflected(raw.get("ai_words", [])),
        closers=re.compile(
            rf"^\s*(?:{'|'.join(_phrase(c) for c in raw.get('summary_closers', []))})\b",
            re.IGNORECASE,
        )
        if raw.get("summary_closers")
        else None,
        plain_acronyms=frozenset(raw.get("plain_acronyms", [])),
        formats={str(k): tuple(str(f) for f in v) for k, v in formats.items()},
        section_labels=re.compile(
            rf"^[ \t*_]*(?:(?:{'|'.join(_phrase(c) for c in raw['section_labels'])})[ \t*_]*:"
            r"|tl;?dr\b)",
            re.IGNORECASE | re.MULTILINE,
        )
        if raw.get("section_labels")
        else None,
        claim_verbs=_inflected(raw.get("framing_claim_verbs", [])),
        claim_subjects=frozenset(w.casefold() for w in raw.get("framing_claim_subjects", [])),
        change_words=_inflected(raw.get("change_words", [])),
    )


# --- sentences ---------------------------------------------------------------------------------

_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_ABBREVIATIONS = frozenset(
    {"e.g", "i.e", "etc", "vs", "cf", "mr", "mrs", "ms", "dr", "no", "fig", "approx", "inc"}
)
_INITIALS = re.compile(r"[a-z](?:\.[a-z])*")


def sentence_ends(text: str) -> list[int]:
    """The end offset of every sentence of `text` (the last is always len(text)). A decimal, a
    version or an abbreviation such as "e.g." does not end one."""
    ends = []
    for match in _END.finditer(text):
        word = text[: match.start() + 1].split()[-1]
        stem = word.strip("\"'“‘(").rstrip(".!?").lower()
        if text[match.start()] == "." and (stem in _ABBREVIATIONS or _INITIALS.fullmatch(stem)):
            continue
        ends.append(match.start() + len(match.group().rstrip()))
    return [*ends, len(text)]


def sentences(text: str) -> list[str]:
    out, start = [], 0
    for end in sentence_ends(text):
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        start = end
    return out


_WORD = re.compile(r"[^\s—–]+")


def words(text: str) -> list[str]:
    """Whitespace-separated words, punctuation stripped; a lone dash is not a word."""
    out = []
    for token in _WORD.findall(text):
        token = token.strip(".,;:!?\"'“”‘’()[]{}")
        if token and any(ch.isalnum() for ch in token):
            out.append(token)
    return out


# --- jargon ------------------------------------------------------------------------------------

_CAMEL = re.compile(r"[a-z][A-Z]")
_LETTER_DIGIT = re.compile(r"(?=.*[A-Za-z])(?=.*\d)")
_PATHLIKE = re.compile(r"\w[./]\w")
# "1st", "1990s", "640-character", "10-line": a number and plain English, not a code name.
_COUNTED = re.compile(r"\d+(?:st|nd|rd|th|s)|\d+(?:[.,]\d+)?-[a-z]+(?:-[a-z]+)*")


def jargon(text: str) -> list[str]:
    """Tokens a general reader would not know: identifiers (snake_case, camelCase), code or file
    names, letter-digit mixes and all-caps words that are not in `plain_acronyms`. Plain
    numbers and hyphenated English words are not jargon."""
    plain = lists().plain_acronyms
    found = []
    for raw in _WORD.findall(text):
        token = raw.strip(".,;:!?\"'“”‘’()[]{}")
        if not token or not any(ch.isalpha() for ch in token):
            continue
        bare = token[:-2] if token.endswith(("'s", "’s")) else token
        upper = re.sub(r"[^A-Za-z]", "", bare)
        if (
            "_" in bare
            or "`" in raw
            or _CAMEL.search(bare)
            or _PATHLIKE.search(bare)
            or (_LETTER_DIGIT.match(bare) and not _COUNTED.fullmatch(bare))
            or (len(upper) >= 2 and upper.isupper() and bare not in plain and upper not in plain)
        ):
            found.append(token)
    return found


# --- checks ------------------------------------------------------------------------------------

_CONTRAST = (
    re.compile(
        r"\b(?:it|this|that)(?:['’]s|\s+is|\s+was)\s+not\b[^.;!?]{1,80}?[,;—–]\s*"
        r"(?:it|this|that)(?:['’]s|\s+is|\s+was)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:isn['’]t|wasn['’]t)\b[^.;!?]{1,80}?[,;—–]\s*(?:it|this|that)(?:['’]s|\s+is|\s+was)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnot\s+(?:just|only|merely)\b[^.;!?]{1,100}?\bbut\b", re.IGNORECASE),
)
_ITEM = r"[\w'’-]+(?:\s[\w'’-]+){0,3}"
_TRIAD = re.compile(rf"\b{_ITEM},\s{_ITEM},?\s(?:and|or)\s{_ITEM}", re.IGNORECASE)
# The chunk before a match, when the match follows a comma: a short chunk is another list item
# ("A, B, C and D" is not a triad); a long one is a clause ("After X happened, A, B and C").
_BEFORE = re.compile(r"(?:^|[,;:—–(])([^,;:—–(]*),\s*$")


def _triad(sentence: str) -> str | None:
    for match in _TRIAD.finditer(sentence):
        before = _BEFORE.search(sentence[: match.start()])
        if before is None or len(words(before.group(1))) > 4:
            return match.group()
    return None


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" and ord(ch) > 0x2000


_MARKDOWN_LINE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]|[-*•+][ \t]+\S|\d{1,2}[.)][ \t]+\S)", re.MULTILINE
)


def structure_notes(segment: Segment) -> list[VoiceNote]:
    """Labelled-section and Markdown-structure lines in one segment."""
    notes = []
    found = lists()
    if found.section_labels and (m := found.section_labels.search(segment.text)):
        notes.append(VoiceNote("VOICE_LABELLED_SECTION", segment.id, m.group().strip(" \t*_")))
    if m := _MARKDOWN_LINE.search(segment.text):
        notes.append(VoiceNote("VOICE_MARKDOWN_STRUCTURE", segment.id, m.group().strip()))
    return notes


def claim_verbs(text: str) -> list[str]:
    """The listed factual verbs (`framing_claim_verbs`) in `text`, in order, once each."""
    pattern = lists().claim_verbs
    return list(dict.fromkeys(m.group() for m in pattern.finditer(text))) if pattern else []


_TOKEN = re.compile(r"[A-Za-z][\w'’-]*|[^\w\s]")
_SKIPPED = frozenset(
    {
        "also",
        "still",
        "never",
        "now",
        "just",
        "always",
        "only",
        "already",
        "even",
        "then",
        "all",
        "both",
        "really",
        "actually",
        "simply",
        "quietly",
        "not",
    }
)


def change_words(text: str) -> list[str]:
    """The `change_words` in `text` (a bug, a fix, a reversal ...), in order, once each."""
    pattern = lists().change_words
    return (
        list(dict.fromkeys(m.group().casefold() for m in pattern.finditer(text))) if pattern else []
    )


def subject_claims(text: str) -> list[str]:
    """Each "<subject> <verb>" in `text` where the subject is the repository, its code, its tests
    or the tool (`framing_claim_subjects`) and the verb a listed factual verb
    (`framing_claim_verbs`), adverbs between them skipped ("the tests also pin"). A listed verb
    with any other subject, or none ("a pleasant change", "easy to read"), is the post's own
    wording, not a claim about the code.

    A pronoun ("it", "they"; "this", "that", "these" only where a clause starts, so never the
    "that" of "a scaffold that says") stands for the code too, but only before an action verb:
    a copula or auxiliary after it ("it is a scaffold", "it can") is the post's own wording."""
    found = lists()
    pattern = found.claim_verbs
    if pattern is None or not found.claim_subjects:
        return []
    tokens = [m.group() for m in _TOKEN.finditer(text)]
    claims = []
    for index, token in enumerate(tokens):
        word = token.casefold()
        pronoun = _pronoun_subject(tokens, index)
        if word not in found.claim_subjects and not pronoun:
            continue
        if index and tokens[index - 1].casefold() == "to":  # "to test": a verb, not a subject
            continue
        following = index + 1
        while following < len(tokens) and (
            tokens[following].casefold() in _SKIPPED
            or (tokens[following].casefold().endswith("ly") and len(tokens[following]) > 3)
        ):
            following += 1
        if following >= len(tokens) or not pattern.fullmatch(tokens[following]):
            continue
        if pronoun and tokens[following].casefold() in _NOT_ACTIONS:
            continue
        claims.append(f"{token} {tokens[following]}")
    return list(dict.fromkeys(claims))


_ANYWHERE_PRONOUNS = frozenset({"it", "they"})
_CLAUSE_PRONOUNS = frozenset({"this", "that", "these"})
_CLAUSE_OPENERS = frozenset({"and", "but", "so", "yet", "or", "because", "while", "when", "then"})
# After a pronoun these say what something is or may be, not what the code does.
_NOT_ACTIONS = frozenset(
    {
        "is", "are", "was", "were", "has", "have", "had", "do", "does", "did", "can", "cannot",
        "can't", "will", "won't", "would", "should", "must",
    }
)  # fmt: skip


def _pronoun_subject(tokens: list[str], index: int) -> bool:
    word = tokens[index].casefold()
    if word in _ANYWHERE_PRONOUNS:
        return True
    if word not in _CLAUSE_PRONOUNS:
        return False
    if index == 0:
        return True
    before = tokens[index - 1]
    return not before[:1].isalnum() or before.casefold() in _CLAUSE_OPENERS


def check_voice(segments: Sequence[Segment]) -> tuple[VoiceNote, ...]:
    """Every voice and readability warning of a post, in post order, whole-post notes last."""
    found = lists()
    notes: list[VoiceNote] = []
    lengths: list[int] = []
    triads: list[str] = []
    all_words: list[str] = []
    all_jargon: list[str] = []
    opened = False
    for segment in segments:
        text = segment.text
        if text[:1] and _is_emoji(text.lstrip()[:1]):
            notes.append(VoiceNote("VOICE_EMOJI_BULLET", segment.id, text.lstrip()[:1]))
        hits = [m.group() for m in found.cliches.finditer(text)] if found.cliches else []
        hits += [e for e in found.cliche_emoji if e in text]
        if hits:
            notes.append(VoiceNote("VOICE_CLICHE", segment.id, ", ".join(dict.fromkeys(hits))))
        if found.ai_words and (ai := [m.group() for m in found.ai_words.finditer(text)]):
            notes.append(VoiceNote("VOICE_AI_WORD", segment.id, ", ".join(dict.fromkeys(ai))))
        notes += structure_notes(segment)
        if any(pattern.search(text) for pattern in _CONTRAST):
            match = next(m for p in _CONTRAST if (m := p.search(text)))
            notes.append(VoiceNote("VOICE_CONTRAST", segment.id, match.group()))
        for sentence in sentences(text):
            count = len(words(sentence))
            lengths.append(count)
            if not opened:
                opened = True
                if sentence.rstrip("\"'”’)").endswith("?"):
                    notes.append(VoiceNote("VOICE_QUESTION_OPENER", segment.id, sentence))
                if count > OPENER_MAX_WORDS:
                    notes.append(VoiceNote("READ_OPENER_LONG", segment.id, f"{count} words"))
                if terms := jargon(sentence):
                    detail = ", ".join(dict.fromkeys(terms))
                    notes.append(VoiceNote("READ_OPENER_JARGON", segment.id, detail))
            if count > LONG_SENTENCE_WORDS:
                notes.append(VoiceNote("READ_LONG_SENTENCE", segment.id, f"{count} words"))
            if sentence.count("—") > EM_DASH_MAX:
                notes.append(
                    VoiceNote("VOICE_EM_DASHES", segment.id, f"{sentence.count('—')} em dashes")
                )
            if (triad := _triad(sentence)) is not None:
                triads.append(f"{segment.id}: {triad}")
        all_words += words(text)
        all_jargon += jargon(text)
    last = _last_paragraph(segments)
    if last is not None and found.closers and (m := found.closers.match(last.text)):
        notes.append(VoiceNote("VOICE_SUMMARY_CLOSER", last.id, m.group().strip()))
    if len(triads) >= TRIAD_MIN_SENTENCES:
        notes.append(VoiceNote("VOICE_TRIADS", None, "; ".join(triads)))
    if len(lengths) >= FLAT_MIN_SENTENCES:
        spread = statistics.pstdev(lengths) / statistics.mean(lengths)
        if spread <= FLAT_MAX_SPREAD:
            detail = f"{min(lengths)}-{max(lengths)} words, spread {spread:.2f}"
            notes.append(VoiceNote("VOICE_FLAT_RHYTHM", None, detail))
    if all_words:
        density = len(all_jargon) / len(all_words)
        if len(all_jargon) >= JARGON_MIN_TOKENS and density > JARGON_DENSITY_MAX:
            terms = ", ".join(dict.fromkeys(all_jargon))
            detail = f"{len(all_jargon)} of {len(all_words)} words ({density:.0%}): {terms}"
            notes.append(VoiceNote("READ_JARGON_DENSITY", None, detail))
    return tuple(notes)


def jargon_score(segments: Sequence[Segment]) -> float:
    """Jargon tokens per word over the whole post (0.0 for an empty post)."""
    text = " ".join(segment.text for segment in segments)
    total = len(words(text))
    return len(jargon(text)) / total if total else 0.0


def _last_paragraph(segments: Sequence[Segment]) -> Segment | None:
    """The first segment of the post's last paragraph."""
    start = None
    for index, segment in enumerate(segments):
        if index == 0 or segment.paragraph_break_before:
            start = segment
    return start
